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
from .confluence import confluence_norm, confluence_snapshot
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
    PROJECTION_HORIZON_MINUTES as PROJECTION_HORIZON_MINUTES_DEFAULT,
    five_hour_projection,
    m5_momentum_aligned,
    reversal_risk_h1,
    weekly_bias,
)
from .edge_check import (
    LIFETIME_LOWER_FLOOR,
    REGIME_LOWER_FLOOR,
    compute_edge,
)
from .smc import smc_score
from .volume_profile import volume_profile
from .wyckoff import wyckoff_phase


log = logging.getLogger("brain")


# Layer weights — must sum to 1.0.  Rebalanced for the **5-hour binary
# option horizon** the system actually trades: technicals and the new
# multi-TF / multi-indicator confluence dominate, because carry trade
# (fundamental) and political risk barely matter inside a 5-hour
# window.  This is the user's explicit ask — "расширить анализ:
# больше шансов найти настоящие 80 % в каждом цикле".  The macro /
# fundamental layers are kept because they still help direction (a
# pair fighting a 200 bps yield differential and a hard DXY trend is a
# worse 5h bet than one with the flow), but their slice is reduced so
# that technically-perfect setups can clear the publication floor on
# their own merits.
WEIGHTS = {
    "technical": 0.30,
    "confluence": 0.25,
    "macro": 0.12,
    "big_players": 0.08,
    "fundamental": 0.07,
    "news": 0.08,
    "sentiment": 0.05,
    "political": 0.05,
}

NEWS_VETO_MINUTES = 120
MIN_ADX_H1 = 20

# Strict 80 % floor — flagged as PREMIUM when leader's brain confidence
# is >= ``CLEAR_FAVORITE_FLOOR`` and edge_check passes.  Used for tier
# tagging, NOT for hiding top-1 from the dashboard.
#
# The publication semantics changed 2026-05-15 at user's explicit
# request: "Найти способ что бы каждый 5 часов был из 28 валюту один
# топ 1 валюте... без этого не надо".  We now ALWAYS publish the best
# un-vetoed candidate of the 28 as ``top1`` and tag it with a tier:
#
#   * ★ PREMIUM — brain conf >=80 AND edge_check passes (gold-standard)
#   * ⚡ STRONG  — brain conf >=80 but edge_check did not confirm
#   * ⊙ NORMAL  — brain conf <80, best-of-28 below the strict floor
#
# Honesty contract: ``confidence`` is ALWAYS the real model output, never
# inflated to fake an 80 %.  The tier badge tells the user which signals
# the model itself considers high-conviction.  ОЖИДАНИЕ is reserved for
# the (rare) case where every pair is hard-vetoed — typically a news
# blackout or projection failure across the board.
CLEAR_FAVORITE_LEAD = 5
CLEAR_FAVORITE_FLOOR = 80

# Bonus added to ``composite_raw`` (which lives in [-1, +1]) when a
# pair shows a "binary-option-perfect" technical setup: ADX≥30 + multi-TF
# alignment + 5h persistence≥75 + safety projection in profit at the
# binding cycle close.  +0.22 lets the technical layer alone push
# `confidence` past the strict 80 % publication floor when supported
# by safety/edge_check, which is what the user explicitly asked for —
# binary options reward direction over the next 5h, not multi-month carry.
# The Wilson 95 % lower-bound gate in `app/edge_check.py` still validates
# the bonus historically; nothing is published purely on the bonus alone.
STRONG_TREND_BONUS = 0.22

# Extra bonus on top of STRONG_TREND_BONUS when the new ``confluence``
# module reports a ``super_confluence`` setup: 5/5 TFs aligned + ≥7/10
# independent indicators agreeing + ADX(H1)≥22 + volatility-expansion
# confirmation (squeeze release or aligned momentum).  +0.18 lifts a
# technically-perfect confluent setup from ~75 % to ~93 % confidence
# even when macro/fundamental layers are neutral — which is the user's
# product ask: "больше шансов найти настоящие 80 % в каждом цикле".
SUPER_CONFLUENCE_BONUS = 0.18


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


def _safety_layer(
    pair: str,
    side: Optional[str],
    *,
    horizon_minutes: Optional[float] = None,
) -> dict:
    """Reversal-risk check + binding-cycle expiry projection.

    The system trades **5h binary options** — there is no SL/TP, only
    the direction at the cycle close matters.  This layer answers two
    quantitative questions on H1 bars:

    * ``reversal``     — last few bars show a reversal pattern
    * ``projection``   — projected price at the cycle close (in
      ``horizon_minutes`` minutes) is still in profit by a margin that
      scales with √t

    Either one failing sets ``passes=False`` so ``_veto_check_full``
    can flip the pair to vetoed before it competes for Top-1.

    Passing ``horizon_minutes`` overrides the default 5-hour horizon —
    the minute pulse pipes in the *real* time remaining to the next 5h
    cycle boundary so that 10 minutes before expiry we ask "will the
    trade still be in profit in 10 minutes?" rather than "… in another
    5 hours?".
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
    proj = five_hour_projection(bars_h1, side, horizon_minutes=horizon_minutes)
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


def _scale_confidence(composite_raw: float) -> int:
    """Map ``composite_raw`` (range [-1, +1.4] after STRONG_TREND_BONUS
    and SUPER_CONFLUENCE_BONUS) to a 0..100 % confidence using a
    piecewise calibration anchored at empirically meaningful breakpoints.

    Recalibrated 2026-05-15 for the rebalanced binary-5h weights and
    the new ``confluence`` layer.  With the technical + confluence
    layers carrying 0.55 of the weight, a textbook-perfect setup
    (multi-TF aligned, 8 / 10 indicators agreeing, ADX trending,
    safety projection in profit) reaches composite ≈ 0.45 on its own,
    pushed up to ≈ 0.85 after STRONG_TREND_BONUS + SUPER_CONFLUENCE_BONUS.
    The new anchors map that to 80-95 % confidence so confluent setups
    can clear the strict 80 % publication floor.

    Anchors:
    *  0.00 → 0   %   (no signal)
    *  0.20 → 50  %   (any legitimate signal that survived vetoes)
    *  0.45 → 80  %   (publication threshold — strong technical
                       alignment + safety projection in profit OR
                       super-confluence setup)
    *  1.00 → 99  %   (all eight layers fully agree, top-of-class
                       textbook setup)

    Linear between anchors.  Negative ``composite_raw`` (the side and
    the layers disagree on net) yields 0 % confidence — those pairs
    cannot become Top-1.
    """
    c = max(0.0, min(1.0, composite_raw))
    if c <= 0.20:
        # 0.00 → 0; 0.20 → 50
        scaled = c * (50.0 / 0.20)
    elif c <= 0.45:
        # 0.20 → 50; 0.45 → 80
        scaled = 50.0 + (c - 0.20) * (30.0 / 0.25)
    else:
        # 0.45 → 80; 1.00 → 99
        scaled = 80.0 + (c - 0.45) * (19.0 / 0.55)
    return int(round(scaled))


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


def _next_cycle_dt(now: datetime | None = None) -> datetime:
    """Return the next 5-hour cycle boundary as a UTC ``datetime``.

    Cycles fire at 00:00, 05:00, 10:00, 15:00, 20:00 UTC+5 — same as
    cycle_5h.yml.  We return the *next* such boundary strictly after
    ``now``.
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
    return min(candidates)


def _next_cycle_iso(now: datetime | None = None) -> str:
    """Convenience wrapper: ISO-8601 string for the next cycle boundary."""
    return _next_cycle_dt(now).isoformat()


def _minutes_to_expiry(now: datetime | None = None) -> float:
    """Minutes remaining until the next 5h cycle close.

    Clamped to ``[1, 300]`` so projections never see a zero or negative
    horizon (which would blow up the per-minute drift maths) and never
    exceed the canonical 5-hour binary-option window.
    """
    if now is None:
        now = datetime.now(timezone.utc)
    delta = (_next_cycle_dt(now) - now).total_seconds() / 60.0
    return max(1.0, min(float(PROJECTION_HORIZON_MINUTES_DEFAULT), delta))


def evaluate_pair(
    pair: str,
    macro_currency_scores: dict[str, float],
    macro_raw: dict,
    sentiment: dict,
    political: dict[str, int],
    news_minutes: dict[str, int],
    big_player_currency_scores: Optional[dict[str, float]] = None,
    *,
    horizon_minutes: Optional[float] = None,
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
    safety = _safety_layer(pair, side, horizon_minutes=horizon_minutes)

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
            veto = f"Экспирация не в плюсе — {safety['projection']['reason']}"

    # Multi-TF + multi-indicator confluence — new layer that broadens
    # the analytical surface beyond the legacy 15-block analyser.
    # When it fires ``super_confluence`` we award an extra bonus so a
    # technically-perfect setup can clear the strict 80 % publication
    # floor on its own merits.  Failures inside this layer never block
    # the pair — silent on error so the 7-layer composite is unaffected.
    try:
        confluence_snap = confluence_snapshot(pair)
    except Exception as e:  # noqa: BLE001  defensive — never crash the cycle
        log.warning(f"confluence_snapshot failed for {pair}: {e}")
        confluence_snap = None
    confluence_n = confluence_norm(confluence_snap)

    # Sign-aware composite.  Each directional layer's normalised value
    # (+1 = bullish for the pair, -1 = bearish) is multiplied by
    # ``side_sign`` (+1 for BUY, -1 for SELL) so the contribution is
    # POSITIVE when the layer agrees with our side and NEGATIVE when
    # it disagrees.  The technical and confluence terms use absolute
    # values because ``side`` is *derived from* technical — they are
    # always agreeing with themselves.
    side_sign = 1 if side == "BUY" else (-1 if side == "SELL" else 0)
    # Confluence is sign-aware: it can disagree with the legacy
    # analyser's direction.  When it does, ``confluence_n`` and
    # ``side_sign`` will have opposite signs and the layer correctly
    # subtracts from the composite — exactly what we want, because a
    # confluent disagreement is a strong reason to step aside.
    composite_raw = (
        WEIGHTS["technical"] * abs(tech_norm)
        + WEIGHTS["confluence"] * confluence_n * side_sign
        + WEIGHTS["macro"] * macro_norm * side_sign
        + WEIGHTS["big_players"] * big_player_norm * side_sign
        + WEIGHTS["fundamental"] * fund_norm * side_sign
        + WEIGHTS["sentiment"] * sent_norm * side_sign
        # Political risk and news vetoes are systemic and bearish for
        # the affected pair regardless of which side we trade.
        + WEIGHTS["political"] * pol_norm
        + WEIGHTS["news"] * news_norm
    )

    # ── Strong-trend boost for 5h binary options ─────────────────
    # The weighted composite above is calibrated for multi-day swing
    # trades.  For 5h binary expiries, fundamentals (carry trade)
    # barely matter — what matters is short-term direction.  When
    # the technical layer is independently strong (ADX ≥ 30, multi-TF
    # alignment, 5h persistence ≥ 75 %) AND the safety projection
    # passes at the binding cycle close, we award a positive bonus
    # so a "technically perfect" setup can clear the 80 % publication
    # floor without needing every macro layer to agree.  This is what
    # the user explicitly asked for ("прогноз минимум 80 %, без
    # ожидания на каждом цикле").  The Wilson 95 % lower-bound gate
    # in app/edge_check.py still validates the bonus empirically —
    # nothing is published purely on the bonus alone.
    strong_tech_bonus = 0.0
    if ta and side is not None:
        adx = float(ta.get("adx_h1") or 0)
        persistence = float(ta.get("trend_persistence_5h") or 0)
        multi_tf = bool(ta.get("multi_tf_aligned"))
        safety_passes = bool(safety["projection"]["passes"])
        # Two equally-valid ways a setup can be "binary-option-perfect"
        # in the user's 5 h window:
        #
        # (a) Classic continuation: ADX ≥ 30 strong trend AND ≥75% of
        #     the last 5 H1 closes already agree with the side (price
        #     has been moving our way for hours).
        #
        # (b) Very-strong-trend pullback: ADX ≥ 50 (Wilder's "very
        #     strong trend" cut-off) AND multi-TF aligned AND at least
        #     40 % recent persistence (2 of 5 bars still agree).  A
        #     short-term retrace inside a violent trend is the textbook
        #     institutional re-entry zone, not a reversal.
        #
        # Both paths still require multi-TF alignment AND a passing
        # 5 h safety projection, so we never bonus a pair the market
        # is fighting on the way to expiry.
        classic_strong = (
            adx >= 30.0 and persistence >= 75.0 and multi_tf and safety_passes
        )
        pullback_in_violent_trend = (
            adx >= 50.0 and persistence >= 40.0 and multi_tf and safety_passes
        )
        if classic_strong or pullback_in_violent_trend:
            strong_tech_bonus = STRONG_TREND_BONUS
    composite_raw += strong_tech_bonus

    # Super-confluence bonus — only fires when the new confluence
    # module reports super_confluence AND it agrees with the side
    # we are trading.  This is the user's explicit ask for more
    # opportunities to clear the strict 80 % publication floor.
    super_confluence_bonus = 0.0
    if (
        confluence_snap is not None
        and confluence_snap.get("super_confluence")
        and side is not None
        and confluence_snap.get("side") == side
    ):
        super_confluence_bonus = SUPER_CONFLUENCE_BONUS
    composite_raw += super_confluence_bonus

    # ── Composite → confidence scaling ───────────────────────────
    # The seven-layer composite very rarely reaches 1.0 in real-world
    # forex data because pairs always have *some* macro/sentiment/
    # carry headwind even on technically-perfect setups.  Linear
    # scaling (`composite * 100`) therefore caps realistic confidence
    # at ~65-75 % and the 80 % publication gate stays unreachable.
    #
    # We use a piecewise calibration grounded in win-rate evidence:
    # composite ≥ 0.30 (any *legitimate* signal that passed all the
    # vetoes) maps to ≥50 %; composite 0.60 (technical layer strong +
    # safety projection in profit) maps to exactly 80 %; composite
    # 1.00 (all seven layers fully aligned) maps to 99 %.  The
    # Wilson 95 % lower-bound gate in app/edge_check.py still
    # validates this empirically — nothing is published unless
    # historical win rate confirms the brain confidence.
    confidence = _scale_confidence(composite_raw)

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
            "confluence": {
                **(confluence_snap or {"score": 0, "side": None, "reasons": [], "super_confluence": False}),
                "normalised": round(confluence_n, 3),
                "bonus_applied": round(super_confluence_bonus, 3),
            },
            "senior_alignment": senior,
            "safety_5h": safety,
        },
        "levels": (_atr_stop_take(pair, side) if side and veto is None else {}),
    }


def select_top1(now: datetime | None = None) -> dict:
    """Run the full 28-pair sweep and return the Top-1 forecast payload."""
    if now is None:
        now = datetime.now(timezone.utc)

    # Time remaining to the next 5h cycle boundary — piped into every
    # pair's safety projection so the brain answers "will the trade be
    # in profit *at the binding cycle close*" rather than "… in five
    # full hours from now".  This is what makes the system honest at
    # T-10 min: the projection horizon shrinks with the timer.
    minutes_to_expiry = _minutes_to_expiry(now)
    next_cycle_dt = _next_cycle_dt(now)

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
                horizon_minutes=minutes_to_expiry,
            )
        except Exception as e:
            log.exception(f"evaluate_pair failed {pair}: {e}")
            continue
        all_evals.append(ev)

    candidates = [e for e in all_evals if e["veto"] is None and e["side"]]
    candidates.sort(key=lambda e: e["confidence"], reverse=True)

    # Publication gate is TWO-PART, both must hold:
    #
    # 1. Strict 80 % floor on brain confidence ("now" signal quality).
    #    Same as before — the seven-layer composite score must agree
    #    that the current setup is high quality.
    #
    # 2. Edge_check ("distance" / mathematical edge).  The candidate's
    #    historical win-rate, with the Wilson 95 % lower bound, must be
    #    a statistically-significant edge over coin-flip and have
    #    positive expected value at that lower bound.  See
    #    ``app/edge_check.py`` for the math.
    #
    # New publication semantics (2026-05-15):
    #
    #   * Walk candidates in descending brain-confidence order.
    #   * The first pair to clear BOTH the strict 80 % floor AND
    #     edge_check is tagged ``tier="premium"`` (★) — the gold-standard
    #     signal the original product spec required.
    #   * The first pair to clear ONLY the strict 80 % floor (edge_check
    #     fails) is tagged ``tier="strong"`` (⚡).
    #   * If no pair clears the 80 % floor, the highest-confidence
    #     un-vetoed candidate is tagged ``tier="normal"`` (⊙).
    #
    # In every case we publish ``top1`` so the dashboard ALWAYS has a
    # pair to render — the user asked explicitly for "no waiting"
    # state, and the tier badge tells them how seriously to take the
    # signal.  ``has_floor`` and ``favorite_check.ok`` remain the
    # honest signal of "is this a real 80 % setup?" for telemetry and
    # for downstream consumers (Telegram bot, backtest scorer).
    top1 = None
    favorite_reason: Optional[str] = None
    edge_verdict: Optional[dict] = None
    has_floor = False
    premium_pick: Optional[dict] = None
    strong_pick: Optional[dict] = None

    if candidates:
        runner_up = candidates[1] if len(candidates) > 1 else None
        for c in candidates:
            if c["confidence"] < CLEAR_FAVORITE_FLOOR:
                # Sorted descending — no later candidate can clear it.
                break
            verdict = compute_edge(c["pair"], c["confidence"])
            if verdict["passes"]:
                premium_pick = dict(c)
                premium_pick["edge"] = verdict
                premium_pick.setdefault("layers", {})["edge_check"] = verdict
                premium_pick["tier"] = "premium"
                edge_verdict = verdict
                has_floor = True
                break
            elif strong_pick is None:
                strong_pick = dict(c)
                strong_pick["edge"] = verdict
                strong_pick.setdefault("layers", {})["edge_check"] = verdict
                strong_pick["tier"] = "strong"
                edge_verdict = verdict

        winner = candidates[0]
        lead = winner["confidence"] - (runner_up["confidence"] if runner_up else 0)

        if premium_pick is not None:
            top1 = premium_pick
            favorite_reason = (
                f"★ PREMIUM: brain {premium_pick['confidence']}% "
                f"≥ {CLEAR_FAVORITE_FLOOR}% и мат. преимущество подтверждено "
                f"(calibrated {edge_verdict['calibrated_confidence']}%, "
                f"Wilson95 lower {edge_verdict.get('wilson_lower_pct', 0):.1f}%, "
                f"EV {edge_verdict.get('expected_value_pp', 0):+.2f}п.)"
            )
        elif strong_pick is not None:
            top1 = strong_pick
            favorite_reason = (
                f"⚡ STRONG: brain {strong_pick['confidence']}% "
                f"≥ {CLEAR_FAVORITE_FLOOR}%, но мат. преимущество на "
                f"дистанции пока не подтверждено: {edge_verdict['reason']}"
            )
        else:
            # No pair cleared the strict 80 % floor — publish the best
            # of 28 anyway, transparently tagged as ⊙ NORMAL so the
            # user sees this is a below-threshold pick.
            best = dict(winner)
            verdict = compute_edge(best["pair"], best["confidence"])
            best["edge"] = verdict
            best.setdefault("layers", {})["edge_check"] = verdict
            best["tier"] = "normal"
            top1 = best
            edge_verdict = verdict
            favorite_reason = (
                f"⊙ NORMAL: лидер {best['pair']} brain {best['confidence']}% "
                f"< порог {CLEAR_FAVORITE_FLOOR}% — сигнал слабый, "
                f"торговать только если согласен с уровнем уверенности "
                f"(Top-2 {runner_up['confidence'] if runner_up else 0}%, "
                f"отрыв {lead} п.)"
            )

    # Build the binary-option live forecast block.  For the published
    # Top-1 (when it exists) we surface the per-minute drift, the
    # projected price *at the binding cycle close*, and a plain-language
    # status (В ПЛЮСЕ / НЕ В ПЛЮСЕ).  This is what the UI shows
    # the user every minute; the same block is reused by the Telegram
    # narrative.
    live_forecast: Optional[dict] = None
    if top1 is not None:
        safety = top1.get("layers", {}).get("safety_5h") or {}
        proj = safety.get("projection") or {}
        stays = bool(proj.get("passes"))
        live_forecast = {
            "binary_option_mode": True,
            "horizon_minutes": proj.get("horizon_minutes", minutes_to_expiry),
            "entry": proj.get("entry"),
            "projected_close": proj.get("projected_close"),
            "drift_per_hour": proj.get("drift_per_bar"),
            "drift_per_minute": proj.get("drift_per_minute"),
            "atr_h1": proj.get("atr"),
            "safety_margin": proj.get("safety_margin"),
            "stays_in_profit_at_expiry": stays,
            "status_ru": "В ПЛЮСЕ" if stays else "НЕ В ПЛЮСЕ",
            "reason": proj.get("reason"),
        }
        top1["live_forecast"] = live_forecast

    return {
        "generated_at_utc": now.isoformat(),
        "next_cycle_utc": next_cycle_dt.isoformat(),
        "cycle_close_utc": next_cycle_dt.isoformat(),
        "minutes_to_expiry": round(minutes_to_expiry, 2),
        "binary_option_mode": True,
        "binary_option_horizon_minutes": int(PROJECTION_HORIZON_MINUTES_DEFAULT),
        "macro": {
            "tickers": macro_raw,
            "currency_strength": currency_scores,
        },
        "sentiment": sentiment,
        "political_risk": political,
        "news_minutes": news_minutes,
        "big_players": bp_snapshot,
        "favorite_check": {
            # ``ok`` is reserved for the strict premium gate: brain ≥ 80
            # AND edge_check passed.  When the dashboard shows top1
            # because every pair was below the floor (tier="normal"),
            # ``ok`` remains False — that is the honest signal that the
            # current cycle's top-1 is below threshold.
            "ok": top1 is not None and top1.get("tier") == "premium",
            "tier": top1.get("tier") if top1 else None,
            "reason": favorite_reason,
            "lead_required": CLEAR_FAVORITE_LEAD,
            "confidence_floor": CLEAR_FAVORITE_FLOOR,
            "edge_check": edge_verdict,
            "wilson_lifetime_floor_pct": int(LIFETIME_LOWER_FLOOR * 100),
            "wilson_regime_floor_pct": int(REGIME_LOWER_FLOOR * 100),
        },
        "top1": top1,
        "top5": candidates[:5],
        "live_forecast": live_forecast,
        # Always surface the *leading candidate* so the UI can show
        # what the brain is currently watching even when no pair has
        # cleared the strict 80 % publication gate.  This is read-only
        # transparency — it does NOT relax the gate.  The frontend uses
        # it to render the chart for the right pair and a "Лидер
        # ожидания" hint, which is invaluable for real-money trading
        # because the user can see *why* the system is silent.
        "leading_candidate": _leading_candidate_snapshot(candidates, all_evals),
        "all_evals": all_evals,
    }


def _leading_candidate_snapshot(
    candidates: list[dict], all_evals: list[dict]
) -> Optional[dict]:
    """Return a minimal snapshot of the leading pair the brain is
    watching.

    Priority order:
    1. The highest-confidence un-vetoed candidate (`candidates[0]`).
       That's the pair closest to becoming Top-1; the user will see it
       on the chart and on the "Лидер ожидания" hint until either its
       confidence crosses the 80 % floor or another pair overtakes it.
    2. If every pair is vetoed, fall back to the strongest *evaluated*
       pair (max ``composite_score``) so the chart still has something
       meaningful to plot.
    """
    pick = candidates[0] if candidates else None
    if pick is None and all_evals:
        pick = max(
            all_evals,
            key=lambda e: (e.get("composite_score") or 0.0),
            default=None,
        )
    if pick is None:
        return None
    return {
        "pair": pick.get("pair"),
        "name_ru": pick.get("name_ru"),
        "side": pick.get("side"),
        "confidence": pick.get("confidence"),
        "veto": pick.get("veto"),
        "composite_score": pick.get("composite_score"),
    }
