"""Market regime detection — trending vs ranging vs volatile.

Classifies the *current* state of a pair into one of three regimes so
downstream gates (publication floor, threshold tightening) can adapt:

* ``trending`` — clean directional flow, ADX is high, ATR is moderate,
  and recent H1 candles show a clear bias.  This is the regime the
  strict 5-hour cycle was originally tuned for, so we *tighten*
  thresholds further (less false-positive room).
* ``ranging``  — ADX is low, ATR is compressed, candles oscillate.
  Mean-reversion edges still exist, so we *relax* trend-quality
  thresholds slightly but *also* reduce confidence to be honest about
  the lower directional conviction.
* ``volatile`` — ATR is elevated relative to its history regardless of
  direction (news-driven, wide-range bars).  We tighten thresholds
  because false breakouts are most expensive in this regime.

The result is a small dict:

    {
        "regime": "trending" | "ranging" | "volatile",
        "adx_h1": 27.4,
        "atr_pctile": 0.62,
        "directional_persistence": 0.8,
        "confidence_multiplier": 0.92,
        "threshold_multiplier": 1.05,
        "reasons": [...],
    }

``confidence_multiplier`` and ``threshold_multiplier`` are unit-less
scalars callers can apply uniformly — multiplying the confidence by
``confidence_multiplier`` and the strong-gate thresholds by
``threshold_multiplier``.  Defaults stay close to 1.0 so the regime
adjustment is a *nudge*, not a hard reclassification.
"""
from __future__ import annotations

import logging
from typing import Optional

from .prices import fetch_bars

log = logging.getLogger("regime_detection")


# ── Regime cut-offs ───────────────────────────────────────────────────
# Trending: H1 ADX is comfortably above the strong-trend threshold AND
# the last five H1 bars are mostly directional.
TRENDING_ADX_MIN = 25.0
TRENDING_PERSISTENCE_MIN = 0.6

# Volatile: H1 ATR is above the 80th percentile of its own 90-bar
# history.  We classify volatile *before* trending so news-driven wide
# bars don't masquerade as a clean trend.
VOLATILE_ATR_PCTILE = 0.80
VOLATILE_ATR_LOOKBACK = 90

# Ranging: H1 ADX is below 18 AND ATR is below the 25th percentile of
# its own 90-bar history.  Anything in between is "neutral" — we still
# label that as trending for the strict cycle so the existing behaviour
# stays intact.
RANGING_ADX_MAX = 18.0
RANGING_ATR_PCTILE = 0.25


def _safe_fetch(pair: str):
    try:
        return fetch_bars(pair, "1h", "3mo")
    except Exception as e:  # noqa: BLE001
        log.debug(f"regime_detection fetch failed {pair}: {e}")
        return None


def _persistence_last_n(bars, n: int = 5) -> float:
    """Share of the last ``n`` H1 bars that closed in the dominant direction."""
    if bars is None or bars.empty or len(bars) < n:
        return 0.0
    last = bars.tail(n)
    bull = int((last["Close"] > last["Open"]).sum())
    bear = int((last["Close"] < last["Open"]).sum())
    if bull == 0 and bear == 0:
        return 0.0
    return round(max(bull, bear) / float(n), 3)


def _atr_percentile(bars, lookback: int) -> Optional[float]:
    """Where does the current ATR(14) sit in its own ``lookback``-bar history?

    Returns ``None`` if the data is too short.  The percentile is the
    fraction of historical bars whose ATR is *below* the current bar's
    ATR, so ``1.0`` means the current bar is the most volatile in the
    window.
    """
    if bars is None or bars.empty or len(bars) < lookback + 14:
        return None
    from .indicators import atr as atr_fn

    atr_series = atr_fn(bars, period=14).dropna()
    if len(atr_series) < lookback + 1:
        return None
    window = atr_series.tail(lookback + 1)
    current = float(window.iloc[-1])
    # Strictly-less-than rank → percentile in (0, 1].
    below = int((window.iloc[:-1] < current).sum())
    return round(below / float(lookback), 3)


def _adx_now(bars) -> float:
    """Latest H1 ADX(14) reading, or 0.0 if data is missing."""
    if bars is None or bars.empty or len(bars) < 30:
        return 0.0
    try:
        from .indicators import adx as adx_fn
        adx_series, _, _ = adx_fn(bars, period=14)
        return float(adx_series.iloc[-1])
    except Exception as e:  # noqa: BLE001
        log.debug(f"adx fetch failed: {e}")
        return 0.0


def detect_market_regime(pair: str) -> dict:
    """Classify ``pair`` into trending / ranging / volatile.

    All exceptions are swallowed — the regime detector is advisory, it
    must never crash the publication pipeline.  On failure we return a
    neutral "trending" fallback with multipliers at 1.0 so the caller
    behaves as if regime detection didn't run.
    """
    bars = _safe_fetch(pair)
    if bars is None or bars.empty:
        return {
            "regime": "trending",
            "adx_h1": 0.0,
            "atr_pctile": None,
            "directional_persistence": 0.0,
            "confidence_multiplier": 1.0,
            "threshold_multiplier": 1.0,
            "reasons": ["Нет данных для определения режима — fallback на trending"],
        }

    adx_h1 = _adx_now(bars)
    persistence = _persistence_last_n(bars, n=5)
    atr_pctile = _atr_percentile(bars, VOLATILE_ATR_LOOKBACK)

    reasons: list[str] = []

    # 1. Volatile first — wide-range bars dominate everything else.
    if atr_pctile is not None and atr_pctile >= VOLATILE_ATR_PCTILE:
        reasons.append(
            f"ATR в {atr_pctile:.0%} перцентиле (≥ {VOLATILE_ATR_PCTILE:.0%})"
        )
        return {
            "regime": "volatile",
            "adx_h1": round(adx_h1, 2),
            "atr_pctile": atr_pctile,
            "directional_persistence": persistence,
            # In volatile regime we *tighten* the strong-gate thresholds
            # 10 % and discount confidence 5 % — false breakouts are
            # most expensive when ATR is elevated.
            "confidence_multiplier": 0.95,
            "threshold_multiplier": 1.10,
            "reasons": reasons + [f"ADX H1 = {adx_h1:.1f}, persistence = {persistence}"],
        }

    # 2. Trending — clean directional flow.
    if adx_h1 >= TRENDING_ADX_MIN and persistence >= TRENDING_PERSISTENCE_MIN:
        reasons.append(
            f"ADX H1 = {adx_h1:.1f} ≥ {TRENDING_ADX_MIN:.0f} и persistence "
            f"= {persistence} ≥ {TRENDING_PERSISTENCE_MIN:.0%}"
        )
        return {
            "regime": "trending",
            "adx_h1": round(adx_h1, 2),
            "atr_pctile": atr_pctile,
            "directional_persistence": persistence,
            # In trending regime we *tighten* thresholds 5 % so only the
            # very best clean setups survive — confidence stays at 1.0.
            "confidence_multiplier": 1.0,
            "threshold_multiplier": 1.05,
            "reasons": reasons,
        }

    # 3. Ranging — flat ADX and compressed ATR.
    if (
        adx_h1 < RANGING_ADX_MAX
        and atr_pctile is not None
        and atr_pctile <= RANGING_ATR_PCTILE
    ):
        reasons.append(
            f"ADX H1 = {adx_h1:.1f} < {RANGING_ADX_MAX:.0f} и ATR в нижнем "
            f"{atr_pctile:.0%} перцентиле"
        )
        return {
            "regime": "ranging",
            "adx_h1": round(adx_h1, 2),
            "atr_pctile": atr_pctile,
            "directional_persistence": persistence,
            # In ranging regime we *relax* thresholds 5 % but *reduce*
            # confidence 8 % — mean-reversion edge exists but conviction
            # is honestly lower.
            "confidence_multiplier": 0.92,
            "threshold_multiplier": 0.95,
            "reasons": reasons,
        }

    # 4. Neutral — defaults are pass-through (multipliers = 1.0).
    return {
        "regime": "trending",
        "adx_h1": round(adx_h1, 2),
        "atr_pctile": atr_pctile,
        "directional_persistence": persistence,
        "confidence_multiplier": 1.0,
        "threshold_multiplier": 1.0,
        "reasons": [
            f"Между trending/ranging — ADX {adx_h1:.1f}, ATR pctile "
            f"{atr_pctile if atr_pctile is None else f'{atr_pctile:.0%}'}, "
            f"persistence {persistence}"
        ],
    }


def apply_regime_to_thresholds(base_thresholds: dict, regime_info: dict) -> dict:
    """Apply ``regime_info['threshold_multiplier']`` to a thresholds dict.

    ``base_thresholds`` is a dict like ``{"adx_h1": 30.0, "ratio": 0.65, ...}``
    — every numeric value gets multiplied by the regime multiplier.
    Non-numeric values are passed through untouched so this helper is
    safe to use on the strong-gate dict ``snapshot`` already publishes.
    """
    mult = float(regime_info.get("threshold_multiplier") or 1.0)
    out: dict = {}
    for key, val in base_thresholds.items():
        if isinstance(val, (int, float)) and not isinstance(val, bool):
            out[key] = round(val * mult, 4)
        else:
            out[key] = val
    return out
