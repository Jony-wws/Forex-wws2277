"""Order book approximation based on real price/volume data.

Since free forex data sources don't provide real order book (Level 2) data,
we approximate market depth using:
- Bid/Ask spread estimation from recent price action
- Volume profile analysis (support/resistance zones)
- Price clustering to find key levels
"""
from __future__ import annotations

import threading
import time

import numpy as np
import pandas as pd
from .prices import fetch_bars

_OB_TTL_SEC = 30          # full orderbook per-pair cache
_VP_TTL_SEC = 5 * 60      # volume profile per-pair cache (heavier)
_OB_CACHE: dict[str, tuple[float, dict]] = {}
_VP_CACHE: dict[str, tuple[float, list[dict]]] = {}
_OB_LOCK = threading.Lock()


def _estimate_spread(pair: str) -> dict:
    """Estimate bid/ask from recent 1-minute bars."""
    df = fetch_bars(pair, "1m", "1d")
    if df.empty or len(df) < 5:
        return {"bid": 0, "ask": 0, "spread": 0, "spread_pips": 0}

    last = df.iloc[-1]
    close = float(last["Close"])
    high = float(last["High"])
    low = float(last["Low"])

    # Approximate spread from high-low of last bar
    bar_range = high - low
    if bar_range == 0:
        bar_range = close * 0.0001  # fallback

    # Typical forex spread is ~20-50% of 1min bar range
    half_spread = bar_range * 0.3
    bid = close - half_spread
    ask = close + half_spread

    is_jpy = "JPY" in pair
    pip = 0.01 if is_jpy else 0.0001
    spread_pips = round((ask - bid) / pip, 1)

    return {
        "bid": bid,
        "ask": ask,
        "spread": ask - bid,
        "spread_pips": spread_pips,
        "mid": close,
    }


def _volume_profile(pair: str, bars: int = 100) -> list[dict]:
    """Compute volume profile - key price levels where most trading happened.

    Cached per-pair for ``_VP_TTL_SEC`` because the underlying 1h Yahoo
    history is also cached and the profile is one of the heavier steps in
    the orderbook build.
    """
    cached = _VP_CACHE.get(pair)
    if cached and time.time() - cached[0] < _VP_TTL_SEC:
        return cached[1]

    df = fetch_bars(pair, "1h", "5d")
    if df.empty or len(df) < 20:
        return []

    df = df.tail(bars)
    closes = df["Close"].values
    volumes = df["Volume"].values

    # If no volume data, use equal weights
    if np.sum(volumes) == 0:
        volumes = np.ones_like(closes)

    # Create price bins
    price_min, price_max = float(np.min(closes)), float(np.max(closes))
    n_bins = 15
    if price_max == price_min:
        return []

    bins = np.linspace(price_min, price_max, n_bins + 1)
    profile = []

    for i in range(n_bins):
        low, high = bins[i], bins[i + 1]
        mask = (closes >= low) & (closes < high)
        vol = float(np.sum(volumes[mask]))
        count = int(np.sum(mask))
        mid = (low + high) / 2

        profile.append({
            "price": round(mid, 5),
            "price_low": round(low, 5),
            "price_high": round(high, 5),
            "volume": round(vol, 0),
            "bar_count": count,
        })

    # Normalize volumes to percentage
    total_vol = sum(p["volume"] for p in profile)
    if total_vol > 0:
        for p in profile:
            p["volume_pct"] = round(p["volume"] / total_vol * 100, 1)
    else:
        for p in profile:
            p["volume_pct"] = round(p["bar_count"] / max(1, len(df)) * 100, 1)

    _VP_CACHE[pair] = (time.time(), profile)
    return profile


def _find_support_resistance(pair: str) -> dict:
    """Find key support and resistance levels."""
    df = fetch_bars(pair, "1h", "1mo")
    if df.empty or len(df) < 30:
        return {"supports": [], "resistances": []}

    close = float(df["Close"].iloc[-1])
    highs = df["High"].values
    lows = df["Low"].values

    # Find local maxima/minima
    supports = []
    resistances = []

    for i in range(2, len(df) - 2):
        if lows[i] <= lows[i-1] and lows[i] <= lows[i+1] and lows[i] <= lows[i-2] and lows[i] <= lows[i+2]:
            supports.append(float(lows[i]))
        if highs[i] >= highs[i-1] and highs[i] >= highs[i+1] and highs[i] >= highs[i-2] and highs[i] >= highs[i+2]:
            resistances.append(float(highs[i]))

    # Cluster nearby levels
    supports = _cluster_levels(supports, close * 0.001)
    resistances = _cluster_levels(resistances, close * 0.001)

    # Keep only levels near current price (within 2%)
    supports = [s for s in sorted(supports) if s < close and s > close * 0.98][-3:]
    resistances = [r for r in sorted(resistances) if r > close and r < close * 1.02][:3]

    return {
        "supports": [round(s, 5) for s in supports],
        "resistances": [round(r, 5) for r in resistances],
    }


def _cluster_levels(levels: list[float], threshold: float) -> list[float]:
    """Cluster nearby price levels into single levels."""
    if not levels:
        return []
    levels = sorted(levels)
    clusters = [[levels[0]]]
    for lvl in levels[1:]:
        if lvl - clusters[-1][-1] < threshold:
            clusters[-1].append(lvl)
        else:
            clusters.append([lvl])
    return [sum(c) / len(c) for c in clusters]


def _approximate_depth(pair: str) -> list[dict]:
    """Create approximate depth chart from volume profile and S/R levels."""
    profile = _volume_profile(pair)
    if not profile:
        return []

    spread = _estimate_spread(pair)
    mid = spread.get("mid", 0)
    if mid == 0:
        return []

    depth = []
    for p in profile:
        side = "bid" if p["price"] < mid else "ask"
        distance = abs(p["price"] - mid)
        is_jpy = "JPY" in pair
        pip = 0.01 if is_jpy else 0.0001
        distance_pips = round(distance / pip, 1)

        depth.append({
            "price": p["price"],
            "side": side,
            "volume_pct": p["volume_pct"],
            "distance_pips": distance_pips,
        })

    return depth


def get_orderbook(pair: str, force_refresh: bool = False) -> dict:
    """Full order book data for a pair (cached for ``_OB_TTL_SEC``)."""
    if not force_refresh:
        cached = _OB_CACHE.get(pair)
        if cached and time.time() - cached[0] < _OB_TTL_SEC:
            return cached[1]

    with _OB_LOCK:
        # Double-check after acquiring the lock so concurrent callers reuse
        # the same fresh result instead of recomputing in parallel.
        if not force_refresh:
            cached = _OB_CACHE.get(pair)
            if cached and time.time() - cached[0] < _OB_TTL_SEC:
                return cached[1]

        spread = _estimate_spread(pair)
        sr = _find_support_resistance(pair)
        depth = _approximate_depth(pair)
        profile = _volume_profile(pair)

        is_jpy = "JPY" in pair
        fmt = 3 if is_jpy else 5

        result = {
            "pair": pair,
            "bid": round(spread["bid"], fmt),
            "ask": round(spread["ask"], fmt),
            "spread_pips": spread["spread_pips"],
            "mid": round(spread.get("mid", 0), fmt),
            "supports": sr["supports"],
            "resistances": sr["resistances"],
            "depth": depth,
            "volume_profile": profile,
        }
        _OB_CACHE[pair] = (time.time(), result)
        return result
