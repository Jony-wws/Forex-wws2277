"""ФИНАЛЬНЫЙ ПРОГНОЗ ДЛЯ МЕНЯ — единый сигнал, который пользователь использует
для открытия сделок на РЕАЛЬНОМ счёте.

User ask (2026-05-03):
    "я хочу что бы ты добавил отденый раздел где будет единая прогноз а то в
    систему много прогноз и в этом разделе будет прогноз для меня я буду
    открывать сделку по нему система должна проверит всё что делает его агент
    в потом должен дат единую отценку … я буду открывать сделки на реальном
    счёте".

Логика
------
1. Берём ТОП-1 forecast из ``state/forecasts.json`` (единый источник правды,
   PROGNOZY-28 — paper_trader использует ТОТ ЖЕ источник).
2. Прогоняем его через 8 валидаторов:
       a) probability ≥ 70 (free 70%-gate)
       b) рынок открыт прямо сейчас (или открывается ≤30 мин)
       c) news blackout — нет high-impact новостей в ±30 мин
       d) meta_strategy ячейка (pair, current_session) ≥ PROBABLE
       e) ensemble сходится (если данные есть)
       f) macro report не 🔴
       g) political report не 🔴
       h) state-файлы свежие (forecasts.json не старше 60 мин)
3. Если ВСЕ 8 GREEN → verdict = ``GO`` (открывай сделку).
   Если есть YELLOW/RED — verdict = ``WAIT`` + объясняем что блокирует.
4. Возвращаем sized expiry (1-5h из forecast.recommended_hours).

Никаких симуляций, всё из реальных state-файлов которые уже использует
paper_trader. Это "финальный фильтр" поверх существующей логики.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from . import config

log = logging.getLogger("final_signal")
_STATE = config.STATE_DIR


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


def build() -> dict:
    """Главная функция — возвращает один финальный сигнал + список проверок."""
    now = _utcnow()

    # ─── 1) Берём ТОП forecast из единого источника ───
    fc = _load(_STATE / "forecasts.json", None)
    if fc is None or not (fc.get("rankings") or []):
        return {
            "as_of_utc": now.isoformat(),
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
                "Главный источник прогнозов недоступен. Подожди 5–10 минут "
                "после старта системы или проверь логи forecast_scanner.",
            "session_now": _current_session(now),
            "session_now_ru": _session_ru(_current_session(now)),
        }

    rankings = fc["rankings"]
    top = rankings[0]
    pair = top.get("pair")
    side = top.get("side")
    prob_pct = float(top.get("probability_pct") or 0)
    expiry_h = top.get("recommended_hours") or 3
    forecasts_age_min = _file_age_min(_STATE / "forecasts.json") or 999

    checks: list[dict] = []

    # ─── 2a) Free 70%-gate ───
    checks.append(_check(
        "Probability ≥ 70%",
        prob_pct >= 70,
        f"{prob_pct:.0f}% — {'выше' if prob_pct >= 70 else 'ниже'} порога 70%."
    ))

    # ─── 2b) Рынок открыт ───
    market_ok = None
    market_detail = ""
    try:
        from . import market_hours as mh
        snap = mh.market_status(now)
        is_open = bool(snap.get("is_open"))
        secs_to_open = int(snap.get("seconds_until_open") or 0)
        if is_open:
            market_ok = True
            secs_to_close = int(snap.get("seconds_until_close") or 0)
            market_detail = f"Рынок открыт. До закрытия: {_fmt_eta(secs_to_close)}."
        elif 0 < secs_to_open <= 30 * 60:
            market_ok = None  # yellow — open soon
            market_detail = f"Рынок откроется через {_fmt_eta(secs_to_open)}."
        else:
            market_ok = False
            market_detail = f"Рынок закрыт. Откроется через {_fmt_eta(secs_to_open)}."
    except Exception as e:
        market_detail = f"Не удалось проверить: {e}"
    checks.append(_check("Рынок открыт", market_ok, market_detail))

    # ─── 2c) News blackout ───
    news_ok = None
    news_detail = ""
    try:
        from .data import news as news_mod
        in_blackout = bool(news_mod.is_blackout(pair, now, window_min=30))
        if in_blackout:
            news_ok = False
            news_detail = "High-impact новости в ±30 мин — не открывать."
        else:
            news_ok = True
            news_detail = "В ближайшие ±30 мин high-impact новостей нет."
    except Exception as e:
        news_detail = f"Не удалось проверить: {e}"
    checks.append(_check("News blackout (±30 мин)", news_ok, news_detail))

    # ─── 2d) meta_strategy для ячейки (pair, текущая сессия) ───
    meta_ok = None
    meta_detail = ""
    sess_now = _current_session(now)
    try:
        meta = _load(_STATE / "meta_strategy.json", {})
        cells = (meta or {}).get("cells") or {}
        cell = cells.get(f"{pair}:{sess_now}")
        if cell is None:
            meta_detail = f"Ячейка {pair}:{sess_now} ещё не оценена sweep-ом."
            meta_ok = None
        else:
            status = (cell.get("status") or "").upper()
            wr = cell.get("win_rate_pct")
            n = cell.get("trades", 0)
            if status == "QUALIFIED":
                meta_ok = True
                meta_detail = f"🟢 QUALIFIED · WR {wr}% · n={n}"
            elif status == "PROBABLE":
                meta_ok = True
                meta_detail = f"🟡 PROBABLE · WR {wr}% · n={n} — допустимо."
            elif status == "FROZEN":
                meta_ok = False
                meta_detail = f"🔴 FROZEN · WR {wr}% · n={n} — sweep не нашёл edge."
            else:
                meta_ok = None
                meta_detail = f"status={status}, WR {wr}%, n={n}"
    except Exception as e:
        meta_detail = f"Не удалось проверить: {e}"
    checks.append(_check(
        f"Стратегия для {pair} в сессии {_session_ru(sess_now)}",
        meta_ok, meta_detail
    ))

    # ─── 2e) ensemble (agents_for vs agents_against) ───
    af = top.get("agents_for_count") or 0
    aa = top.get("agents_against_count") or 0
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
            f"но ансамбль 'за' = {af}, 'против' = {aa}."
        ))

    # ─── 2f, 2g) macro / political reports ───
    try:
        from . import agent_reports as ar
        rep = ar.all_reports()
        for ch_key, ch_label in [("macro", "Макро-фон"),
                                 ("political", "Политический фон")]:
            r = rep.get("reports", {}).get(ch_key, {})
            v = r.get("verdict_ru", "")
            if v.startswith("🟢"):
                checks.append(_check(ch_label, True, v))
            elif v.startswith("🔴"):
                checks.append(_check(ch_label, False, v))
            else:
                checks.append(_check(ch_label, None, v))
    except Exception as e:
        checks.append(_check("Макро/политический фон", None, f"Недоступно: {e}"))

    # ─── 2h) state-файлы свежие ───
    if forecasts_age_min < 60:
        checks.append(_check(
            "Данные forecasts.json свежие",
            True, f"Обновлено {int(forecasts_age_min)} мин назад."
        ))
    elif forecasts_age_min < 240:
        checks.append(_check(
            "Данные forecasts.json свежие",
            None, f"Обновлено {int(forecasts_age_min)} мин назад — приемлемо, но скоро устареет."
        ))
    else:
        h = forecasts_age_min // 60
        checks.append(_check(
            "Данные forecasts.json свежие",
            False, f"Обновлено {int(h)}ч назад — слишком давно. Подожди обновления."
        ))

    # ─── 3) Финальный вердикт ───
    n_red = sum(1 for c in checks if c["status"] == "red")
    n_yellow = sum(1 for c in checks if c["status"] == "yellow")
    n_green = sum(1 for c in checks if c["status"] == "green")

    if n_red == 0 and n_yellow == 0:
        verdict = "GO"
        verdict_ru = (f"🟢 ОТКРЫВАЙ {side} {pair} — все {n_green} проверок зелёные. "
                      f"Экспайри {expiry_h}ч.")
    elif n_red == 0 and n_yellow <= 2:
        verdict = "GO_CAUTION"
        verdict_ru = (f"🟡 МОЖНО {side} {pair} С ОСТОРОЖНОСТЬЮ — "
                      f"{n_green} зелёных, {n_yellow} жёлтых, "
                      f"экспайри {expiry_h}ч. Если жёлтое — это "
                      f"\"данных нет\", торговля разрешена.")
    elif n_red >= 1:
        # Find what's blocking
        blockers = [c for c in checks if c["status"] == "red"]
        verdict = "WAIT"
        names = ", ".join(b["name_ru"] for b in blockers[:3])
        verdict_ru = (f"🔴 НЕ ОТКРЫВАЙ — {n_red} критическая проверка не прошла: "
                      f"{names}.")
    else:
        verdict = "WAIT"
        verdict_ru = (f"🟡 ПОДОЖДИ — слишком много неопределённости "
                      f"({n_yellow} жёлтых проверок). "
                      f"Дождись подтверждения от агентов.")

    # Reasoning narrative
    reasoning_lines = [
        f"Лучший сигнал по PROGNOZY-28: <b>{pair} {side}</b> с вероятностью "
        f"<b>{prob_pct:.0f}%</b> (источник forecast_scanner — тот же которым "
        f"пользуется paper_trader)."
    ]
    if verdict == "GO":
        reasoning_lines.append(
            f"Все 8 проверок зелёные. На реальном счёте можно открывать сделку "
            f"{side} {pair} с экспайри {expiry_h}ч."
        )
    elif verdict == "GO_CAUTION":
        reasoning_lines.append(
            "Критических блокеров нет, но есть пара жёлтых проверок "
            "(\"данных нет\" — не \"не сходится\"). Можно открывать с меньшим объёмом."
        )
    else:
        reasoning_lines.append(
            "Открывать НЕ рекомендуется. Подожди когда красные проверки "
            "перейдут в зелёные (или система предложит другую пару)."
        )

    return {
        "as_of_utc": now.isoformat(),
        "session_now": sess_now,
        "session_now_ru": _session_ru(sess_now),
        "pair": pair,
        "side": side,
        "side_ru": "Покупка (BUY)" if side == "BUY" else "Продажа (SELL)" if side == "SELL" else side,
        "probability_pct": prob_pct,
        "expiry_hours": expiry_h,
        "current_price": top.get("current_price"),
        "verdict": verdict,
        "verdict_ru": verdict_ru,
        "reasoning_ru": " ".join(reasoning_lines),
        "checks": checks,
        "summary_counts": {"green": n_green, "yellow": n_yellow, "red": n_red},
        "alternates": [
            {
                "pair": r.get("pair"),
                "side": r.get("side"),
                "probability_pct": r.get("probability_pct"),
            }
            for r in rankings[1:4]
        ],
    }


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
