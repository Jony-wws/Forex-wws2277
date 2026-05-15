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
from .config import PAIRS, PAIR_NAMES_RU, SESSIONS, detect_session
from .indicators import atr as atr_series  # noqa: F401  (re-export via compute_all)
from .macro import (
    CURRENCIES,
    currency_strength_from_macro,
    fetch_macro_snapshot,
    pair_macro_score,
)
from .news_brain import next_high_impact_events, political_risk_scores
from .prices import fetch_bars, get_current_price
from .smc import smc_score
from .volume_profile import volume_profile
from .wyckoff import wyckoff_phase


log = logging.getLogger("brain")


WEIGHTS = {
    "technical": 0.35,
    "macro": 0.25,
    "fundamental": 0.15,
    "news": 0.10,
    "sentiment": 0.10,
    "political": 0.05,
}

NEWS_VETO_MINUTES = 120
MIN_ADX_H1 = 20


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
) -> dict:
    """Score one pair across all six layers and return a transparent record."""
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

    # Direction comes from the technical analyzer; the other layers
    # *modulate confidence* in that direction.  We bake a sign agreement
    # check into the macro layer — if technical says BUY but macro
    # strongly disagrees, composite confidence drops.
    side = (ta or {}).get("side") or (
        "BUY" if tech_norm > 0 else "SELL" if tech_norm < 0 else None
    )

    composite_raw = (
        WEIGHTS["technical"] * abs(tech_norm)
        + WEIGHTS["macro"] * (macro_norm if side == "BUY" else -macro_norm) * (1 if tech_norm >= 0 else -1)
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
            "fundamental": {**fundamental, "normalised": round(fund_norm, 3)},
            "news": news,
            "sentiment": {**sentiment_layer, "normalised": round(sent_norm, 3)},
            "political": {**political_layer, "normalised": round(pol_norm, 3)},
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
            )
        except Exception as e:
            log.exception(f"evaluate_pair failed {pair}: {e}")
            continue
        all_evals.append(ev)

    candidates = [e for e in all_evals if e["veto"] is None and e["side"]]
    candidates.sort(key=lambda e: e["confidence"], reverse=True)

    top1 = candidates[0] if candidates else None
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
        "top1": top1,
        "top5": candidates[:5],
        "all_evals": all_evals,
    }
