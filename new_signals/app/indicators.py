"""Technical indicators - all computed on real candle data."""
from __future__ import annotations

import numpy as np
import pandas as pd


def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    up = delta.where(delta > 0, 0.0).rolling(period, min_periods=period).mean()
    dn = (-delta.where(delta < 0, 0.0)).rolling(period, min_periods=period).mean()
    rs = up / dn.replace(0.0, np.nan)
    return (100.0 - 100.0 / (1.0 + rs)).fillna(50.0)


def ema(close: pd.Series, span: int) -> pd.Series:
    return close.ewm(span=span, adjust=False).mean()


def sma(close: pd.Series, period: int) -> pd.Series:
    return close.rolling(period, min_periods=period).mean()


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    high, low, close = df["High"], df["Low"], df["Close"]
    tr = pd.concat([
        high - low,
        (high - close.shift()).abs(),
        (low - close.shift()).abs(),
    ], axis=1).max(axis=1)
    return tr.rolling(period, min_periods=period).mean()


def bollinger_bands(close: pd.Series, period: int = 20, std: float = 2.0):
    ma = close.rolling(period, min_periods=period).mean()
    sd = close.rolling(period, min_periods=period).std()
    upper = ma + std * sd
    lower = ma - std * sd
    pct_b = (close - lower) / (upper - lower).replace(0.0, np.nan)
    return ma, upper, lower, pct_b.fillna(0.5)


def macd(close: pd.Series, fast: int = 12, slow: int = 26, signal_period: int = 9):
    ema_fast = close.ewm(span=fast, adjust=False).mean()
    ema_slow = close.ewm(span=slow, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal_period, adjust=False).mean()
    histogram = macd_line - signal_line
    return macd_line, signal_line, histogram


def stochastic(df: pd.DataFrame, k_period: int = 14, d_period: int = 3):
    low_min = df["Low"].rolling(k_period).min()
    high_max = df["High"].rolling(k_period).max()
    denom = (high_max - low_min).replace(0.0, np.nan)
    k = 100.0 * (df["Close"] - low_min) / denom
    d = k.rolling(d_period).mean()
    return k.fillna(50.0), d.fillna(50.0)


def adx(df: pd.DataFrame, period: int = 14):
    high, low, close = df["High"], df["Low"], df["Close"]
    plus_dm = high.diff().clip(lower=0.0)
    minus_dm = (-low.diff()).clip(lower=0.0)
    plus_dm = plus_dm.where(plus_dm > minus_dm, 0.0)
    minus_dm = minus_dm.where(minus_dm > plus_dm, 0.0)

    atr_val = atr(df, period)
    atr_safe = atr_val.replace(0.0, np.nan)

    plus_di = 100.0 * plus_dm.rolling(period).mean() / atr_safe
    minus_di = 100.0 * minus_dm.rolling(period).mean() / atr_safe

    dx_denom = (plus_di + minus_di).replace(0.0, np.nan)
    dx = 100.0 * (plus_di - minus_di).abs() / dx_denom
    adx_val = dx.rolling(period).mean()

    return adx_val.fillna(0.0), plus_di.fillna(0.0), minus_di.fillna(0.0)


def williams_r(df: pd.DataFrame, period: int = 14) -> pd.Series:
    high_max = df["High"].rolling(period).max()
    low_min = df["Low"].rolling(period).min()
    denom = (high_max - low_min).replace(0.0, np.nan)
    wr = -100.0 * (high_max - df["Close"]) / denom
    return wr.fillna(-50.0)


def ichimoku(df: pd.DataFrame):
    high, low = df["High"], df["Low"]
    tenkan = (high.rolling(9).max() + low.rolling(9).min()) / 2
    kijun = (high.rolling(26).max() + low.rolling(26).min()) / 2
    senkou_a = (tenkan + kijun) / 2
    senkou_b = (high.rolling(52).max() + low.rolling(52).min()) / 2
    close = df["Close"]
    above_cloud = (close > senkou_a) & (close > senkou_b)
    below_cloud = (close < senkou_a) & (close < senkou_b)
    return tenkan, kijun, senkou_a, senkou_b, above_cloud, below_cloud


def momentum(close: pd.Series, lookback: int = 5) -> pd.Series:
    return (close - close.shift(lookback)) / close.shift(lookback) * 100.0


def vwap(df: pd.DataFrame) -> pd.Series:
    typical = (df["High"] + df["Low"] + df["Close"]) / 3.0
    vol = df.get("Volume", pd.Series(0.0, index=df.index))
    if vol.sum() <= 0:
        return typical.rolling(20, min_periods=1).mean()
    cum_pv = (typical * vol).cumsum()
    cum_v = vol.cumsum().replace(0.0, np.nan)
    return (cum_pv / cum_v).fillna(typical)


def compute_all(df: pd.DataFrame) -> dict | None:
    if df.empty or len(df) < 30:
        return None

    close = df["Close"]
    last_close = float(close.iloc[-1])

    rsi_val = rsi(close)
    ema20 = ema(close, 20)
    ema50 = ema(close, 50)
    ema200 = ema(close, 200)
    bb_ma, bb_upper, bb_lower, bb_pct = bollinger_bands(close)
    macd_line, macd_signal, macd_hist = macd(close)
    stoch_k, stoch_d = stochastic(df)
    adx_val, plus_di, minus_di = adx(df)
    wr = williams_r(df)
    tenkan, kijun, senkou_a, senkou_b, above_cloud, below_cloud = ichimoku(df)
    mom = momentum(close)
    vwap_val = vwap(df)

    return {
        "close": last_close,
        "rsi14": float(rsi_val.iloc[-1]),
        "ema20": float(ema20.iloc[-1]),
        "ema50": float(ema50.iloc[-1]),
        "ema200": float(ema200.iloc[-1]),
        "bb_pct": float(bb_pct.iloc[-1]),
        "bb_upper": float(bb_upper.iloc[-1]),
        "bb_lower": float(bb_lower.iloc[-1]),
        "macd_line": float(macd_line.iloc[-1]),
        "macd_signal": float(macd_signal.iloc[-1]),
        "macd_hist": float(macd_hist.iloc[-1]),
        "macd_prev_hist": float(macd_hist.iloc[-2]) if len(macd_hist) > 1 else 0.0,
        "stoch_k": float(stoch_k.iloc[-1]),
        "stoch_d": float(stoch_d.iloc[-1]),
        "adx": float(adx_val.iloc[-1]),
        "plus_di": float(plus_di.iloc[-1]),
        "minus_di": float(minus_di.iloc[-1]),
        "williams_r": float(wr.iloc[-1]),
        "ichimoku_tenkan": float(tenkan.iloc[-1]) if not pd.isna(tenkan.iloc[-1]) else last_close,
        "ichimoku_kijun": float(kijun.iloc[-1]) if not pd.isna(kijun.iloc[-1]) else last_close,
        "ichimoku_above_cloud": bool(above_cloud.iloc[-1]) if not pd.isna(above_cloud.iloc[-1]) else False,
        "ichimoku_below_cloud": bool(below_cloud.iloc[-1]) if not pd.isna(below_cloud.iloc[-1]) else False,
        "momentum": float(mom.iloc[-1]) if not pd.isna(mom.iloc[-1]) else 0.0,
        "vwap": float(vwap_val.iloc[-1]),
    }
