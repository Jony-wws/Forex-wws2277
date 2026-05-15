"""Volume Profile (POC + Value Area) approximation for forex.

Yahoo Finance's forex feed has tick-level volume = 0, so the only
practical proxy is *typical price × bar count* weighted by bar range —
which still gives a usable POC (the most-traded price level) for swing
analysis on M15..H4.  This is a deterministic, single-pass calculator
suitable for CI cron jobs.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def volume_profile(
    df: pd.DataFrame,
    bins: int = 24,
    lookback: int = 100,
) -> dict:
    """Return POC, Value Area high/low, and price's position vs them."""
    if df is None or len(df) < 30:
        return {"poc": None, "vah": None, "val": None, "score": 0, "reason": ""}

    window = df.tail(lookback)
    highs = window["High"].to_numpy()
    lows = window["Low"].to_numpy()
    closes = window["Close"].to_numpy()
    typical = (highs + lows + closes) / 3.0

    # Bar weight = range; flat bars contribute almost nothing to POC.
    weights = (highs - lows)
    if weights.sum() <= 0:
        weights = np.ones_like(weights)

    lo = float(lows.min())
    hi = float(highs.max())
    if hi <= lo:
        return {"poc": None, "vah": None, "val": None, "score": 0, "reason": ""}

    edges = np.linspace(lo, hi, bins + 1)
    centers = (edges[:-1] + edges[1:]) / 2.0
    hist, _ = np.histogram(typical, bins=edges, weights=weights)

    total = float(hist.sum())
    if total <= 0:
        return {"poc": None, "vah": None, "val": None, "score": 0, "reason": ""}

    # POC = bin with the largest weight.
    poc_idx = int(hist.argmax())
    poc_price = float(centers[poc_idx])

    # Value Area = expand outward from POC until 70% of weighted volume captured.
    target = 0.70 * total
    cum = float(hist[poc_idx])
    left, right = poc_idx, poc_idx
    while cum < target and (left > 0 or right < bins - 1):
        left_val = hist[left - 1] if left > 0 else -1
        right_val = hist[right + 1] if right < bins - 1 else -1
        if left_val >= right_val:
            left -= 1
            cum += float(hist[left])
        else:
            right += 1
            cum += float(hist[right])

    val_low = float(edges[left])
    val_high = float(edges[right + 1])
    last_close = float(closes[-1])

    if last_close > val_high:
        score = +1
        reason = "Цена выше Value Area High — институциональный спрос"
    elif last_close < val_low:
        score = -1
        reason = "Цена ниже Value Area Low — институциональное предложение"
    else:
        score = 0
        reason = "Цена внутри Value Area — равновесие"

    return {
        "poc": round(poc_price, 6),
        "vah": round(val_high, 6),
        "val": round(val_low, 6),
        "score": score,
        "reason": reason,
    }
