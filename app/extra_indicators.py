"""Extra technical indicators — broadens the analysis surface so the
brain can find genuinely confluent setups (the user-stated requirement
"расширить анализ — больше шансов найти настоящие 80% в каждом цикле").

These are *in addition to* ``app/indicators.py``.  The existing module
is left untouched so back-tests, the 15-block analyser and the legacy
voting logic keep producing identical numbers.  The new indicators are
consumed by ``app/confluence.py`` which folds them into a single multi-
timeframe confluence score for the brain.

All functions operate on real Yahoo Finance OHLCV ``pandas`` frames —
no simulators, no fake data, in keeping with the repo invariant.
Every function returns a finite scalar (last bar value) or ``None``
when the input frame is too short to compute meaningfully.
"""
from __future__ import annotations

from typing import Optional

import numpy as np
import pandas as pd


# ─── Helpers ─────────────────────────────────────────────────────────


def _safe_last(s: pd.Series) -> Optional[float]:
    """Return the last finite scalar in ``s`` or ``None`` if empty/NaN."""
    if s is None or len(s) == 0:
        return None
    val = s.iloc[-1]
    if pd.isna(val):
        return None
    return float(val)


def _true_range(df: pd.DataFrame) -> pd.Series:
    high, low, close = df["High"], df["Low"], df["Close"]
    return pd.concat(
        [
            high - low,
            (high - close.shift()).abs(),
            (low - close.shift()).abs(),
        ],
        axis=1,
    ).max(axis=1)


# ─── Money Flow Index (volume-weighted RSI) ──────────────────────────


def mfi(df: pd.DataFrame, period: int = 14) -> Optional[float]:
    """Money Flow Index over ``period`` bars.

    MFI is "RSI with volume" — directional flow weighted by traded
    volume.  Values >80 = overbought, <20 = oversold.  When ``Volume``
    is missing or all-zero (some yfinance crosses), MFI gracefully
    degrades to plain RSI on typical price.
    """
    if df is None or len(df) < period + 2:
        return None
    typical = (df["High"] + df["Low"] + df["Close"]) / 3.0
    vol = df.get("Volume", pd.Series(0.0, index=df.index))
    if vol.sum() <= 0:
        # Fall back to typical-price momentum so MFI still produces a
        # bounded scalar in [0, 100] rather than NaN.
        delta = typical.diff()
        up = delta.where(delta > 0, 0.0).rolling(period, min_periods=period).mean()
        dn = (-delta.where(delta < 0, 0.0)).rolling(period, min_periods=period).mean()
        rs = up / dn.replace(0.0, np.nan)
        return _safe_last((100.0 - 100.0 / (1.0 + rs)).fillna(50.0))
    raw_flow = typical * vol
    pos_flow = raw_flow.where(typical > typical.shift(), 0.0)
    neg_flow = raw_flow.where(typical < typical.shift(), 0.0)
    pos_sum = pos_flow.rolling(period, min_periods=period).sum()
    neg_sum = neg_flow.rolling(period, min_periods=period).sum()
    mr = pos_sum / neg_sum.replace(0.0, np.nan)
    return _safe_last((100.0 - 100.0 / (1.0 + mr)).fillna(50.0))


# ─── Commodity Channel Index ─────────────────────────────────────────


def cci(df: pd.DataFrame, period: int = 20) -> Optional[float]:
    """CCI on typical price — standard 20-period.

    Values >+100 = strong bullish trend; <-100 = strong bearish trend;
    [-100, +100] = ranging market.  CCI complements RSI by being
    unbounded — it flags genuine breakouts that an oversold/overbought
    oscillator would silence.
    """
    if df is None or len(df) < period + 2:
        return None
    typical = (df["High"] + df["Low"] + df["Close"]) / 3.0
    ma = typical.rolling(period, min_periods=period).mean()
    md = (typical - ma).abs().rolling(period, min_periods=period).mean()
    cci_val = (typical - ma) / (0.015 * md.replace(0.0, np.nan))
    return _safe_last(cci_val.fillna(0.0))


# ─── On-Balance Volume (cumulative) ──────────────────────────────────


def obv_slope(df: pd.DataFrame, lookback: int = 20) -> Optional[float]:
    """OBV slope over ``lookback`` bars, normalised to [-1, +1].

    Returns the sign-and-magnitude of accumulation/distribution: +1.0
    means OBV climbed monotonically (heavy buying), -1.0 means
    monotonic selling.  Used as a directional volume confirmation —
    a BUY signal with positive OBV slope is much higher-conviction.
    """
    if df is None or len(df) < lookback + 2:
        return None
    close = df["Close"]
    vol = df.get("Volume", pd.Series(0.0, index=df.index))
    if vol.sum() <= 0:
        # No volume → use sign-of-close-changes as a proxy so OBV is
        # always defined.  This means crosses without volume contribute
        # a weak directional confirmation rather than nothing.
        sign = np.sign(close.diff().fillna(0.0))
        proxy = sign.cumsum()
        recent = proxy.tail(lookback)
        rng = recent.max() - recent.min()
        if rng <= 0:
            return 0.0
        slope = (recent.iloc[-1] - recent.iloc[0]) / rng
        return float(max(-1.0, min(1.0, slope)))
    direction = np.sign(close.diff().fillna(0.0))
    obv = (direction * vol).cumsum()
    recent = obv.tail(lookback)
    rng = recent.max() - recent.min()
    if rng <= 0:
        return 0.0
    slope = (recent.iloc[-1] - recent.iloc[0]) / rng
    return float(max(-1.0, min(1.0, slope)))


# ─── Supertrend ──────────────────────────────────────────────────────


def supertrend(
    df: pd.DataFrame,
    period: int = 10,
    multiplier: float = 3.0,
) -> Optional[dict]:
    """Supertrend on H1/H4.

    Returns a dict with ``direction`` (+1 = uptrend, -1 = downtrend)
    and ``level`` (the active band level).  Supertrend is one of the
    most popular and battle-tested trend-following indicators among
    retail and institutional algos.  It is computed from ATR-buffered
    HL2 — flips only on a clean break of the band.
    """
    if df is None or len(df) < period + 5:
        return None
    high, low, close = df["High"], df["Low"], df["Close"]
    hl2 = (high + low) / 2.0
    tr = _true_range(df)
    atr = tr.rolling(period, min_periods=period).mean()
    upper = hl2 + multiplier * atr
    lower = hl2 - multiplier * atr
    final_upper = upper.copy()
    final_lower = lower.copy()
    direction = pd.Series(index=df.index, dtype=float)
    direction.iloc[0] = 1.0
    for i in range(1, len(df)):
        prev_close = close.iloc[i - 1]
        prev_upper = final_upper.iloc[i - 1]
        prev_lower = final_lower.iloc[i - 1]
        cur_upper = upper.iloc[i]
        cur_lower = lower.iloc[i]
        # Warm-up: ATR is NaN until `period` bars accumulate.  Treat
        # those bars as "no band yet" and copy through the current
        # raw values rather than letting NaN propagate forever.
        if pd.isna(prev_upper) or pd.isna(cur_upper):
            final_upper.iloc[i] = cur_upper
        elif cur_upper < prev_upper or prev_close > prev_upper:
            final_upper.iloc[i] = cur_upper
        else:
            final_upper.iloc[i] = prev_upper
        if pd.isna(prev_lower) or pd.isna(cur_lower):
            final_lower.iloc[i] = cur_lower
        elif cur_lower > prev_lower or prev_close < prev_lower:
            final_lower.iloc[i] = cur_lower
        else:
            final_lower.iloc[i] = prev_lower
        prev_dir = direction.iloc[i - 1]
        cur_close = close.iloc[i]
        cur_final_upper = final_upper.iloc[i]
        cur_final_lower = final_lower.iloc[i]
        if pd.isna(cur_final_upper) or pd.isna(cur_final_lower):
            # Bands not warm yet — preserve prior direction.
            direction.iloc[i] = prev_dir if not pd.isna(prev_dir) else 1.0
        elif prev_dir > 0:
            direction.iloc[i] = -1.0 if cur_close < cur_final_lower else 1.0
        else:
            direction.iloc[i] = 1.0 if cur_close > cur_final_upper else -1.0
    last_dir = direction.iloc[-1]
    if pd.isna(last_dir):
        return None
    level = final_lower.iloc[-1] if last_dir > 0 else final_upper.iloc[-1]
    if pd.isna(level):
        return None
    return {"direction": int(last_dir), "level": float(level)}


# ─── Vortex Indicator ────────────────────────────────────────────────


def vortex(df: pd.DataFrame, period: int = 14) -> Optional[dict]:
    """Vortex Indicator (VI+ / VI-).

    Captures trend direction via the relationship between two non-
    overlapping moving sums of directional movement.  ``VI+ > VI-``
    confirms an uptrend; the larger the divergence between them, the
    stronger the trend.
    """
    if df is None or len(df) < period + 2:
        return None
    high, low, close = df["High"], df["Low"], df["Close"]
    tr = _true_range(df)
    vm_plus = (high - low.shift()).abs()
    vm_minus = (low - high.shift()).abs()
    tr_sum = tr.rolling(period, min_periods=period).sum().replace(0.0, np.nan)
    vi_plus = vm_plus.rolling(period, min_periods=period).sum() / tr_sum
    vi_minus = vm_minus.rolling(period, min_periods=period).sum() / tr_sum
    vp = _safe_last(vi_plus.fillna(0.0))
    vm = _safe_last(vi_minus.fillna(0.0))
    if vp is None or vm is None:
        return None
    return {"vi_plus": vp, "vi_minus": vm, "spread": vp - vm}


# ─── Rate of Change ──────────────────────────────────────────────────


def roc(df: pd.DataFrame, period: int = 10) -> Optional[float]:
    """Rate of Change (percent) over ``period`` bars."""
    if df is None or len(df) < period + 2:
        return None
    close = df["Close"]
    prev = close.shift(period)
    val = ((close - prev) / prev.replace(0.0, np.nan)) * 100.0
    return _safe_last(val.fillna(0.0))


# ─── Bollinger / Keltner Squeeze (volatility expansion gate) ─────────


def squeeze_momentum(
    df: pd.DataFrame,
    bb_period: int = 20,
    bb_std: float = 2.0,
    kc_period: int = 20,
    kc_mult: float = 1.5,
) -> Optional[dict]:
    """Bollinger / Keltner squeeze + linear-regression momentum.

    Returns:

    * ``squeeze_on``    — Bollinger bands inside Keltner channel
                          (compressed volatility; coiled-spring setup)
    * ``squeeze_off``   — bands have just expanded out of Keltner
                          (release; volatility burst beginning)
    * ``momentum``      — sign + magnitude of recent linear-regression
                          slope on close minus the (HH+LL)/2 midline

    For binary 5h options, ``squeeze_off`` + aligned momentum is a
    textbook entry condition — high-conviction expansion.
    """
    if df is None or len(df) < max(bb_period, kc_period) + 5:
        return None
    close = df["Close"]
    sma = close.rolling(bb_period, min_periods=bb_period).mean()
    sd = close.rolling(bb_period, min_periods=bb_period).std()
    bb_upper = sma + bb_std * sd
    bb_lower = sma - bb_std * sd
    atr = _true_range(df).rolling(kc_period, min_periods=kc_period).mean()
    kc_upper = sma + kc_mult * atr
    kc_lower = sma - kc_mult * atr
    squeeze_on = (bb_lower.iloc[-1] > kc_lower.iloc[-1]) and (
        bb_upper.iloc[-1] < kc_upper.iloc[-1]
    )
    squeeze_off_now = not squeeze_on
    # Was the prior bar in squeeze?  Then this bar is the release.
    prev_on = False
    if len(close) >= 2:
        prev_on = (bb_lower.iloc[-2] > kc_lower.iloc[-2]) and (
            bb_upper.iloc[-2] < kc_upper.iloc[-2]
        )
    just_released = squeeze_off_now and prev_on
    # Linear-regression momentum on close vs midline of recent range.
    lookback = min(20, len(df) - 1)
    window = close.tail(lookback)
    if lookback < 5 or window.empty:
        return None
    hh = df["High"].tail(lookback).max()
    ll = df["Low"].tail(lookback).min()
    mid = (hh + ll) / 2.0
    detrended = window - mid
    x = np.arange(len(detrended), dtype=float)
    if len(x) < 2 or detrended.std() == 0:
        slope = 0.0
    else:
        slope = float(np.polyfit(x, detrended.to_numpy(), 1)[0])
    return {
        "squeeze_on": bool(squeeze_on),
        "squeeze_just_released": bool(just_released),
        "momentum": slope,
    }


# ─── Donchian Channel (high/low breakout) ────────────────────────────


def donchian(df: pd.DataFrame, period: int = 20) -> Optional[dict]:
    """Donchian channel breakout — Turtle-style trend follower.

    Returns the channel high/low and whether the latest close has
    broken out of the prior channel (excludes the current bar so a
    breakout is only valid against past structure).
    """
    if df is None or len(df) < period + 2:
        return None
    prior_high = df["High"].shift().rolling(period, min_periods=period).max()
    prior_low = df["Low"].shift().rolling(period, min_periods=period).min()
    close = df["Close"]
    hh = _safe_last(prior_high)
    ll = _safe_last(prior_low)
    last_close = _safe_last(close)
    if hh is None or ll is None or last_close is None:
        return None
    return {
        "high": hh,
        "low": ll,
        "breakout_up": last_close > hh,
        "breakout_down": last_close < ll,
    }


# ─── Compute-all convenience for confluence module ───────────────────


def compute_extras(df: pd.DataFrame) -> Optional[dict]:
    """Bundle of new indicators on a single timeframe.

    Returns ``None`` if the frame is too short for any of the
    indicators to produce a meaningful value (matches the existing
    ``indicators.compute_all`` contract).
    """
    if df is None or df.empty or len(df) < 35:
        return None
    return {
        "mfi": mfi(df),
        "cci": cci(df),
        "obv_slope": obv_slope(df),
        "supertrend": supertrend(df),
        "vortex": vortex(df),
        "roc": roc(df),
        "squeeze": squeeze_momentum(df),
        "donchian": donchian(df),
    }
