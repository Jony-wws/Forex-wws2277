"""Safety checks for the 5-hour binary-options cycle.

The user explicitly asked that the system *know* a trade will still be
in profit at the end of the 5-hour window — not just that the entry
candle looks good.  Two checks live here:

1. ``reversal_risk_h1`` — scans the last few H1 candles for a strong
   reversal pattern against the proposed side (bearish engulfing /
   shooting star against BUY, bullish engulfing / hammer against
   SELL).  If found, the trade is vetoed.

2. ``five_hour_projection`` — projects price five H1 bars forward using
   EMA slope + ATR-bounded drift, then checks whether the projected
   end price is still on the right side of the entry by at least a
   safety margin.  This is a *quantitative* model of "последний момент
   не должен быть минус" — the cycle's expiry boundary.

Both helpers are pure functions over a single ``bars_1h`` DataFrame
returned by ``app.prices.fetch_bars`` — no side effects, no network.
"""
from __future__ import annotations

import logging
from typing import Optional

import pandas as pd

log = logging.getLogger("safety")


REVERSAL_LOOKBACK_BARS = 3
PROJECTION_HORIZON_BARS = 5     # 5 × H1 = 5 hours
SAFETY_MARGIN_FRACTION_OF_ATR = 0.5


def _engulfing(prev: pd.Series, curr: pd.Series, side: str) -> bool:
    """Bearish/bullish engulfing pattern.

    ``side`` here is the *trade* side we are about to take, so the
    function returns True if the *opposite* (engulfing-against-trade)
    pattern is found.
    """
    prev_body = abs(prev["Close"] - prev["Open"])
    curr_body = abs(curr["Close"] - curr["Open"])
    if curr_body <= prev_body:
        return False
    if side == "BUY":
        # Bearish engulfing against a BUY: prev green, curr red
        return (
            prev["Close"] > prev["Open"]
            and curr["Close"] < curr["Open"]
            and curr["Open"] >= prev["Close"]
            and curr["Close"] <= prev["Open"]
        )
    if side == "SELL":
        # Bullish engulfing against a SELL
        return (
            prev["Close"] < prev["Open"]
            and curr["Close"] > curr["Open"]
            and curr["Open"] <= prev["Close"]
            and curr["Close"] >= prev["Open"]
        )
    return False


def _shooting_star(bar: pd.Series, side: str) -> bool:
    """Shooting star against BUY / hammer against SELL."""
    rng = bar["High"] - bar["Low"]
    if rng <= 0:
        return False
    body = abs(bar["Close"] - bar["Open"])
    upper_wick = bar["High"] - max(bar["Close"], bar["Open"])
    lower_wick = min(bar["Close"], bar["Open"]) - bar["Low"]
    if side == "BUY":
        # Long upper wick = sellers stepped in at the top
        return body <= rng * 0.35 and upper_wick >= rng * 0.55
    if side == "SELL":
        # Long lower wick = buyers stepped in at the bottom
        return body <= rng * 0.35 and lower_wick >= rng * 0.55
    return False


def reversal_risk_h1(
    bars_1h: pd.DataFrame,
    side: str,
    lookback: int = REVERSAL_LOOKBACK_BARS,
) -> dict:
    """Detect a reversal pattern against ``side`` in the last bars.

    Returns ``{"reversal": bool, "reason": str, "bar_index": int}``.
    ``bar_index = -1`` means "no reversal".
    """
    if bars_1h is None or bars_1h.empty or side not in ("BUY", "SELL"):
        return {"reversal": False, "reason": "Нет данных или нейтрально", "bar_index": -1}
    if len(bars_1h) < lookback + 1:
        return {"reversal": False, "reason": "Мало H1-баров", "bar_index": -1}

    recent = bars_1h.tail(lookback + 1).reset_index(drop=True)
    for i in range(1, len(recent)):
        prev = recent.iloc[i - 1]
        curr = recent.iloc[i]
        if _engulfing(prev, curr, side):
            return {
                "reversal": True,
                "reason": f"Разворотный engulfing против {side} на свече H1-{lookback - i + 1}",
                "bar_index": i,
            }
        if _shooting_star(curr, side):
            label = "shooting star" if side == "BUY" else "hammer"
            return {
                "reversal": True,
                "reason": f"Разворотная свеча {label} против {side} на свече H1-{lookback - i + 1}",
                "bar_index": i,
            }
    return {"reversal": False, "reason": "Разворотных паттернов нет", "bar_index": -1}


def _ema(series: pd.Series, period: int) -> float:
    if len(series) < period:
        return float(series.iloc[-1])
    return float(series.ewm(span=period, adjust=False).mean().iloc[-1])


def _atr(bars: pd.DataFrame, period: int = 14) -> float:
    if len(bars) < period + 1:
        return float((bars["High"] - bars["Low"]).iloc[-1] or 0)
    high = bars["High"]
    low = bars["Low"]
    close = bars["Close"]
    prev_close = close.shift(1)
    tr = pd.concat(
        [(high - low).abs(), (high - prev_close).abs(), (low - prev_close).abs()],
        axis=1,
    ).max(axis=1)
    return float(tr.rolling(period).mean().iloc[-1] or 0)


def five_hour_projection(
    bars_1h: pd.DataFrame,
    side: str,
    horizon: int = PROJECTION_HORIZON_BARS,
) -> dict:
    """Project the end-of-cycle price and check it's still in profit.

    Heuristic: linear drift = mean(close diff over last 10 bars), bounded
    by ±0.5×ATR per bar.  Project ``horizon`` bars forward.  Then check
    that the projected end price is on the right side of the entry by at
    least ``SAFETY_MARGIN_FRACTION_OF_ATR × ATR(H1)`` so we have a real
    profit margin at expiry — not just a coin flip.

    Returns a dict with the projected price, raw drift, ATR, margin and
    a ``passes`` flag the brain treats as a veto when False.
    """
    if bars_1h is None or bars_1h.empty or side not in ("BUY", "SELL"):
        return {
            "passes": False,
            "reason": "Нет данных для 5h-проекции",
            "projected_close": None,
            "entry": None,
            "atr": 0.0,
            "drift_per_bar": 0.0,
            "horizon_bars": horizon,
            "safety_margin": 0.0,
        }
    if len(bars_1h) < 20:
        return {
            "passes": False,
            "reason": f"Мало H1-баров ({len(bars_1h)}<20)",
            "projected_close": None,
            "entry": None,
            "atr": 0.0,
            "drift_per_bar": 0.0,
            "horizon_bars": horizon,
            "safety_margin": 0.0,
        }

    closes = bars_1h["Close"]
    entry = float(closes.iloc[-1])

    # Drift = average per-bar change over the last 10 bars, clipped to ±0.5×ATR.
    diffs = closes.diff().tail(10).dropna()
    raw_drift = float(diffs.mean()) if len(diffs) else 0.0
    atr_val = _atr(bars_1h, period=14)
    max_drift = 0.5 * atr_val
    drift = max(-max_drift, min(max_drift, raw_drift)) if max_drift > 0 else raw_drift

    projected = entry + drift * horizon
    safety_margin = SAFETY_MARGIN_FRACTION_OF_ATR * atr_val

    if side == "BUY":
        in_profit = projected - entry >= safety_margin
        progress = projected - entry
    else:
        in_profit = entry - projected >= safety_margin
        progress = entry - projected

    if in_profit:
        reason = (
            f"5h-проекция в плюсе: drift={drift:+.5f}/бар, "
            f"итог {progress:+.5f} ≥ запас {safety_margin:.5f}"
        )
    elif progress >= 0:
        reason = (
            f"5h-проекция в нейтрале: drift={drift:+.5f}/бар, "
            f"итог {progress:+.5f} < запас {safety_margin:.5f}"
        )
    else:
        reason = (
            f"5h-проекция МИНУС: drift={drift:+.5f}/бар, "
            f"итог {progress:+.5f} против {side}"
        )

    return {
        "passes": bool(in_profit),
        "reason": reason,
        "projected_close": round(projected, 5),
        "entry": round(entry, 5),
        "atr": round(atr_val, 5),
        "drift_per_bar": round(drift, 5),
        "horizon_bars": horizon,
        "safety_margin": round(safety_margin, 5),
    }


def weekly_bias(bars_1w: pd.DataFrame) -> Optional[str]:
    """Return 'BUY' / 'SELL' / None for the weekly direction bias.

    Senior-timeframe filter: if the W1 close is below a 20-week EMA we
    treat the weekly bias as SELL and refuse BUY trades that go *against*
    it on the lower timeframes.  Mirror for BUY.  When the weekly bias is
    neutral (EMA-flat or insufficient data) the function returns None and
    the brain leaves the alignment check untouched.
    """
    if bars_1w is None or bars_1w.empty or len(bars_1w) < 21:
        return None
    closes = bars_1w["Close"]
    ema20 = _ema(closes, 20)
    last = float(closes.iloc[-1])
    flat_band = 0.001 * last  # 0.1% — ignore noise around the EMA
    if last > ema20 + flat_band:
        return "BUY"
    if last < ema20 - flat_band:
        return "SELL"
    return None


def m5_momentum_aligned(bars_5m: pd.DataFrame, side: str) -> bool:
    """Quick sanity check: last 6 M5 bars should not be moving against ``side``.

    Used as a soft filter inside the MTF gate.  Returns False if the last
    six M5 closes show a 5-bar slope that contradicts ``side`` by more
    than 0.05% — a tiny but clear short-term momentum mismatch.
    """
    if bars_5m is None or bars_5m.empty or side not in ("BUY", "SELL"):
        return True   # no opinion = don't block
    if len(bars_5m) < 6:
        return True
    closes = bars_5m["Close"].tail(6)
    slope_pct = (float(closes.iloc[-1]) - float(closes.iloc[0])) / float(closes.iloc[0]) * 100.0
    if side == "BUY" and slope_pct < -0.05:
        return False
    if side == "SELL" and slope_pct > +0.05:
        return False
    return True
