"""Технические индикаторы — все на реальных свечах, без заглушек.

RSI(14), EMA(20/50/200), ATR(14), Bollinger %B, Momentum, CEI, OFI, VWAP, BBP.
"""
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


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """ATR(14) — Average True Range."""
    high, low, close = df["High"], df["Low"], df["Close"]
    tr = pd.concat(
        [
            (high - low),
            (high - close.shift()).abs(),
            (low - close.shift()).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return tr.rolling(period, min_periods=period).mean()


def bollinger_pct_b(close: pd.Series, period: int = 20, std: float = 2.0) -> pd.Series:
    """%B = (close - lower) / (upper - lower). Фиксируем NaN → 0.5 (нейтрально)."""
    ma = close.rolling(period, min_periods=period).mean()
    sd = close.rolling(period, min_periods=period).std()
    upper = ma + std * sd
    lower = ma - std * sd
    width = (upper - lower).replace(0.0, np.nan)
    pct = (close - lower) / width
    return pct.fillna(0.5)


def momentum(close: pd.Series, lookback: int = 5) -> pd.Series:
    return (close - close.shift(lookback)) / close.shift(lookback) * 100.0


def cei(df: pd.DataFrame, n: int = 10) -> float:
    """Candle Efficiency Index за последние n свечей. 0..100."""
    if len(df) < n:
        return 0.0
    last = df.tail(n)
    body = (last["Close"] - last["Open"]).abs()
    range_ = (last["High"] - last["Low"]).replace(0.0, np.nan)
    eff = (body / range_).fillna(0.0)
    return float(eff.mean() * 100.0)


def ofi(df: pd.DataFrame, n: int = 10) -> float:
    """Order Flow Imbalance: (бычьих закрытий − медвежьих) / n. От -1 до +1."""
    if len(df) < n:
        return 0.0
    last = df.tail(n)
    bull = int((last["Close"] > last["Open"]).sum())
    bear = int((last["Close"] < last["Open"]).sum())
    return (bull - bear) / float(n)


def vwap_session(df: pd.DataFrame) -> pd.Series:
    """VWAP в рамках текущей сессии (день).

    Если в df нет колонки Volume или Volume=0 — возвращаем typical-price-MA20
    как реалистичный фоллбэк (FX-дата с Yahoo часто без объёма).
    """
    typical = (df["High"] + df["Low"] + df["Close"]) / 3.0
    vol = df.get("Volume", pd.Series(0.0, index=df.index))
    if vol.sum() <= 0:
        # фоллбэк — typical-price MA(20)
        return typical.rolling(20, min_periods=1).mean()
    cum_pv = (typical * vol).cumsum()
    cum_v = vol.cumsum().replace(0.0, np.nan)
    return (cum_pv / cum_v).fillna(typical)


def bbp(close: pd.Series, length: int = 1000) -> pd.Series:
    """Bull-Bear Power 1000:  EMA(close, 13) → close − EMA13.
    Урезаем length до фактической длины серии.
    """
    span = min(length, max(13, len(close) // 4))
    return close - close.ewm(span=span, adjust=False).mean()


def all_indicators(df: pd.DataFrame) -> dict[str, float]:
    """Скан всех индикаторов на последнем баре. Возвращает плоский dict для UI/агентов."""
    if df is None or df.empty or len(df) < 30:
        return {}
    close = df["Close"]
    out: dict[str, float] = {}
    out["rsi14"] = float(rsi(close, 14).iloc[-1])
    out["ema20"] = float(ema(close, 20).iloc[-1])
    out["ema50"] = float(ema(close, 50).iloc[-1])
    out["ema200"] = float(ema(close, 200).iloc[-1]) if len(close) >= 200 else float(ema(close, len(close) - 1).iloc[-1])
    out["atr14"] = float(atr(df, 14).iloc[-1])
    out["bb_pct"] = float(bollinger_pct_b(close, 20, 2.0).iloc[-1])
    out["mom5"] = float(momentum(close, 5).iloc[-1])
    out["cei10"] = cei(df, 10)
    out["ofi10"] = ofi(df, 10)
    out["vwap"] = float(vwap_session(df).iloc[-1])
    out["bbp"] = float(bbp(close, 1000).iloc[-1])
    out["close"] = float(close.iloc[-1])
    return out
