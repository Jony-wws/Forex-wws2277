"""ФИНАЛЬНЫЙ ПРОГНОЗ ДЛЯ МЕНЯ — единые сигналы для открытия сделок на РЕАЛЬНОМ
счёте.

User explicit ask (2026-05-04 ~01:30 UTC):
    "я хочу что бы финальный прогноз был всё 27 валюти … нужно найти подод для
    каждого валюти и сессиях отденый подходит нужно … 70% win rate во всех
    валютах и сессиях".

Архитектура (2026-05-04, второй итоген)
=======================================

Раньше функция ``build()`` возвращала ОДИН сигнал — топ-1 из rankings.
Теперь возвращаем СПИСОК из всех 28 пар, каждая со своими 8-ю проверками
и индивидуальным вердиктом GO / GO_CAUTION / WAIT.

Логика на каждую пару (одинаковая, чтобы единая шкала):

1. Берём её ranking-строку из ``state/forecasts.json`` (тот же источник
   правды, что paper_trader).
2. Прогоняем 8 валидаторов:

       a) probability ≥ 70 (free 70%-gate, единственный обязательный фильтр)
       b) рынок открыт прямо сейчас (или открывается ≤30 мин)
       c) нет high-impact новостей в ±30 мин для этой пары
       d) ``meta_strategy[pair:current_session]`` ≥ PROBABLE
          (это и есть индивидуальный подход для (пары × сессии))
       e) ансамбль агентов сходится с side
       f) macro report не 🔴
       g) political report не 🔴
       h) state-файлы свежие (forecasts.json не старше 60 мин)

3. Суммируем красные / жёлтые / зелёные → выводим вердикт:

       n_red == 0 and n_yellow == 0   → GO
       n_red == 0 and n_yellow ≤ 2    → GO_CAUTION
       n_red ≥ 1                      → WAIT

   Для WAIT-пар добавляем человекочитаемое объяснение какие именно проверки
   их блокируют (одной фразой).

4. Сортируем итоговый список GO → GO_CAUTION → WAIT, внутри каждой группы
   — по probability_pct убывающе.

Никаких симуляций, всё из реальных state-файлов которые уже использует
paper_trader. Это «финальный фильтр» поверх существующей логики.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import config

log = logging.getLogger("final_signal")
_STATE = config.STATE_DIR


# ═══════════════════════════════════════════════════════════════════
# helpers
# ═══════════════════════════════════════════════════════════════════
def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _load(path: Path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default
    except Exception as e:
        log.warning(f"_load({path}): {e}")
        return default


def _file_age_min(path: Path) -> float | None:
    if not path.exists():
        return None
    import time as _t
    return max(0.0, (_t.time() - path.stat().st_mtime) / 60.0)


def _check(name_ru: str, ok: bool | None, detail_ru: str = "") -> dict:
    """Returns one validator result. ``ok=True`` → green; ``False`` → red;
    ``None`` → yellow ("неизвестно / нет данных")."""
    if ok is True:
        return {"status": "green", "name_ru": name_ru, "detail_ru": detail_ru}
    if ok is False:
        return {"status": "red", "name_ru": name_ru, "detail_ru": detail_ru}
    return {"status": "yellow", "name_ru": name_ru, "detail_ru": detail_ru}


def _current_session(now: datetime) -> str:
    """Простая мапа UTC-часа → название сессии. Совпадает с config.SESSIONS."""
    h = now.hour
    if 22 <= h or h < 8:
        return "Asia"
    if 8 <= h < 13:
        return "London"
    if 13 <= h < 17:
        return "Overlap"
    return "NY"  # 17–22 UTC


def _session_ru(s: str) -> str:
    return {
        "Asia": "Азия",
        "London": "Лондон",
        "Overlap": "Лондон+NY",
        "NY": "Нью-Йорк",
    }.get(s, s)


def _fmt_eta(secs: int) -> str:
    if secs <= 0:
        return "—"
    if secs < 60:
        return f"{secs}с"
    m = secs // 60
    if m < 60:
        return f"{m}м"
    h = m // 60
    return f"{h}ч {m % 60}м"


# ═══════════════════════════════════════════════════════════════════
# Кэшированные «глобальные» проверки — рассчитываются один раз на запрос,
# потом подмешиваются в каждый из 28 сигналов (рынок, news не зависят от
# пары, кроме news_blackout — он ПО ПАРЕ).
# ═══════════════════════════════════════════════════════════════════
class _GlobalContext:
    """Контейнер для проверок которые одинаковы для всех 28 пар."""

    def __init__(self, now: datetime):
        self.now = now
        self.session = _current_session(now)

        # market open/close — единая для всех пар
        self.market_ok: bool | None = None
        self.market_detail = ""
        try:
            from . import market_hours as mh
            snap = mh.market_status(now)
            is_open = bool(snap.get("is_open"))
            secs_to_open = int(snap.get("seconds_until_open") or 0)
            if is_open:
                self.market_ok = True
                secs_to_close = int(snap.get("seconds_until_close") or 0)
                self.market_detail = f"Рынок открыт. До закрытия: {_fmt_eta(secs_to_close)}."
            elif 0 < secs_to_open <= 30 * 60:
                self.market_ok = None
                self.market_detail = f"Рынок откроется через {_fmt_eta(secs_to_open)}."
            else:
                self.market_ok = False
                self.market_detail = f"Рынок закрыт. Откроется через {_fmt_eta(secs_to_open)}."
        except Exception as e:
            self.market_detail = f"Не удалось проверить: {e}"

        # macro / political — единые для всех пар
        self.macro_check: dict | None = None
        self.political_check: dict | None = None
        try:
            from . import agent_reports as ar
            rep = ar.all_reports() or {}
            for ch_key, ch_label, attr in [
                ("macro", "Макро-фон", "macro_check"),
                ("political", "Политический фон", "political_check"),
            ]:
                r = (rep.get("reports", {}) or {}).get(ch_key, {}) or {}
                v = r.get("verdict_ru", "")
                if v.startswith("🟢"):
                    setattr(self, attr, _check(ch_label, True, v))
                elif v.startswith("🔴"):
                    setattr(self, attr, _check(ch_label, False, v))
                else:
                    setattr(self, attr, _check(ch_label, None, v))
        except Exception as e:
            self.macro_check = _check("Макро-фон", None, f"Недоступно: {e}")
            self.political_check = _check("Политический фон", None, f"Недоступно: {e}")

        # forecasts.json freshness — общий
        self.forecasts_age_min = _file_age_min(_STATE / "forecasts.json") or 999
        if self.forecasts_age_min < 60:
            self.fresh_check = _check(
                "Данные forecasts.json свежие",
                True, f"Обновлено {int(self.forecasts_age_min)} мин назад."
            )
        elif self.forecasts_age_min < 240:
            self.fresh_check = _check(
                "Данные forecasts.json свежие",
                None,
                f"Обновлено {int(self.forecasts_age_min)} мин назад — приемлемо, но "
                f"скоро устареет."
            )
        else:
            h = self.forecasts_age_min // 60
            self.fresh_check = _check(
                "Данные forecasts.json свежие",
                False,
                f"Обновлено {int(h)}ч назад — слишком давно. Подожди обновления."
            )

        # meta_strategy кэш
        meta = _load(_STATE / "meta_strategy.json", {})
        self.meta_cells = (meta or {}).get("cells") or {}


# ═══════════════════════════════════════════════════════════════════
# Один сигнал на одну пару
# ═══════════════════════════════════════════════════════════════════
def _build_for_row(row: dict, ctx: _GlobalContext) -> dict | None:
    """Берёт одну строку из ``forecasts.rankings`` и возвращает финальный
    сигнал для этой пары (с 8-ю проверками + verdict).
    """
    pair = row.get("pair")
    side = row.get("side")
    if not pair or side not in ("BUY", "SELL"):
        return None

    prob_pct = float(row.get("probability_pct") or 0)
    expiry_h = row.get("recommended_hours") or 3
    sess_now = ctx.session

    checks: list[dict] = []

    # a) probability
    checks.append(_check(
        "Вероятность ≥ 70%",
        prob_pct >= 70,
        f"{prob_pct:.0f}% — {'выше' if prob_pct >= 70 else 'ниже'} порога 70%."
    ))

    # b) market (общий)
    checks.append(_check("Рынок открыт", ctx.market_ok, ctx.market_detail))

    # c) news blackout — ПО ПАРЕ
    news_ok: bool | None = None
    news_detail = ""
    try:
        from .data import news as news_mod
        in_blackout = bool(news_mod.is_blackout(pair, ctx.now, window_min=30))
        if in_blackout:
            news_ok = False
            news_detail = f"Для {pair}: high-impact новости в ±30 мин — не открывать."
        else:
            news_ok = True
            news_detail = f"Для {pair}: ±30 мин high-impact новостей нет."
    except Exception as e:
        news_detail = f"Не удалось проверить: {e}"
    checks.append(_check("News blackout (±30 мин)", news_ok, news_detail))

    # d) meta_strategy — индивидуальная стратегия для (пара × сессия)
    cell = ctx.meta_cells.get(f"{pair}:{sess_now}")
    meta_ok: bool | None = None
    if cell is None:
        meta_detail = (f"Стратегия для {pair} в сессии {_session_ru(sess_now)} "
                       f"ещё не оценена sweep-ом. Подожди следующего прогона.")
    else:
        status = (cell.get("status") or "").upper()
        wr = cell.get("win_rate_pct")
        n = cell.get("trades", 0)
        if status == "QUALIFIED":
            meta_ok = True
            meta_detail = f"🟢 QUALIFIED · WR {wr}% · n={n} (≥70%, ≥10 трейдов)"
        elif status == "PROBABLE":
            meta_ok = True
            meta_detail = f"🟡 PROBABLE · WR {wr}% · n={n} — подходит."
        elif status == "FROZEN":
            meta_ok = False
            meta_detail = f"🔴 FROZEN · WR {wr}% · n={n} — sweep не нашёл edge."
        else:
            meta_detail = f"status={status} · WR {wr}% · n={n}"
    checks.append(_check(
        f"Стратегия для {pair} в сессии «{_session_ru(sess_now)}»",
        meta_ok, meta_detail
    ))

    # e) ансамбль
    af = row.get("agents_for_count") or 0
    aa = row.get("agents_against_count") or 0
    ens_total = af + aa
    if ens_total == 0:
        checks.append(_check("Ансамбль агентов согласен", None,
                             "Голосов нет — данные ансамбля недоступны."))
    elif side == "BUY" and af >= aa:
        checks.append(_check(
            "Ансамбль агентов согласен",
            True, f"BUY: {af} за / {aa} против."
        ))
    elif side == "SELL" and aa >= af:
        checks.append(_check(
            "Ансамбль агентов согласен",
            True, f"SELL: {aa} против цены / {af} за."
        ))
    else:
        checks.append(_check(
            "Ансамбль агентов согласен",
            False,
            f"Конфликт: forecast говорит {side}, "
            f"ансамбль 'за' = {af}, 'против' = {aa}."
        ))

    # f, g) macro / political (общие)
    checks.append(ctx.macro_check)
    checks.append(ctx.political_check)

    # h) forecasts freshness (общая)
    checks.append(ctx.fresh_check)

    # ═══ verdict ═══
    n_red = sum(1 for c in checks if c["status"] == "red")
    n_yellow = sum(1 for c in checks if c["status"] == "yellow")
    n_green = sum(1 for c in checks if c["status"] == "green")

    if n_red == 0 and n_yellow == 0:
        verdict = "GO"
        verdict_ru = (f"🟢 ОТКРЫВАЙ {side} {pair} — все {n_green} проверок "
                      f"зелёные. Экспайри {expiry_h}ч.")
    elif n_red == 0 and n_yellow <= 2:
        verdict = "GO_CAUTION"
        verdict_ru = (f"🟡 МОЖНО {side} {pair} С ОСТОРОЖНОСТЬЮ — "
                      f"{n_green} зелёных, {n_yellow} жёлтых, "
                      f"экспайри {expiry_h}ч.")
    elif n_red >= 1:
        blockers = [c for c in checks if c["status"] == "red"]
        names = ", ".join(b["name_ru"] for b in blockers[:2])
        verdict = "WAIT"
        verdict_ru = f"🔴 ЖДУ — блокирует: {names}."
    else:
        verdict = "WAIT"
        verdict_ru = (f"🟡 ПОДОЖДИ — слишком много неопределённости "
                      f"({n_yellow} жёлтых проверок).")

    # короткий «короткий блокер» для сортировки + UI
    short_blocker = ""
    for c in checks:
        if c["status"] == "red":
            short_blocker = c["name_ru"]
            break

    return {
        "pair": pair,
        "side": side,
        "side_ru": "Покупка (BUY)" if side == "BUY"
                   else "Продажа (SELL)" if side == "SELL" else side,
        "probability_pct": prob_pct,
        "expiry_hours": expiry_h,
        "current_price": row.get("current_price"),
        "verdict": verdict,
        "verdict_ru": verdict_ru,
        "short_blocker": short_blocker,
        "checks": checks,
        "summary_counts": {"green": n_green, "yellow": n_yellow, "red": n_red},
    }


# ═══════════════════════════════════════════════════════════════════
# Public API
# ═══════════════════════════════════════════════════════════════════
def build_all() -> dict:
    """Возвращает финальные сигналы по ВСЕМ парам.

    Структура ответа::

        {
          "as_of_utc": "...",
          "session_now": "Asia",
          "session_now_ru": "Азия",
          "global_context": {
            "market_ok": true|false|null,
            "market_detail": "...",
            "macro_verdict_ru": "🟢 ...",
            "political_verdict_ru": "🟡 ...",
            "forecasts_age_min": 15,
          },
          "summary": {
            "total": 28,
            "go": 3, "go_caution": 5, "wait": 20,
            "qualified_cells_for_session": 14,
          },
          "signals": [
             { pair, side, probability_pct, verdict, checks, ... },
             ... отсортировано GO → GO_CAUTION → WAIT
          ]
        }
    """
    now = _utcnow()
    ctx = _GlobalContext(now)

    fc = _load(_STATE / "forecasts.json", None)
    rankings = (fc or {}).get("rankings") or []

    if not rankings:
        return {
            "as_of_utc": now.isoformat(),
            "session_now": ctx.session,
            "session_now_ru": _session_ru(ctx.session),
            "global_context": {
                "market_ok": ctx.market_ok,
                "market_detail": ctx.market_detail,
                "macro_verdict_ru": (ctx.macro_check or {}).get("detail_ru", ""),
                "political_verdict_ru": (ctx.political_check or {}).get("detail_ru", ""),
                "forecasts_age_min": int(ctx.forecasts_age_min),
            },
            "summary": {"total": 0, "go": 0, "go_caution": 0, "wait": 0,
                        "qualified_cells_for_session": 0},
            "signals": [],
            "error_ru":
                "Главный источник forecasts.json пуст. Подожди 5–10 минут после "
                "старта системы.",
        }

    signals: list[dict] = []
    for row in rankings:
        sig = _build_for_row(row, ctx)
        if sig is not None:
            signals.append(sig)

    # сортировка: GO > GO_CAUTION > WAIT, внутри — по probability убывающе
    rank = {"GO": 0, "GO_CAUTION": 1, "WAIT": 2}
    signals.sort(key=lambda s: (rank.get(s["verdict"], 9),
                                -float(s.get("probability_pct") or 0)))

    summary = {
        "total": len(signals),
        "go": sum(1 for s in signals if s["verdict"] == "GO"),
        "go_caution": sum(1 for s in signals if s["verdict"] == "GO_CAUTION"),
        "wait": sum(1 for s in signals if s["verdict"] == "WAIT"),
        "qualified_cells_for_session": sum(
            1 for k, v in ctx.meta_cells.items()
            if k.endswith(":" + ctx.session)
            and (v or {}).get("status", "").upper() in ("QUALIFIED", "PROBABLE")
        ),
    }

    return {
        "as_of_utc": now.isoformat(),
        "session_now": ctx.session,
        "session_now_ru": _session_ru(ctx.session),
        "global_context": {
            "market_ok": ctx.market_ok,
            "market_detail": ctx.market_detail,
            "macro_verdict_ru": (ctx.macro_check or {}).get("detail_ru", ""),
            "political_verdict_ru": (ctx.political_check or {}).get("detail_ru", ""),
            "forecasts_age_min": int(ctx.forecasts_age_min),
        },
        "summary": summary,
        "signals": signals,
    }


def build() -> dict:
    """Backwards-compatible single-signal API.

    Возвращает ТОП-1 сигнал из ``build_all()`` обогащённый ``alternates`` и
    ``reasoning_ru``. Старая UI-секция использует именно этот формат.
    """
    full = build_all()
    signals = full.get("signals") or []
    if not signals:
        return {
            "as_of_utc": full.get("as_of_utc"),
            "session_now": full.get("session_now"),
            "session_now_ru": full.get("session_now_ru"),
            "verdict": "WAIT",
            "verdict_ru": "🟡 НЕТ ДАННЫХ — forecasts.json пуст или отсутствует.",
            "pair": None,
            "side": None,
            "probability_pct": 0,
            "expiry_hours": None,
            "checks": [
                _check("Источник forecasts.json существует", False,
                       "Файл отсутствует или пуст. Запусти forecast_scanner.")
            ],
            "reasoning_ru":
                "Главный источник прогнозов недоступен. Подожди 5–10 минут после "
                "старта системы или проверь логи forecast_scanner.",
            "summary_counts": {"green": 0, "yellow": 0, "red": 1},
            "alternates": [],
        }

    top = signals[0]
    out = dict(top)  # shallow copy

    # narrative
    pair = top["pair"]
    side = top["side"]
    prob_pct = top["probability_pct"]
    expiry_h = top["expiry_hours"]
    verdict = top["verdict"]
    parts = [
        f"Лучший сигнал по PROGNOZY-28: <b>{pair} {side}</b> с вероятностью "
        f"<b>{prob_pct:.0f}%</b> (источник forecast_scanner — тот же которым "
        f"пользуется paper_trader).",
    ]
    if verdict == "GO":
        parts.append(
            f"Все 8 проверок зелёные. На реальном счёте можно открывать {side} {pair} "
            f"с экспайри {expiry_h}ч."
        )
    elif verdict == "GO_CAUTION":
        parts.append(
            "Критических блокеров нет, но есть пара жёлтых проверок (\"данных нет\" "
            "— не \"не сходится\"). Можно открывать с меньшим объёмом."
        )
    else:
        parts.append(
            "Открывать НЕ рекомендуется. Подожди когда красные проверки перейдут в "
            "зелёные (или попробуй другую пару из списка GO_CAUTION/GO ниже)."
        )
    out["reasoning_ru"] = " ".join(parts)
    out["as_of_utc"] = full.get("as_of_utc")
    out["session_now"] = full.get("session_now")
    out["session_now_ru"] = full.get("session_now_ru")
    out["alternates"] = [
        {"pair": s["pair"], "side": s["side"],
         "probability_pct": s["probability_pct"], "verdict": s["verdict"]}
        for s in signals[1:5]
    ]
    return out
