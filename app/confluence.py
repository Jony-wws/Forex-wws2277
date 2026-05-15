"""Multi-timeframe + multi-indicator confluence scoring.

This module is the user's explicit ask — "расширить анализ: больше
индикаторов, больше таймфреймов → больше шансов найти настоящие 80% в
каждом цикле".  It folds the existing 15-block analyser, the new
indicators in ``app/extra_indicators.py``, and a 5-timeframe alignment
check (W1 / D1 / H4 / H1 / M15) into a single 0-100 confluence score
plus a strict ``super_confluence`` boolean.

The score is *directional*:

* > 0  → confluent BUY (bullish across the indicators)
* < 0  → confluent SELL
* ~0   → indicators disagree, no clean trade

``super_confluence`` fires only when **all** of these hold:

* 5/5 timeframes agree on direction (W1, D1, H4, H1, M15)
* ≥ 7 / 10 indicator votes agree on direction
* H1 ADX ≥ 22 (a genuine trend, not chop)
* squeeze release OR aligned momentum on H1 (volatility confirms move)

This boolean is consumed by ``app/brain.py`` to award a +0.30 bonus to
the composite score — letting technically-perfect setups clear the
strict 80 % publication floor without depending on macro/carry.
"""
from __future__ import annotations

from typing import Optional

from .extra_indicators import compute_extras
from .indicators import compute_all
from .prices import fetch_bars


# ─── Tunables ────────────────────────────────────────────────────────

# Minimum H1 ADX required for super-confluence.  Below 22 a "trend" is
# really chop — any directional bet has near-coinflip odds regardless
# of how many oscillators agree.
SUPER_CONFLUENCE_MIN_ADX_H1 = 22.0

# Minimum number of independent indicator votes required (out of 10).
SUPER_CONFLUENCE_MIN_VOTES = 7

# Voting indicators (10 total, each contributes +1 BUY / -1 SELL / 0 neutral)
INDICATOR_NAMES = (
    "ema_stack",
    "rsi",
    "macd",
    "stoch",
    "adx",
    "mfi",
    "cci",
    "obv",
    "supertrend",
    "vortex",
)


def _tf_direction(ind: dict) -> int:
    """Return +1 (bullish), -1 (bearish), 0 (neutral) for a TF block.

    Direction is derived from EMA stack on the TF — a battle-tested
    proxy that aligns with how the rest of the analyser scores trend.
    """
    if not ind:
        return 0
    close = ind.get("close", 0.0)
    ema20 = ind.get("ema20", close)
    ema50 = ind.get("ema50", close)
    if close > ema20 and ema20 > ema50:
        return +1
    if close < ema20 and ema20 < ema50:
        return -1
    return 0


def _vote(direction: int, extras: dict) -> dict:
    """Per-indicator BUY/SELL/NEUTRAL vote on a single TF.

    ``direction`` is the EMA-stack direction so we can phrase votes
    consistently (e.g. RSI > 55 in an uptrend = confirms BUY; in a
    downtrend = warns of overbought).  For confluence purposes each
    indicator answers "do you agree with the trend?" — +1 yes, -1 no,
    0 silent.
    """
    return direction


def confluence_snapshot(pair: str) -> Optional[dict]:
    """Build the full confluence record for one pair.

    Returns a dict with:

    * ``score``            — signed composite score, range ≈ [-30, +30]
    * ``score_pct``        — abs(score) / max_score in [0, 1]
    * ``side``             — ``"BUY"`` / ``"SELL"`` / ``None``
    * ``tf_directions``    — per-timeframe direction map
    * ``tf_aligned``       — True if all 5 TFs agree
    * ``votes``            — count of indicator votes per direction
    * ``super_confluence`` — strict boolean (see module docstring)
    * ``reasons``          — short list of human-readable findings
    """
    # Five real timeframes — no proxies, no synthetic resampling.
    bars_w1 = fetch_bars(pair, "1wk", "2y")
    bars_1d = fetch_bars(pair, "1d", "1y")
    bars_4h = fetch_bars(pair, "4h", "3mo")
    bars_1h = fetch_bars(pair, "1h", "1mo")
    bars_15m = fetch_bars(pair, "15m", "5d")
    if any(
        df is None or df.empty or len(df) < 30
        for df in (bars_w1, bars_1d, bars_4h, bars_1h, bars_15m)
    ):
        return None

    ind_w1 = compute_all(bars_w1)
    ind_1d = compute_all(bars_1d)
    ind_4h = compute_all(bars_4h)
    ind_1h = compute_all(bars_1h)
    ind_15m = compute_all(bars_15m)
    if any(i is None for i in (ind_w1, ind_1d, ind_4h, ind_1h, ind_15m)):
        return None

    # Extras only on H1 — that is the binary-option execution timeframe.
    # The other TFs are confirmation, not entry.
    extras_h1 = compute_extras(bars_1h)
    if extras_h1 is None:
        return None

    tf_dirs = {
        "W1": _tf_direction(ind_w1),
        "D1": _tf_direction(ind_1d),
        "H4": _tf_direction(ind_4h),
        "H1": _tf_direction(ind_1h),
        "M15": _tf_direction(ind_15m),
    }
    aligned = all(d == +1 for d in tf_dirs.values()) or all(
        d == -1 for d in tf_dirs.values()
    )
    leading_dir = +1 if sum(tf_dirs.values()) > 0 else (-1 if sum(tf_dirs.values()) < 0 else 0)

    # ─── Indicator votes (each +1/-1/0 along leading_dir) ────────
    votes: dict[str, int] = {}
    reasons: list[str] = []

    # 1. EMA stack on H1
    votes["ema_stack"] = _tf_direction(ind_1h)
    if votes["ema_stack"] != 0:
        reasons.append(
            f"H1 EMA-стек {'бычий' if votes['ema_stack'] > 0 else 'медвежий'}"
        )

    # 2. RSI on H1 — in trend-confirming zone
    rsi = ind_1h.get("rsi14", 50.0)
    if rsi >= 55:
        votes["rsi"] = +1
        reasons.append(f"RSI(H1) {rsi:.0f} — в бычьей зоне")
    elif rsi <= 45:
        votes["rsi"] = -1
        reasons.append(f"RSI(H1) {rsi:.0f} — в медвежьей зоне")
    else:
        votes["rsi"] = 0

    # 3. MACD histogram on H1
    macd_h = ind_1h.get("macd_hist", 0.0)
    if macd_h > 0:
        votes["macd"] = +1
        reasons.append("MACD(H1) положителен")
    elif macd_h < 0:
        votes["macd"] = -1
        reasons.append("MACD(H1) отрицателен")
    else:
        votes["macd"] = 0

    # 4. Stochastic on H1 — direction from K vs D
    sk, sd = ind_1h.get("stoch_k", 50.0), ind_1h.get("stoch_d", 50.0)
    if sk > sd and sk < 80:
        votes["stoch"] = +1
    elif sk < sd and sk > 20:
        votes["stoch"] = -1
    else:
        votes["stoch"] = 0

    # 5. ADX direction on H1 (using DI+/DI-)
    adx_val = ind_1h.get("adx", 0.0)
    plus_di = ind_1h.get("plus_di", 0.0)
    minus_di = ind_1h.get("minus_di", 0.0)
    if adx_val >= 20 and plus_di > minus_di:
        votes["adx"] = +1
        reasons.append(f"ADX(H1) {adx_val:.0f} с DI+ > DI- — тренд вверх")
    elif adx_val >= 20 and minus_di > plus_di:
        votes["adx"] = -1
        reasons.append(f"ADX(H1) {adx_val:.0f} с DI- > DI+ — тренд вниз")
    else:
        votes["adx"] = 0

    # 6. MFI on H1
    mfi_val = extras_h1.get("mfi")
    if mfi_val is None:
        votes["mfi"] = 0
    elif mfi_val >= 55:
        votes["mfi"] = +1
        reasons.append(f"MFI(H1) {mfi_val:.0f} — деньги входят (бычий)")
    elif mfi_val <= 45:
        votes["mfi"] = -1
        reasons.append(f"MFI(H1) {mfi_val:.0f} — деньги выходят (медвежий)")
    else:
        votes["mfi"] = 0

    # 7. CCI on H1
    cci_val = extras_h1.get("cci") or 0.0
    if cci_val >= 100:
        votes["cci"] = +1
        reasons.append(f"CCI(H1) {cci_val:.0f} — сильный бычий импульс")
    elif cci_val <= -100:
        votes["cci"] = -1
        reasons.append(f"CCI(H1) {cci_val:.0f} — сильный медвежий импульс")
    elif cci_val >= 30:
        votes["cci"] = +1
    elif cci_val <= -30:
        votes["cci"] = -1
    else:
        votes["cci"] = 0

    # 8. OBV slope
    obv_val = extras_h1.get("obv_slope") or 0.0
    if obv_val >= 0.4:
        votes["obv"] = +1
        reasons.append("OBV растёт — объёмы подтверждают рост")
    elif obv_val <= -0.4:
        votes["obv"] = -1
        reasons.append("OBV падает — объёмы подтверждают падение")
    else:
        votes["obv"] = 0

    # 9. Supertrend direction
    st = extras_h1.get("supertrend")
    if st is None:
        votes["supertrend"] = 0
    elif st["direction"] > 0:
        votes["supertrend"] = +1
        reasons.append("Supertrend(H1) бычий")
    else:
        votes["supertrend"] = -1
        reasons.append("Supertrend(H1) медвежий")

    # 10. Vortex spread
    vx = extras_h1.get("vortex")
    if vx is None or vx.get("spread") is None:
        votes["vortex"] = 0
    elif vx["spread"] > 0.05:
        votes["vortex"] = +1
        reasons.append(f"Vortex VI+ - VI- = {vx['spread']:+.2f} — тренд вверх")
    elif vx["spread"] < -0.05:
        votes["vortex"] = -1
        reasons.append(f"Vortex VI+ - VI- = {vx['spread']:+.2f} — тренд вниз")
    else:
        votes["vortex"] = 0

    bull_votes = sum(1 for v in votes.values() if v > 0)
    bear_votes = sum(1 for v in votes.values() if v < 0)
    neutral_votes = sum(1 for v in votes.values() if v == 0)
    leading_count = max(bull_votes, bear_votes)
    leading_side = "BUY" if bull_votes > bear_votes else "SELL" if bear_votes > bull_votes else None

    # Squeeze release on H1 is a separate volatility confirmation —
    # not counted in the 10 votes, but required for super-confluence.
    squeeze = extras_h1.get("squeeze") or {}
    just_released = bool(squeeze.get("squeeze_just_released"))
    squeeze_momentum_val = squeeze.get("momentum") or 0.0
    momentum_aligned = (
        (leading_side == "BUY" and squeeze_momentum_val > 0)
        or (leading_side == "SELL" and squeeze_momentum_val < 0)
    )

    # ─── Composite numeric score for the brain ────────────────────
    #
    # Each indicator vote contributes ±2 *in its own direction* (BUY
    # = +2, SELL = -2, neutral = 0).  Multi-TF alignment contributes
    # ±10 in the leading direction — a heavy tilt because the user's
    # "пять таймфреймов согласованы" is the single best predictor of
    # 5h directional success.  Squeeze release with aligned momentum
    # adds another ±4 in the leading direction.
    #
    # The composite is intrinsically *signed*: positive ⇒ confluent
    # BUY, negative ⇒ confluent SELL.  ``side`` is derived from the
    # sign, so by construction ``score`` and ``side`` always agree.
    score = 0
    for v in votes.values():
        score += 2 * v
    if aligned and leading_side is not None:
        score += 10 if leading_side == "BUY" else -10
    if just_released and momentum_aligned and leading_side is not None:
        score += 4 if leading_side == "BUY" else -4
        reasons.append("Сжатие Боллинджера/Кельтнера разрешилось по направлению")

    max_score = 2 * len(votes) + 10 + 4  # ≈ 34
    score_pct = abs(score) / max_score if max_score > 0 else 0.0

    super_confluence = bool(
        leading_side in ("BUY", "SELL")
        and aligned
        and leading_count >= SUPER_CONFLUENCE_MIN_VOTES
        and adx_val >= SUPER_CONFLUENCE_MIN_ADX_H1
        and (just_released or momentum_aligned)
    )

    return {
        "pair": pair,
        "side": leading_side,
        "score": int(score),
        "max_score": int(max_score),
        "score_pct": round(score_pct, 3),
        "tf_directions": tf_dirs,
        "tf_aligned": bool(aligned),
        "votes": votes,
        "bull_votes": bull_votes,
        "bear_votes": bear_votes,
        "neutral_votes": neutral_votes,
        "leading_count": leading_count,
        "adx_h1": round(adx_val, 1),
        "squeeze_released": just_released,
        "squeeze_momentum_aligned": bool(momentum_aligned),
        "super_confluence": super_confluence,
        "reasons": reasons[:6],
    }


def confluence_norm(snapshot: Optional[dict]) -> float:
    """Map a confluence snapshot to a normalised value in [-1, +1].

    Returns 0.0 when the snapshot is missing or has no direction.
    Used by ``app/brain.py`` as the contribution of the new
    ``confluence`` layer to the composite score.
    """
    if snapshot is None or snapshot.get("side") is None:
        return 0.0
    score = snapshot.get("score", 0)
    max_score = snapshot.get("max_score", 1) or 1
    return max(-1.0, min(1.0, score / max_score))
