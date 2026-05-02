"""Технические индикаторы — все на реальных свечах, без заглушек.

RSI(14), EMA(20/50/200), ATR(14), Bollinger %B, Momentum, CEI, OFI, VWAP, BBP,
MACD, Stochastic, ADX, Williams %R, Ichimoku Cloud.
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


def macd(close: pd.Series, fast: int = 12, slow: int = 26,
         signal_period: int = 9) -> tuple[pd.Series, pd.Series, pd.Series]:
    """MACD: (macd_line, signal_line, histogram).
    macd_line = EMA(close, fast) − EMA(close, slow)
    signal_line = EMA(macd_line, signal_period)
    histogram = macd_line − signal_line
    """
    ema_fast = close.ewm(span=fast, adjust=False).mean()
    ema_slow = close.ewm(span=slow, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal_period, adjust=False).mean()
    histogram = macd_line - signal_line
    return macd_line, signal_line, histogram


def stochastic(df: pd.DataFrame, k_period: int = 14,
               d_period: int = 3) -> tuple[pd.Series, pd.Series]:
    """Stochastic Oscillator: (%K, %D), 0..100. Фоллбэк 50 при NaN."""
    low_min = df["Low"].rolling(k_period).min()
    high_max = df["High"].rolling(k_period).max()
    denom = (high_max - low_min).replace(0.0, np.nan)
    k = 100.0 * (df["Close"] - low_min) / denom
    d = k.rolling(d_period).mean()
    return k.fillna(50.0), d.fillna(50.0)


def adx_indicator(df: pd.DataFrame, period: int = 14
                  ) -> tuple[pd.Series, pd.Series, pd.Series]:
    """ADX (+DI, -DI). Возвращает (adx, plus_di, minus_di).
    ADX > 25 — выраженный тренд; ADX < 15 — флэт.
    """
    high, low, close = df["High"], df["Low"], df["Close"]
    plus_dm = high.diff().clip(lower=0.0)
    minus_dm = (-low.diff()).clip(lower=0.0)
    # обнуляем направление с меньшим вкладом, как в классическом ADX
    plus_dm_clean = plus_dm.where(plus_dm > minus_dm, 0.0)
    minus_dm_clean = minus_dm.where(minus_dm > plus_dm, 0.0)
    tr = pd.concat(
        [
            (high - low),
            (high - close.shift()).abs(),
            (low - close.shift()).abs(),
        ],
        axis=1,
    ).max(axis=1)
    atr_val = tr.ewm(span=period, adjust=False).mean().replace(0.0, np.nan)
    plus_di = 100.0 * plus_dm_clean.ewm(span=period, adjust=False).mean() / atr_val
    minus_di = 100.0 * minus_dm_clean.ewm(span=period, adjust=False).mean() / atr_val
    dx_denom = (plus_di + minus_di).replace(0.0, np.nan)
    dx = 100.0 * (plus_di - minus_di).abs() / dx_denom
    adx_val = dx.ewm(span=period, adjust=False).mean()
    return adx_val.fillna(0.0), plus_di.fillna(0.0), minus_di.fillna(0.0)


def williams_r(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Williams %R: -100..0. >−20 — перекупленность, <−80 — перепроданность."""
    high_max = df["High"].rolling(period).max()
    low_min = df["Low"].rolling(period).min()
    denom = (high_max - low_min).replace(0.0, np.nan)
    wr = -100.0 * (high_max - df["Close"]) / denom
    return wr.fillna(-50.0)


def ichimoku(df: pd.DataFrame, tenkan_period: int = 9, kijun_period: int = 26,
             senkou_b_period: int = 52
             ) -> tuple[pd.Series, pd.Series, pd.Series, pd.Series]:
    """Ichimoku Cloud: (tenkan, kijun, senkou_a, senkou_b)."""
    high, low, close = df["High"], df["Low"], df["Close"]
    tenkan = (high.rolling(tenkan_period).max() + low.rolling(tenkan_period).min()) / 2.0
    kijun = (high.rolling(kijun_period).max() + low.rolling(kijun_period).min()) / 2.0
    senkou_a = (tenkan + kijun) / 2.0
    senkou_b = (high.rolling(senkou_b_period).max() + low.rolling(senkou_b_period).min()) / 2.0
    return (
        tenkan.fillna(close),
        kijun.fillna(close),
        senkou_a.fillna(close),
        senkou_b.fillna(close),
    )


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

    # MACD
    macd_line, macd_signal, macd_hist = macd(close)
    out["macd_line"] = float(macd_line.iloc[-1])
    out["macd_signal"] = float(macd_signal.iloc[-1])
    out["macd_hist"] = float(macd_hist.iloc[-1])
    out["macd_prev_hist"] = float(macd_hist.iloc[-2]) if len(macd_hist) > 1 else 0.0

    # Stochastic
    stoch_k, stoch_d = stochastic(df)
    out["stoch_k"] = float(stoch_k.iloc[-1])
    out["stoch_d"] = float(stoch_d.iloc[-1])

    # ADX
    adx_val, plus_di, minus_di = adx_indicator(df)
    out["adx"] = float(adx_val.iloc[-1])
    out["plus_di"] = float(plus_di.iloc[-1])
    out["minus_di"] = float(minus_di.iloc[-1])

    # Williams %R
    out["williams_r"] = float(williams_r(df).iloc[-1])

    # Ichimoku Cloud
    tenkan, kijun, senkou_a, senkou_b = ichimoku(df)
    out["ichimoku_tenkan"] = float(tenkan.iloc[-1])
    out["ichimoku_kijun"] = float(kijun.iloc[-1])
    out["ichimoku_senkou_a"] = float(senkou_a.iloc[-1])
    out["ichimoku_senkou_b"] = float(senkou_b.iloc[-1])
    cloud_top = max(out["ichimoku_senkou_a"], out["ichimoku_senkou_b"])
    cloud_bot = min(out["ichimoku_senkou_a"], out["ichimoku_senkou_b"])
    out["ichimoku_above_cloud"] = float(out["close"] > cloud_top)
    out["ichimoku_below_cloud"] = float(out["close"] < cloud_bot)

    return out
