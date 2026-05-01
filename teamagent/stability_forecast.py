"""
Pre-emptive stability forecast — рассчитывает ожидаемый WR и доверительные
границы НА БУДУЩИЕ N часов **до того как** появятся закрытые сделки.

Это отвечает на вопрос пользователя:
  "Я хочу систему которая не зависит от сколько мы открыли сделок
   он заранее анализировал... заранее сказал что нам стоит ожидать."

Ключевое отличие от `stability_engine.py`:
  - stability_engine считает по фактическим closed_trades
    → нужна выборка (хотя бы 30+ сделок для жёсткой нижней границы)
  - stability_forecast считает по **strategy_config × market_hours
    × news_blackout × current session** — БЕЗ необходимости иметь сделки.
    Дает ожидаемый WR на следующие N часов прямо сейчас.

Все цифры — из реальных данных:
  - strategy_config_locked.json (365-day Yahoo backtest на каждой ячейке)
  - news.is_blackout (ForexFactory RSS)
  - market_hours (Sun22:00→Fri22:00 UTC)
  - state/forecasts.json (текущие 28 forecasts с probability_pct)

Ничего рандомного, ничего синтетического.
"""
from __future__ import annotations

import json
import logging
import math
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from . import config
from . import market_hours as mh
from . import stability_engine as se

log = logging.getLogger("stability_forecast")

_STATE_DIR = Path(__file__).resolve().parent / "state"
_STRATEGY_LOCKED = _STATE_DIR / "strategy_config_locked.json"
_STRATEGY_LIVE = _STATE_DIR / "strategy_config.json"
_FORECASTS = _STATE_DIR / "forecasts.json"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _load_strategy() -> dict:
    """Live strategy_config или locked baseline — что свежее и непустое."""
    for p in (_STRATEGY_LIVE, _STRATEGY_LOCKED):
        if p.exists():
            try:
                with p.open() as f:
                    d = json.load(f)
                if d and (d.get("pairs") or {}):
                    return d
            except Exception as e:
                log.warning(f"failed to load {p.name}: {e}")
    return {}


def _load_forecasts() -> dict:
    if not _FORECASTS.exists():
        return {}
    try:
        with _FORECASTS.open() as f:
            return json.load(f)
    except Exception:
        return {}


def _session_at(t: datetime) -> str:
    return mh.current_session(t)


def _qualified_cells(strategy: dict, min_wr: float = 70.0,
                     min_trades: int = 5) -> dict:
    """Вернёт {pair: {session: {wr, trades, pnl, variant}}} — только qualified."""
    out: dict = {}
    for pair, pdata in (strategy.get("pairs") or {}).items():
        by = pdata.get("by_session") or {}
        for sess, cell in by.items():
            if not cell:
                continue
            wr = cell.get("win_rate_pct", 0) or 0
            tr = cell.get("trades", 0) or 0
            if wr >= min_wr and tr >= min_trades:
                out.setdefault(pair, {})[sess] = {
                    "wr": float(wr),
                    "trades": int(tr),
                    "pnl_usd": float(cell.get("pnl_usd", 0) or 0),
                    "variant": cell.get("best_variant"),
                }
    return out


def _all_cells(strategy: dict) -> list[dict]:
    """Все ячейки с инфой для подсчёта baseline-WR (даже непрошедшие гейт)."""
    out: list[dict] = []
    for pair, pdata in (strategy.get("pairs") or {}).items():
        by = pdata.get("by_session") or {}
        for sess, cell in by.items():
            if not cell:
                continue
            wr = cell.get("win_rate_pct", 0) or 0
            tr = cell.get("trades", 0) or 0
            if tr <= 0:
                continue
            out.append({
                "pair": pair,
                "session": sess,
                "wr": float(wr),
                "trades": int(tr),
                "pnl_usd": float(cell.get("pnl_usd", 0) or 0),
                "qualifies": wr >= 70.0 and tr >= 5,
            })
    return out


def _expected_wr_for_session(session: str, strategy: dict,
                             min_wr: float = 70.0, min_trades: int = 5) -> dict:
    """Ожидаемый WR в данной сессии — взвешенное среднее по qualified ячейкам.

    Веса: число trades в backtest (более торгующая ячейка = больший вес).
    Если qualified ячеек нет — возвращает baseline-WR по всем ячейкам этой
    сессии (диагностика).
    """
    cells = _all_cells(strategy)
    sess_cells = [c for c in cells if c["session"] == session]
    qual = [c for c in sess_cells if c["qualifies"]]
    if qual:
        wt = sum(c["trades"] for c in qual)
        wr = sum(c["wr"] * c["trades"] for c in qual) / wt if wt else 0
        n = sum(c["trades"] for c in qual)
        return {
            "expected_wr_pct": round(wr, 2),
            "n_qualified_pairs": len(qual),
            "n_total_pairs": len(sess_cells),
            "backtest_trades_weight": int(n),
            "method": "qualified_weighted",
        }
    if sess_cells:
        wt = sum(c["trades"] for c in sess_cells)
        wr = sum(c["wr"] * c["trades"] for c in sess_cells) / wt if wt else 0
        return {
            "expected_wr_pct": round(wr, 2),
            "n_qualified_pairs": 0,
            "n_total_pairs": len(sess_cells),
            "backtest_trades_weight": int(sum(c["trades"] for c in sess_cells)),
            "method": "all_cells_baseline",
        }
    return {
        "expected_wr_pct": 50.0,
        "n_qualified_pairs": 0,
        "n_total_pairs": 0,
        "backtest_trades_weight": 0,
        "method": "no_data_fallback",
    }


def _forecasts_above_gate(forecasts: dict, min_prob: float = 70.0) -> list[dict]:
    """Текущий список прогнозов которые проходят 70%-гейт прямо сейчас."""
    out = []
    rankings = forecasts.get("rankings") or []
    for r in rankings:
        if (r.get("probability_pct") or 0) >= min_prob:
            out.append(r)
    return out


def forecast_window(hours_ahead: int = 24,
                    min_prob: float = 70.0) -> dict:
    """Прогноз стабильности на следующие N часов вперёд.

    Возвращает:
        as_of_utc, hours_ahead
        market_status: текущий market_status() snapshot
        sessions_in_window: [{session, hours_in_window, expected_wr_pct,...}]
        weighted_expected_wr_pct: средневзвешенный ожидаемый WR по часам
        wilson_lower_pct / wilson_upper_pct: 95% CI на основе суммарного
            backtest sample (trades_weight)
        active_qualified_pairs_count: сколько пар проходят гейт сейчас
        forecasts_eligible_now: сколько forecasts ≥ min_prob прямо сейчас
        readiness_score_0_100: оценка готовности системы (НЕ зависит от
            числа закрытых сделок — зависит только от качества стратегии,
            активной сессии, открытости рынка, наличия eligible forecasts)
        diagnosis_ru: список фактических наблюдений на русском
        recommendations_ru: список рекомендаций
    """
    now = _utcnow()
    end = now + timedelta(hours=hours_ahead)
    strategy = _load_strategy()
    forecasts = _load_forecasts()

    # Разбиение [now, end] по часам и сессиям. Считаем сколько часов в каждой
    # сессии попадёт в окно — так получим взвешенный ожидаемый WR.
    hours_per_session: dict = {"Asia": 0.0, "London": 0.0,
                                "Overlap": 0.0, "NY": 0.0, "Closed": 0.0}
    cur = now
    step_h = 0.25  # 15-min шаг
    while cur < end:
        nxt = min(cur + timedelta(hours=step_h), end)
        sess = _session_at(cur)
        hours_per_session[sess] = hours_per_session.get(sess, 0.0) + (nxt - cur).total_seconds() / 3600.0
        cur = nxt

    sessions_data = []
    total_weighted_wr = 0.0
    total_weighted_trades = 0
    total_active_hours = 0.0
    for sess in ("Asia", "London", "Overlap", "NY"):
        h = hours_per_session.get(sess, 0.0)
        if h <= 0:
            continue
        wr_info = _expected_wr_for_session(sess, strategy)
        sessions_data.append({
            "session": sess,
            "hours_in_window": round(h, 2),
            **wr_info,
        })
        total_weighted_wr += wr_info["expected_wr_pct"] * h
        total_weighted_trades += wr_info["backtest_trades_weight"]
        total_active_hours += h

    closed_hours = hours_per_session.get("Closed", 0.0)

    if total_active_hours > 0:
        weighted_wr = total_weighted_wr / total_active_hours
    else:
        weighted_wr = 0.0

    # Wilson CI на основе суммарного backtest trades — это даёт нам *honest*
    # confidence интервал на ожидаемом WR не из закрытых сделок, а из
    # размера обучающей выборки.
    if total_weighted_trades > 0:
        wins_eq = int(round(weighted_wr / 100.0 * total_weighted_trades))
        wlo, wup = se.wilson_lower_upper(wins_eq, total_weighted_trades, 0.95)
    else:
        wlo, wup = 0.0, 1.0

    # Текущие forecasts ≥ 70% — это «реальный pipe» прямо сейчас
    eligible = _forecasts_above_gate(forecasts, min_prob)
    qualified = _qualified_cells(strategy)

    # Readiness score (0..100) — независим от числа закрытых сделок
    readiness = _compute_readiness(
        market_open=mh.is_market_open(now),
        weighted_wr_pct=weighted_wr,
        active_hours_in_window=total_active_hours,
        closed_hours_in_window=closed_hours,
        eligible_forecasts_now=len(eligible),
        qualified_pairs=len(qualified),
        wilson_lower_pct=wlo * 100.0,
    )

    # Диагноз на русском
    diag = _build_diagnosis_ru(
        weighted_wr=weighted_wr,
        wilson_lower=wlo * 100.0,
        wilson_upper=wup * 100.0,
        sessions=sessions_data,
        closed_hours=closed_hours,
        eligible_now=len(eligible),
        qualified=qualified,
        market_open=mh.is_market_open(now),
        readiness=readiness,
    )
    recos = _build_recommendations_ru(
        weighted_wr=weighted_wr,
        wilson_lower=wlo * 100.0,
        readiness=readiness,
        eligible_now=len(eligible),
        market_open=mh.is_market_open(now),
        closed_hours=closed_hours,
    )

    return {
        "as_of_utc": now.isoformat(),
        "hours_ahead": hours_ahead,
        "market_status": mh.market_status(now),
        "sessions_in_window": sessions_data,
        "closed_hours_in_window": round(closed_hours, 2),
        "active_hours_in_window": round(total_active_hours, 2),
        "weighted_expected_wr_pct": round(weighted_wr, 2),
        "wilson_lower_pct_95": round(wlo * 100.0, 2),
        "wilson_upper_pct_95": round(wup * 100.0, 2),
        "backtest_trades_weight_total": int(total_weighted_trades),
        "active_qualified_pairs_count": len(qualified),
        "forecasts_eligible_now": len(eligible),
        "readiness_score_0_100": readiness,
        "verdict": _verdict_for_score(readiness),
        "diagnosis_ru": diag,
        "recommendations_ru": recos,
    }


def _compute_readiness(market_open: bool, weighted_wr_pct: float,
                       active_hours_in_window: float,
                       closed_hours_in_window: float,
                       eligible_forecasts_now: int,
                       qualified_pairs: int,
                       wilson_lower_pct: float) -> float:
    """0..100 — pre-emptive «готовность» системы.

    Веса (всё нормировано к 0..1):
      0.30 — wilson_lower_pct (нижняя граница ожидаемого WR; 70 → 1.0, 50 → 0)
      0.25 — weighted_wr_pct (ожидаемый WR; 75 → 1.0, 55 → 0)
      0.20 — qualified_pairs / 28
      0.10 — eligible_forecasts_now / 5  (5+ = 1.0)
      0.10 — active_hours / total_window_hours (рынок реально работает)
      0.05 — market_open (булевый штраф если закрыт)
    """
    total = active_hours_in_window + closed_hours_in_window
    active_ratio = (active_hours_in_window / total) if total > 0 else 0.0

    def n(x: float, lo: float, hi: float) -> float:
        if hi == lo:
            return 0.0
        return max(0.0, min(1.0, (x - lo) / (hi - lo)))

    parts = {
        "wilson_lower":  0.30 * n(wilson_lower_pct, 50, 70),
        "weighted_wr":   0.25 * n(weighted_wr_pct, 55, 75),
        "qualified":     0.20 * n(qualified_pairs, 0, 28),
        "eligible":      0.10 * n(eligible_forecasts_now, 0, 5),
        "active_ratio":  0.10 * active_ratio,
        "market_open":   0.05 * (1.0 if market_open else 0.0),
    }
    return round(sum(parts.values()) * 100.0, 2)


def _verdict_for_score(score: float) -> dict:
    if score >= 75:
        return {"emoji": "🟢", "color": "green",
                "text_ru": "ГОТОВ — ожидаем стабильную работу"}
    if score >= 55:
        return {"emoji": "🟡", "color": "yellow",
                "text_ru": "СРЕДНЯЯ ГОТОВНОСТЬ — есть слабые места"}
    if score >= 35:
        return {"emoji": "🟠", "color": "orange",
                "text_ru": "СЛАБАЯ ГОТОВНОСТЬ — нестабильное окно"}
    return {"emoji": "🔴", "color": "red",
            "text_ru": "НЕ ГОТОВ — ожидаем низкий WR в этом окне"}


def _build_diagnosis_ru(weighted_wr: float, wilson_lower: float,
                        wilson_upper: float, sessions: list,
                        closed_hours: float, eligible_now: int,
                        qualified: dict, market_open: bool,
                        readiness: float) -> list[str]:
    lines = []
    lines.append(
        f"Ожидаемый WR в окне (взвешенно по часам каждой сессии): "
        f"{weighted_wr:.1f}%, 95% CI: [{wilson_lower:.1f}%; {wilson_upper:.1f}%]."
    )
    if not market_open:
        lines.append("🔴 Рынок Forex сейчас ЗАКРЫТ — новые сделки не открываются.")
    if closed_hours > 0:
        lines.append(
            f"⏸ В этом окне будет {closed_hours:.1f}ч когда рынок закрыт "
            f"(выходные/гэп) — система будет ждать."
        )
    lines.append(
        f"Активных qualified пар (≥70% WR на 365д back-test): "
        f"{len(qualified)}/28."
    )
    lines.append(
        f"Текущих forecasts проходящих 70%-гейт прямо сейчас: {eligible_now}."
    )
    if sessions:
        best = max(sessions, key=lambda s: s["expected_wr_pct"])
        worst = min(sessions, key=lambda s: s["expected_wr_pct"])
        lines.append(
            f"Лучшая сессия в этом окне — {best['session']} "
            f"(ожид. WR {best['expected_wr_pct']:.1f}%, "
            f"{best['hours_in_window']:.1f}ч). "
            f"Худшая — {worst['session']} "
            f"(ожид. WR {worst['expected_wr_pct']:.1f}%, "
            f"{worst['hours_in_window']:.1f}ч)."
        )
    return lines


def _build_recommendations_ru(weighted_wr: float, wilson_lower: float,
                              readiness: float, eligible_now: int,
                              market_open: bool, closed_hours: float) -> list[str]:
    rec = []
    if not market_open:
        rec.append("Рынок закрыт — ничего не предпринимать. Дождись открытия.")
        return rec
    if wilson_lower < 55:
        rec.append(
            "Нижняя граница ожидаемого WR <55% — лучше переждать это окно "
            "или резко уменьшить stake (martingale 1×)."
        )
    if eligible_now == 0:
        rec.append(
            "Сейчас нет forecasts проходящих 70%-гейт — система автоматически "
            "будет ждать."
        )
    if 55 <= weighted_wr < 65:
        rec.append(
            "Ожидаемый WR около break-even — торгуй только qualified пары "
            "и снижай stake до минимума."
        )
    if weighted_wr >= 70 and wilson_lower >= 65:
        rec.append("Окно благоприятное — стандартная торговля разрешена.")
    if closed_hours > 6:
        rec.append(
            f"В окне {closed_hours:.1f}ч закрытого рынка — будет пауза. "
            "Это нормально для выходных."
        )
    if readiness < 35:
        rec.append(
            "Готовность <35% — рекомендую перезапустить strategy_search "
            "(`python -m teamagent.strategy_search`) или ждать пока локальная "
            "сессия даст более активные ячейки."
        )
    if not rec:
        rec.append("Окно стабильное, продолжай обычную работу.")
    return rec
