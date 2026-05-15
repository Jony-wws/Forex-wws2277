"""Lightweight Wyckoff phase detection.

Wyckoff in trading literature recognises four phases (Accumulation,
Markup, Distribution, Markdown).  A full Wyckoff schematic needs the
trader's eye on volume + structure across weeks of bars; this module
implements a *minimal, mechanical proxy* that works on M15..H4 forex
data and feeds a single signed score into the AI brain.

Heuristic:
- Compute the 50-bar rolling range (high − low).
- If the most recent 20 bars stay inside the prior 50-bar range AND
  volume is contracting → potential Accumulation / Distribution.
- Direction (accum vs distribution) is decided by where price sits in
  the range (lower third → accum, upper third → distribution).
- A strong impulsive close *out of* the range = phase shift (Markup
  starts on a bullish breakout from accumulation; Markdown on a
  bearish breakdown from distribution).
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def _zscore(series: pd.Series, window: int = 50) -> float:
    if len(series) < window:
        return 0.0
    last = float(series.iloc[-1])
    mean = float(series.rolling(window).mean().iloc[-1])
    std = float(series.rolling(window).std().iloc[-1] or 1.0)
    if std == 0:
        return 0.0
    return (last - mean) / std


def wyckoff_phase(df: pd.DataFrame) -> dict:
    """Return a phase label, signed score and breakdown reason."""
    if df is None or len(df) < 60:
        return {"phase": "unknown", "score": 0, "reason": "Мало данных"}

    closes = df["Close"]
    highs = df["High"]
    lows = df["Low"]

    range50_high = float(highs.tail(50).max())
    range50_low = float(lows.tail(50).min())
    range_size = range50_high - range50_low or 1e-9
    last_close = float(closes.iloc[-1])

    pos_in_range = (last_close - range50_low) / range_size  # 0..1

    recent_range = float(highs.tail(20).max() - lows.tail(20).min())
    prior_range = float(highs.iloc[-50:-20].max() - lows.iloc[-50:-20].min())

    volume = df.get("Volume")
    vol_contract = False
    if volume is not None and float(volume.tail(50).sum()) > 0:
        recent_vol = float(volume.tail(20).mean())
        prior_vol = float(volume.iloc[-50:-20].mean() or 1.0)
        vol_contract = recent_vol < 0.7 * prior_vol

    consolidation = recent_range < 0.7 * (prior_range or recent_range or 1e-9)

    # Default — trend phase.
    momentum_5 = float(closes.iloc[-1] - closes.iloc[-6]) if len(closes) > 6 else 0.0

    if consolidation and pos_in_range < 0.4:
        return {
            "phase": "accumulation",
            "score": +2,
            "reason": "Сжатие в нижней трети диапазона — накопление",
            "volume_contraction": vol_contract,
        }
    if consolidation and pos_in_range > 0.6:
        return {
            "phase": "distribution",
            "score": -2,
            "reason": "Сжатие в верхней трети диапазона — распределение",
            "volume_contraction": vol_contract,
        }

    breakout_z = _zscore(closes, 50)
    if breakout_z > 1.5 and momentum_5 > 0:
        return {
            "phase": "markup",
            "score": +3,
            "reason": "Сильный пробой вверх — фаза Markup",
        }
    if breakout_z < -1.5 and momentum_5 < 0:
        return {
            "phase": "markdown",
            "score": -3,
            "reason": "Сильный пробой вниз — фаза Markdown",
        }

    return {
        "phase": "range",
        "score": 0,
        "reason": "Боковой диапазон — без фазового перевеса",
    }
