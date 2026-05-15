"""The Top-1 decision engine — the new AI brain.

Stitches together six analytical layers and picks the single best pair
out of the 28 the system tracks, with full explainable reasoning.

Layers (weights):

    technical          (35%)   indicators + SMC + Wyckoff + VP
    macro              (25%)   DXY / yield / commodity proxies
    fundamental_bias   (15%)   carry differential, central-bank stance
    news_veto          (10%)   high-impact events in next 2h → veto
    sentiment          (10%)   risk-on/risk-off pulse (DXY/VIX/Gold)
    political_risk      (5%)   geopolitical heat penalises pairs

Hard veto rules (any one fires → pair eliminated from Top-1):

    - Multi-timeframe not aligned (D1+H4+H1+M15 all in one direction)
    - ADX H1 < 20  → no real trend, just chop
    - High-impact news within 120 minutes for base OR quote
    - Market is closed (weekend gap) for the pair's session

The output JSON is consumed by the static site (site/index.html) and
by the Telegram bot.  Every numeric field is calibrated for human
readability so a trader can audit the AI's reasoning in seconds.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from .analyzer import analyze_pair
from .big_players import (
    big_player_scores,
    pair_big_player_score,
)
from .config import PAIRS, PAIR_NAMES_RU, SESSIONS, detect_session
from .cot import cot_currency_zscores
from .indicators import atr as atr_series  # noqa: F401  (re-export via compute_all)
from .macro import (
    CURRENCIES,
    currency_strength_from_macro,
    fetch_macro_snapshot,
    pair_macro_score,
)
from .news_brain import next_high_impact_events, political_risk_scores
from .prices import fetch_bars, get_current_price
from .safety import (
    five_hour_projection,
    m5_momentum_aligned,
    reversal_risk_h1,
    weekly_bias,
)
from .smc import smc_score
from .volume_profile import volume_profile
from .wyckoff import wyckoff_phase


log = logging.getLogger("brain")


# Layer weights — must sum to 1.0.  ``big_players`` carries 12 % and
# the other layers were each trimmed by 1-3 % to make room.  The user
# explicitly asked for a system that goes "inside the market" and
# knows where the institutional money sits, so we give that layer a
# meaningful but non-dominant slice of the score.
WEIGHTS = {
    "technical": 0.33,
    "macro": 0.22,
    "big_players": 0.12,
    "fundamental": 0.13,
    "news": 0.09,
    "sentiment": 0.08,
    "political": 0.03,
}

NEWS_VETO_MINUTES = 120
MIN_ADX_H1 = 20

# Top-1 must lead Top-2 by at least this many confidence points, *or*
# have a confidence ≥ ``CLEAR_FAVORITE_FLOOR``.  Otherwise the cycle
# refuses to publish a Top-1 because there is no "явный фаворит".
CLEAR_FAVORITE_LEAD = 5
CLEAR_FAVORITE_FLOOR = 80


# Carry: which currency in each pair is "high-yielder" by historical
# central-bank stance.  Static — refreshed manually when policy regimes
# clearly shift.  Real values pulled from official rate decisions, no
# simulators.  Range = annualised policy rate in percent.
POLICY_RATES_PCT = {
    "USD": 4.50,
    "EUR": 3.25,
    "GBP": 4.75,
    "JPY": 0.50,
    "CHF": 1.00,
    "AUD": 4.10,
    "CAD": 3.25,
    "NZD": 4.25,
}


def _fundamental_bias(pair: str) -> dict:
    """Carry & central-bank-stance score in [-3, +3]."""
    base, quote = pair[:3], pair[3:]
    if base not in POLICY_RATES_PCT or quote not in POLICY_RATES_PCT:
        return {"score": 0, "reason": "Ставка ЦБ неизвестна"}
    spread = POLICY_RATES_PCT[base] - POLICY_RATES_PCT[quote]
    if spread >= 2.0:
        return {
            "score": +3,
            "reason": f"Carry: {base} +{spread:.2f}% vs {quote} — сильный плюс",
        }
    if spread >= 0.5:
        return {
            "score": +1,
            "reason": f"Carry: {base} +{spread:.2f}% vs {quote}",
        }
    if spread <= -2.0:
        return {
            "score": -3,
            "reason": f"Carry: {base} {spread:.2f}% vs {quote} — сильный минус",
        }
    if spread <= -0.5:
        return {
            "score": -1,
            "reason": f"Carry: {base} {spread:.2f}% vs {quote}",
        }
    return {
        "score": 0,
        "reason": f"Carry: {base} {spread:+.2f}% vs {quote} — нейтрально",
    }


def _sentiment_score(macro: dict) -> dict:
    """Risk-on/risk-off pulse derived from DXY/Gold/VIX moves."""
    if not macro:
        return {"score": 0, "reason": "Нет sentiment-данных"}
    dxy = macro.get("DXY", 0.0)
    gold = macro.get("GOLD", 0.0)
    vix = macro.get("VIX", 0.0)

    # Falling DXY + rising stocks + falling VIX = risk-on (good for
    # AUD/NZD/CAD, bad for JPY/CHF/USD safe-havens).
    if dxy < -0.3 and vix < -2:
        return {"score": +1, "reason": "Глобальный risk-on (DXY↓, VIX↓)"}
    if dxy > 0.3 and vix > 2:
        return {"score": -1, "reason": "Глобальный risk-off (DXY↑, VIX↑)"}
    if gold > 1.0:
        return {"score": 0, "reason": "Золото растёт — нейтральный фон"}
    return {"score": 0, "reason": "Sentiment без выраженного перевеса"}


def _apply_sentiment_to_pair(pair: str, sentiment: dict) -> int:
    base, quote = pair[:3], pair[3:]
    safe = {"JPY", "CHF", "USD"}
    risk = {"AUD", "NZD", "CAD"}
    s = sentiment["score"]
    if s == 0:
        return 0
    if base in risk and quote in safe:
        return +1 if s > 0 else -1
    if base in safe and quote in risk:
        return -1 if s > 0 else +1
    return 0


def _political_pair_penalty(pair: str, risk_scores: dict[str, int]) -> dict:
    base, quote = pair[:3], pair[3:]
    risk = max(risk_scores.get(base, 0), risk_scores.get(quote, 0))
    if risk >= 2:
        return {"score": -risk, "reason": f"Геополитика: повышенный риск ({base}/{quote})"}
    return {"score": 0, "reason": "Геополитика спокойна"}


def _veto_check(
    pair: str,
    ta: Optional[dict],
    news_minutes: dict[str, int],
) -> Optional[str]:
    """Return a veto reason string or None if the pair is allowed."""
    base, quote = pair[:3], pair[3:]
    if ta is None:
        return "Нет данных для анализа"
    if not ta.get("multi_tf_aligned"):
        return "Multi-TF не выровнен — нет согласованного тренда"
    adx_h1 = ta.get("adx_h1", 0)
    if adx_h1 < MIN_ADX_H1:
        return f"ADX H1 = {adx_h1:.0f} (<{MIN_ADX_H1}) — рынок без тренда"
    next_event_base = news_minutes.get(base, 9999)
    next_event_quote = news_minutes.get(quote, 9999)
    if min(next_event_base, next_event_quote) < NEWS_VETO_MINUTES:
        return (
            "Высокоимпактная новость "
            f"({base} {next_event_base}м / {quote} {next_event_quote}м) — veto"
        )
    session = detect_session()
    if session == "Closed":
        return "Рынок закрыт (Asia/London/NY вне сессий)"
    return None


def _senior_alignment_check(pair: str, side: Optional[str]) -> dict:
    """Extra MTF guard the user asked for: W1 bias + M5 momentum.

    The analyser already aligns D1+H4+H1+M15.  This helper adds

    * ``weekly_aligned`` — W1 bias must not be against ``side``
    * ``m5_aligned``      — last 6 M5 closes must not move against ``side``

    Returns a dict with both flags plus a human-readable reason.
    Always returns *some* dict so the brain can include the layer in
    its breakdown even when ``side`` is None.
    """
    if side not in ("BUY", "SELL"):
        return {
            "weekly_aligned": None,
            "weekly_bias": None,
            "m5_aligned": None,
            "reason": "Стороны нет — проверка пропущена",
        }
    bars_w1 = fetch_bars(pair, "1wk", "2y")
    bars_m5 = fetch_bars(pair, "5m", "5d")
    w_bias = weekly_bias(bars_w1)
    w_ok = (w_bias is None) or (w_bias == side)
    m5_ok = m5_momentum_aligned(bars_m5, side)
    reasons: list[str] = []
    if w_bias is None:
        reasons.append("W1 нейтральна")
    else:
        reasons.append(f"W1 bias = {w_bias} ({'согласован' if w_ok else 'против'})")
    reasons.append(f"M5 momentum {'согласован' if m5_ok else 'против'}")
    return {
        "weekly_aligned": bool(w_ok),
        "weekly_bias": w_bias,
        "m5_aligned": bool(m5_ok),
        "reason": ", ".join(reasons),
    }


def _safety_layer(pair: str, side: Optional[str]) -> dict:
    """Reversal-risk check + 5-hour projection (last-moment safety).

    The user explicitly asked for the system to know the trade will be
    in profit at expiry — not just at entry.  We answer with two
    quantitative checks on H1 bars:

    * ``reversal``     — last few bars show a reversal pattern
    * ``projection``   — projected close in 5 H1 bars stays in profit

    Either one failing sets ``passes=False`` so ``_veto_check_full``
    can flip the pair to vetoed before it competes for Top-1.
    """
    if side not in ("BUY", "SELL"):
        return {
            "passes": True,
            "reversal": {"reversal": False, "reason": "Сторона не определена"},
            "projection": {
                "passes": True,
                "reason": "Сторона не определена",
            },
        }
    bars_h1 = fetch_bars(pair, "1h", "1mo")
    rev = reversal_risk_h1(bars_h1, side)
    proj = five_hour_projection(bars_h1, side)
    return {
        "passes": (not rev["reversal"]) and proj["passes"],
        "reversal": rev,
        "projection": proj,
    }


def _technical_extras(pair: str) -> dict:
    """SMC + Wyckoff + Volume Profile composite (one timeframe each)."""
    bars_h1 = fetch_bars(pair, "1h", "1mo")
    bars_h4 = fetch_bars(pair, "4h", "3mo")
    if bars_h1.empty or bars_h4.empty:
        return {
            "smc": {"score": 0, "reasons": []},
            "wyckoff": {"score": 0, "reason": "Нет данных"},
            "vp": {"score": 0, "reason": "Нет данных"},
            "score": 0,
        }
    smc = smc_score(bars_h4)
    wy = wyckoff_phase(bars_h4)
    vp = volume_profile(bars_h1)
    composite = smc["score"] + wy["score"] + vp["score"]
    composite = max(-9, min(9, composite))
    return {"smc": smc, "wyckoff": wy, "vp": vp, "score": composite}


def _normalised_technical(ta: dict, extras: dict) -> float:
    """Map analyzer score + extras to [-1, +1]."""
    if not ta:
        return 0.0
    base = ta.get("score", 0) / max(1, ta.get("max_score", 36)) * 2.0  # ≈ [-2, +2]
    extra = extras["score"] / 9.0  # [-1, +1]
    combined = 0.7 * base + 0.3 * extra
    return max(-1.0, min(1.0, combined))


def _norm(value: float, scale: float) -> float:
    return max(-1.0, min(1.0, value / scale))


def _atr_stop_take(pair: str, side: str) -> dict:
    """Compute ATR-based SL/TP levels for the trader.

    Stop = 1.5 × ATR(H1), Take = 2.5 × ATR(H1) → expected R:R ≈ 1.66.
    """
    bars = fetch_bars(pair, "1h", "1mo")
    if bars.empty or len(bars) < 20:
        return {}
    from .indicators import atr  # local import to keep top clean
    atr_val = float(atr(bars, period=14).iloc[-1])
    price = float(bars["Close"].iloc[-1])
    if side == "BUY":
        sl = round(price - 1.5 * atr_val, 5)
        tp = round(price + 2.5 * atr_val, 5)
    else:
        sl = round(price + 1.5 * atr_val, 5)
        tp = round(price - 2.5 * atr_val, 5)
    return {
        "entry": round(price, 5),
        "stop_loss": sl,
        "take_profit": tp,
        "atr_h1": round(atr_val, 5),
        "rr_target": 1.66,
    }


def _next_cycle_iso(now: datetime | None = None) -> str:
    """Return the next 5-hour cycle boundary in UTC, aligned to UTC+5 5h grid.

    Cycles fire at 00:00, 05:00, 10:00, 15:00, 20:00 UTC+5 — same as
    cycle_5h.yml.  We return the *next* such boundary strictly after now.
    """
    if now is None:
        now = datetime.now(timezone.utc)
    # UTC+5 boundary hours → UTC hours
    boundaries_utc_hours = [19, 0, 5, 10, 15]
    # Build a sorted list of upcoming boundaries within 24h.
    candidates = []
    for h in sorted(set(boundaries_utc_hours)):
        cand = now.replace(hour=h, minute=0, second=0, microsecond=0)
        if cand <= now:
            cand = cand.replace(day=cand.day + 1)
        candidates.append(cand)
    return min(candidates).isoformat()


def evaluate_pair(
    pair: str,
    macro_currency_scores: dict[str, float],
    macro_raw: dict,
    sentiment: dict,
    political: dict[str, int],
    news_minutes: dict[str, int],
    big_player_currency_scores: Optional[dict[str, float]] = None,
) -> dict:
    """Score one pair across all seven layers and return a transparent record."""
    ta = analyze_pair(pair)
    veto = _veto_check(pair, ta, news_minutes)

    extras = _technical_extras(pair)
    tech_norm = _normalised_technical(ta or {}, extras)

    macro = pair_macro_score(pair, macro_currency_scores)
    macro_norm = _norm(macro["score"], 3.0)

    fundamental = _fundamental_bias(pair)
    fund_norm = _norm(fundamental["score"], 3.0)

    news = {
        "score": 0 if veto is None else -1,
        "reason": "Веток новостей нет" if veto is None
        else (veto if "Высокоимпактная" in veto else "Новостной фон OK"),
        "next_event_base_min": news_minutes.get(pair[:3], 9999),
        "next_event_quote_min": news_minutes.get(pair[3:], 9999),
    }
    news_norm = float(news["score"])

    sent_pair_score = _apply_sentiment_to_pair(pair, sentiment)
    sentiment_layer = {
        "score": sent_pair_score,
        "reason": sentiment["reason"],
    }
    sent_norm = _norm(sent_pair_score, 1.0)

    political_layer = _political_pair_penalty(pair, political)
    pol_norm = _norm(political_layer["score"], 3.0)

    big_player = pair_big_player_score(pair, big_player_currency_scores or {})
    big_player_norm = _norm(big_player["score"], 3.0)

    # Direction comes from the technical analyzer; the other layers
    # *modulate confidence* in that direction.  We bake a sign agreement
    # check into the macro layer — if technical says BUY but macro
    # strongly disagrees, composite confidence drops.
    side = (ta or {}).get("side") or (
        "BUY" if tech_norm > 0 else "SELL" if tech_norm < 0 else None
    )

    senior = _senior_alignment_check(pair, side)
    safety = _safety_layer(pair, side)

    # Promote senior MTF / safety failures into the veto field so the
    # rest of the pipeline (filtering candidates, picking Top-1) sees a
    # uniformly-vetoed pair regardless of which guard fired.
    if veto is None and side is not None:
        if senior["weekly_aligned"] is False:
            veto = f"W1 bias {senior['weekly_bias']} против {side} — старший тренд не поддерживает"
        elif senior["m5_aligned"] is False:
            veto = "M5 momentum против — краткосрочный импульс мешает"
        elif safety["reversal"]["reversal"]:
            veto = safety["reversal"]["reason"]
        elif not safety["projection"]["passes"]:
            veto = f"5h-проекция: последний момент не в плюсе — {safety['projection']['reason']}"

    composite_raw = (
        WEIGHTS["technical"] * abs(tech_norm)
        + WEIGHTS["macro"] * (macro_norm if side == "BUY" else -macro_norm) * (1 if tech_norm >= 0 else -1)
        + WEIGHTS["big_players"] * (big_player_norm if side == "BUY" else -big_player_norm) * (1 if tech_norm >= 0 else -1)
        + WEIGHTS["fundamental"] * (fund_norm if side == "BUY" else -fund_norm) * (1 if tech_norm >= 0 else -1)
        + WEIGHTS["sentiment"] * sent_norm * (1 if tech_norm >= 0 else -1)
        + WEIGHTS["political"] * pol_norm
        + WEIGHTS["news"] * news_norm
    )
    # composite_raw is in roughly [-1, +1]; scale to a 0..100 confidence.
    confidence = int(round(max(0.0, min(1.0, abs(composite_raw))) * 100))

    return {
        "pair": pair,
        "name_ru": PAIR_NAMES_RU.get(pair, pair),
        "side": side,
        "veto": veto,
        "confidence": confidence,
        "composite_score": round(composite_raw, 4),
        "layers": {
            "technical": {
                "score": ta.get("score") if ta else 0,
                "max_score": ta.get("max_score") if ta else 0,
                "side": ta.get("side") if ta else None,
                "confidence": ta.get("confidence") if ta else 0,
                "adx_h1": ta.get("adx_h1") if ta else 0,
                "persistence_5h": ta.get("trend_persistence_5h") if ta else 0,
                "multi_tf_aligned": bool(ta.get("multi_tf_aligned")) if ta else False,
                "details": ta.get("details", []) if ta else [],
                "extras": extras,
                "normalised": round(tech_norm, 3),
            },
            "macro": {**macro, "normalised": round(macro_norm, 3)},
            "big_players": {**big_player, "normalised": round(big_player_norm, 3)},
            "fundamental": {**fundamental, "normalised": round(fund_norm, 3)},
            "news": news,
            "sentiment": {**sentiment_layer, "normalised": round(sent_norm, 3)},
            "political": {**political_layer, "normalised": round(pol_norm, 3)},
            "senior_alignment": senior,
            "safety_5h": safety,
        },
        "levels": (_atr_stop_take(pair, side) if side and veto is None else {}),
    }


def select_top1(now: datetime | None = None) -> dict:
    """Run the full 28-pair sweep and return the Top-1 forecast payload."""
    if now is None:
        now = datetime.now(timezone.utc)

    macro_raw = fetch_macro_snapshot()
    currency_scores = currency_strength_from_macro(macro_raw)
    sentiment = _sentiment_score(macro_raw)
    political = political_risk_scores()
    news_minutes = next_high_impact_events()

    # Smart Money / big-player composite — COT + bid-ask + macro flow.
    # Failures inside this layer never block the cycle; on error the
    # currency scores are zero and the brain treats the layer as silent.
    cot_scores = cot_currency_zscores()
    bp_snapshot = big_player_scores(cot_scores=cot_scores, macro_currency=currency_scores)
    big_player_currency = bp_snapshot["currency_scores"]

    all_evals: list[dict] = []
    for pair in PAIRS:
        try:
            ev = evaluate_pair(
                pair,
                currency_scores,
                macro_raw,
                sentiment,
                political,
                news_minutes,
                big_player_currency_scores=big_player_currency,
            )
        except Exception as e:
            log.exception(f"evaluate_pair failed {pair}: {e}")
            continue
        all_evals.append(ev)

    candidates = [e for e in all_evals if e["veto"] is None and e["side"]]
    candidates.sort(key=lambda e: e["confidence"], reverse=True)

    # Clear-favorite gate — the user explicitly asked the system to pick
    # ONE pair "where there is a clear favorite".  We publish a Top-1
    # only when the top candidate is unambiguously ahead.
    top1 = None
    favorite_reason: Optional[str] = None
    if candidates:
        winner = candidates[0]
        runner_up = candidates[1] if len(candidates) > 1 else None
        lead = winner["confidence"] - (runner_up["confidence"] if runner_up else 0)
        has_clear_lead = lead >= CLEAR_FAVORITE_LEAD
        has_floor = winner["confidence"] >= CLEAR_FAVORITE_FLOOR
        if has_clear_lead or has_floor:
            top1 = winner
            favorite_reason = (
                f"Явный фаворит: уверенность {winner['confidence']}% "
                f"(отрыв от Top-2 = {lead} п.)"
            )
        else:
            favorite_reason = (
                f"Нет явного фаворита: Top-1 {winner['confidence']}% / "
                f"Top-2 {runner_up['confidence'] if runner_up else 0}% — "
                f"отрыв {lead} < {CLEAR_FAVORITE_LEAD}"
            )

    return {
        "generated_at_utc": now.isoformat(),
        "next_cycle_utc": _next_cycle_iso(now),
        "macro": {
            "tickers": macro_raw,
            "currency_strength": currency_scores,
        },
        "sentiment": sentiment,
        "political_risk": political,
        "news_minutes": news_minutes,
        "big_players": bp_snapshot,
        "favorite_check": {
            "ok": top1 is not None,
            "reason": favorite_reason,
            "lead_required": CLEAR_FAVORITE_LEAD,
            "confidence_floor": CLEAR_FAVORITE_FLOOR,
        },
        "top1": top1,
        "top5": candidates[:5],
        "all_evals": all_evals,
    }
