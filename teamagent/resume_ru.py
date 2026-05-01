"""resume_ru — генератор русскоязычной общей оценки + прогноза.

Не использует LLM (бесплатно, локально, мгновенно). Логика — детерминированный
шаблон с подставлением реальных метрик. Всегда работает, всегда стабильно
(no random, no temperature).

Источники данных:
  - state/paper_stats.json (общая WR/PnL)
  - state/forecasts.json (28 текущих прогнозов)
  - state/strategy_config.json (per-pair quality)
  - stability_engine.system_stability_report() (сводный отчёт стабильности)

Блок "ПРОГНОЗ" — это не предсказание движения цены (запрещено в системе), а
прогноз стабильности самой системы: какой WR ожидается с 95% доверием, какой
worst-case PnL.
"""
from __future__ import annotations
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import config
from . import stability_engine as se

log = logging.getLogger("resume_ru")

STATE_DIR = config.STATE_DIR


def _load(path: Path, default):
    try:
        if not path.exists():
            return default
        return json.loads(path.read_text())
    except Exception:
        return default


def _verdict(score: float) -> tuple[str, str, str]:
    """(emoji, badge, color) для сводной оценки 0-100."""
    if score >= 80:
        return ("🟢", "ОТЛИЧНО", "green")
    if score >= 65:
        return ("🟢", "ХОРОШО", "green")
    if score >= 50:
        return ("🟡", "СТАБИЛЬНО", "yellow")
    if score >= 35:
        return ("🟠", "ОСТОРОЖНО", "orange")
    return ("🔴", "ВЫСОКИЙ РИСК", "red")


def _streak_text(streaks: dict) -> str:
    cur = streaks.get("current_streak", 0)
    kind = streaks.get("current_streak_kind", "—")
    if cur <= 0 or kind == "—":
        return "Серий пока нет."
    if kind == "WIN":
        return f"Сейчас идёт серия из {cur} побед подряд."
    return f"Сейчас идёт серия из {cur} убытков подряд (martingale активен)."


def general_assessment() -> dict[str, Any]:
    """Главная общая оценка системы на русском языке."""
    rep = se.system_stability_report()
    score = rep.get("stability_score_0_100", 0)
    emoji, badge, color = _verdict(score)

    wr_obs = rep.get("wilson_wr_lower_95", 0)
    wr_up = rep.get("wilson_wr_upper_95", 0)
    qualified = rep.get("qualified_cells_total", 0)
    longest_win = rep.get("longest_win_streak", 0)
    longest_loss = rep.get("longest_loss_streak", 0)
    sharpe = rep.get("sharpe_ratio", 0)
    pf = rep.get("profit_factor", 0)
    var95 = rep.get("var_95", 0)
    cvar95 = rep.get("cvar_95", 0)
    p5 = rep.get("bootstrap_pnl_p5", 0)
    p95 = rep.get("bootstrap_pnl_p95", 0)
    n = rep.get("n_closed_trades", 0)
    streaks = {
        "current_streak": rep.get("current_streak", 0),
        "current_streak_kind": rep.get("current_streak_kind", "—"),
    }

    # Заголовок (с пробелами между emoji и текстом — fix отрисовки в HTML)
    headline = f"{emoji}  Общая оценка системы: {badge} ({score}/100)"

    # Диагноз
    diag_lines = [
        f"📊  Закрытых сделок: {n}. Wilson 95% CI для WR: [{wr_obs}%; {wr_up}%].",
        f"🎯  Qualified-ячеек ≥70% WR за 365 дней: {qualified}/112.",
        f"📈  Sharpe={sharpe}, Profit Factor={pf}, P5/P95 PnL/сделку: [{p5}; {p95}] $.",
        f"⚠️  VaR 95%={var95} $, CVaR 95% (худший хвост)={cvar95} $.",
        f"🔁  Самая длинная серия побед: {longest_win}, убытков: {longest_loss}. {_streak_text(streaks)}",
    ]

    # Прогноз стабильности (НЕ движения цены)
    forecast_lines = []
    if n >= 10:
        forecast_lines.append(
            f"С 95%-вероятностью реальный WR в долгой перспективе будет в "
            f"диапазоне [{wr_obs}%; {wr_up}%] (Wilson)."
        )
    else:
        forecast_lines.append(
            f"Выборка {n} сделок — слишком мала для жёстких границ. "
            f"Дай системе хотя бы 30 сделок для надёжной нижней оценки."
        )
    mg = se.min_guarantee_per_trade(stake_usd=1.0, payout_pct=0.85)
    forecast_lines.append(
        f"Гарантированный (95% доверие) минимальный ожидаемый PnL/сделку: "
        f"{mg['expected_pnl_lower_per_trade']} $; средний: "
        f"{mg['expected_pnl_mean_per_trade']} $; верхняя оценка: "
        f"{mg['expected_pnl_upper_per_trade']} $."
    )
    forecast_lines.append(
        f"Минимум для безубыточности при payout 85%: "
        f"{rep.get('break_even_probability', 54.1)}% WR. С учётом slippage: "
        f"{rep.get('slippage_threshold_probability', 54.1)}%."
    )

    # Рекомендации
    rec = []
    if score < 50:
        rec.append("⚠️ Снизить стейк или поставить пары с низким WR на паузу.")
    if longest_loss >= 5:
        rec.append("⚠️ Длинная серия убытков — пересчитать strategy_search sweep.")
    if qualified < 10:
        rec.append("⚠️ Мало qualified ячеек — сейчас система торгует только на проверенных.")
    if sharpe < 0:
        rec.append("⚠️ Отрицательный Sharpe — стратегия проигрывает risk-free.")
    if not rec:
        rec.append("✅ Система в норме, продолжаем работу по STRICT-gate (≥70% WR).")

    return {
        "as_of": datetime.now(timezone.utc).isoformat(),
        "score_0_100": score,
        "verdict": badge,
        "color": color,
        "emoji": emoji,
        "headline": headline,
        "diagnosis": diag_lines,
        "forecast": forecast_lines,
        "recommendations": rec,
        "raw": rep,
    }


def per_pair_summary(pair: str) -> dict[str, Any]:
    """Per-pair краткая оценка."""
    ps = se.pair_stability_score(pair)
    cb = se.conformal_price_band(pair, 4, 0.90, 90)
    rv = se.realized_volatility(pair, 30)
    stress = se.stress_test_pair(pair, 365)
    score = ps.score_0_100
    emoji, badge, color = _verdict(score)

    forecasts = _load(STATE_DIR / "forecasts.json", {"forecasts": {}})
    f = forecasts.get("forecasts", {}).get(pair, {})
    side = f.get("side", "—")
    prob = f.get("probability_pct", 0)

    text_lines = [
        f"{emoji} {pair}: {badge} (стабильность {score}/100)",
        f"Текущий прогноз: {side} {prob}% (источник: PROGNOZY-28).",
    ]
    if cb:
        spread_pct = (cb["upper"] - cb["lower"]) / cb["spot"] * 100
        text_lines.append(
            f"90%-коридор цены через 4 ч: [{cb['lower']:.5f}; {cb['upper']:.5f}] "
            f"(±{spread_pct/2:.2f}%)."
        )
    if rv:
        text_lines.append(f"Волатильность 30д: σ={rv.get('rv_daily', 0)*100:.2f}%/день.")
    if stress:
        text_lines.append(
            f"Худшая неделя за 365д: {stress.get('worst_week_pct', 0):.2f}%, "
            f"лучшая: {stress.get('best_week_pct', 0):.2f}%."
        )

    return {
        "pair": pair,
        "score_0_100": score,
        "verdict": badge,
        "emoji": emoji,
        "color": color,
        "text_lines": text_lines,
        "components": ps.components,
        "guarantees": ps.guarantees,
        "notes": ps.notes,
        "conformal_band": cb,
        "realized_vol": rv,
        "stress_test": stress,
    }
