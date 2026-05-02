"""
EDGE v14 — робастная система для реального 70%+ WR на 3+ месяца.

Главные улучшения:
- 15 пар (10 + EURGBP, EURAUD, EURCHF, GBPCHF, CADJPY)
- Rolling walk-forward: 5 окон по 1 месяцу для проверки робастности
- Новые индикаторы: Supertrend, MACD divergence, Pivot points, OBV-like flow
- Консервативные фильтры (минимум n=20 на окно)
- Cross-asset из v13 + новые блоки
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import yfinance as yf

from edge_v10 import session_vwap, bbp1000, atr
from edge_v11 import adx, ema_stack, squeeze, momentum_persistence, score_v11 as score_v11_base
from edge_v11 import combined_v11 as combined_v11_base
from edge_v12 import (candlestick_patterns, bb_position, bollinger,
                       regime, level_block, reversal_block, score_v12, combined_v12)
from edge_v13 import cross_asset_score, CONTEXT_MAP, download_context_assets


PAIRS_15 = [
    "EURJPY=X", "GBPJPY=X", "AUDJPY=X", "USDJPY=X", "CADJPY=X",  # JPY-кроссы
    "USDCAD=X", "USDCHF=X",                                       # USD majors
    "AUDUSD=X", "NZDUSD=X", "GBPUSD=X", "EURUSD=X",
    "EURGBP=X", "EURAUD=X", "EURCHF=X", "GBPCHF=X",               # EUR/GBP кроссы
]

# Контекст для новых пар
EXTRA_CONTEXT = {
    "CADJPY=X": {"jpy_asset": "^N225", "base_asset": None, "extra": "CL=F"},
    "EURGBP=X": {"jpy_asset": None, "base_asset": None},
    "EURAUD=X": {"jpy_asset": None, "base_asset": "^GSPC"},  # AUD = risk
    "EURCHF=X": {"jpy_asset": None, "base_asset": None},
    "GBPCHF=X": {"jpy_asset": None, "base_asset": None},
}
CONTEXT_MAP_15 = {**CONTEXT_MAP, **EXTRA_CONTEXT}


# ---------------------------------------------------------------------------
# Новые индикаторы
# ---------------------------------------------------------------------------
def supertrend(df: pd.DataFrame, period: int = 10, mult: float = 3.0) -> pd.Series:
    """Supertrend: +1 = uptrend, -1 = downtrend."""
    a = atr(df, period)
    hl2 = (df["High"] + df["Low"]) / 2
    upper = hl2 + mult * a
    lower = hl2 - mult * a

    trend = pd.Series(1, index=df.index, dtype=int)
    for i in range(1, len(df)):
        if df["Close"].iloc[i] > upper.iloc[i-1]:
            trend.iloc[i] = 1
        elif df["Close"].iloc[i] < lower.iloc[i-1]:
            trend.iloc[i] = -1
        else:
            trend.iloc[i] = trend.iloc[i-1]
            if trend.iloc[i] == 1 and lower.iloc[i] < lower.iloc[i-1]:
                lower.iloc[i] = lower.iloc[i-1]
            if trend.iloc[i] == -1 and upper.iloc[i] > upper.iloc[i-1]:
                upper.iloc[i] = upper.iloc[i-1]
    return trend


def macd(df: pd.DataFrame, fast: int = 12, slow: int = 26, sig: int = 9):
    e1 = df["Close"].ewm(span=fast, adjust=False).mean()
    e2 = df["Close"].ewm(span=slow, adjust=False).mean()
    line = e1 - e2
    signal = line.ewm(span=sig, adjust=False).mean()
    hist = line - signal
    return line, signal, hist


def macd_divergence(df: pd.DataFrame, lookback: int = 20) -> pd.Series:
    """+1 = bullish div (price LL, MACD HL), -1 = bearish div (price HH, MACD LH)."""
    _, _, hist = macd(df)
    out = pd.Series(0, index=df.index, dtype=int)
    closes = df["Close"].values
    h = hist.values
    for i in range(lookback, len(df)):
        prev_low_idx = np.argmin(closes[i-lookback:i]) + (i - lookback)
        prev_high_idx = np.argmax(closes[i-lookback:i]) + (i - lookback)
        # Bull div: текущий close ниже предыдущего low, но MACD hist выше
        if closes[i] < closes[prev_low_idx] and h[i] > h[prev_low_idx]:
            out.iloc[i] = 1
        # Bear div
        elif closes[i] > closes[prev_high_idx] and h[i] < h[prev_high_idx]:
            out.iloc[i] = -1
    return out


def pivot_points(df: pd.DataFrame) -> pd.DataFrame:
    """Daily pivot levels на основе предыдущего дня."""
    day = df.index.floor("D")
    daily = df.groupby(day).agg(H=("High","max"), L=("Low","min"), C=("Close","last"))
    pivot = (daily["H"] + daily["L"] + daily["C"]) / 3
    r1 = 2*pivot - daily["L"]
    s1 = 2*pivot - daily["H"]
    r2 = pivot + (daily["H"] - daily["L"])
    s2 = pivot - (daily["H"] - daily["L"])

    # Forward-fill через бары
    out = pd.DataFrame(index=df.index)
    day_idx = df.index.floor("D")
    out["pivot"] = day_idx.map(pivot.shift(1))
    out["r1"] = day_idx.map(r1.shift(1))
    out["s1"] = day_idx.map(s1.shift(1))
    out["r2"] = day_idx.map(r2.shift(1))
    out["s2"] = day_idx.map(s2.shift(1))
    return out


def pivot_block(df: pd.DataFrame) -> pd.Series:
    """+/- очки за позицию относительно pivot уровней."""
    p = pivot_points(df)
    close = df["Close"]
    a = atr(df, 14)
    out = pd.Series(0, index=df.index, dtype=int)
    # Bullish: цена выше pivot и приближается к R1
    bull_above = (close > p["pivot"]) & (close < p["r1"])
    # Bearish: ниже pivot но выше S1
    bear_below = (close < p["pivot"]) & (close > p["s1"])
    # Strong bull: пробила R1
    super_bull = close > p["r1"]
    super_bear = close < p["s1"]
    out = out.mask(bull_above, 1)
    out = out.mask(bear_below, -1)
    out = out.mask(super_bull, 2)
    out = out.mask(super_bear, -2)
    return out


def obv_flow(df: pd.DataFrame, n: int = 20) -> pd.Series:
    """OBV-like cumulative direction flow (используем pseudo-volume = ATR)."""
    direction = np.sign(df["Close"].diff()).fillna(0)
    vol_proxy = (df["High"] - df["Low"])
    obv = (direction * vol_proxy).cumsum()
    obv_ma = obv.rolling(n).mean()
    # Подъём OBV над средней = bull
    return np.sign(obv - obv_ma)


# ---------------------------------------------------------------------------
# v14 score
# ---------------------------------------------------------------------------
def score_v14(df_15m: pd.DataFrame, df_1h: pd.DataFrame, df_4h: pd.DataFrame,
               pair: str, ctx: dict):
    """Расширяет v13 новыми блоками."""
    # Базовый v12 combined
    s15 = score_v12(df_15m)
    s1h = score_v12(df_1h)
    s4h = score_v12(df_4h)
    base = combined_v12(s15, s1h, s4h).dropna()

    # Cross-asset из v13
    ca = cross_asset_score(df_15m, pair, ctx)
    ca = ca.reindex(base.index, method="ffill").fillna(0)
    base["cross_asset"] = ca

    # Новые блоки на 15m и 1h
    st_15m = supertrend(df_15m, 10, 3.0).reindex(base.index, method="ffill")
    st_1h = supertrend(df_1h, 10, 3.0).reindex(base.index, method="ffill")
    st_4h = supertrend(df_4h, 10, 3.0).reindex(base.index, method="ffill")
    # Supertrend confluence: 3 из 3 одного знака → ±3
    st_sum = st_15m + st_1h + st_4h
    base["supertrend"] = st_sum.fillna(0)

    # MACD divergence
    macd_15m = macd_divergence(df_15m, 20).reindex(base.index, method="ffill").fillna(0)
    macd_1h = macd_divergence(df_1h, 20).reindex(base.index, method="ffill").fillna(0)
    base["macd_div"] = (macd_15m * 2 + macd_1h).fillna(0)

    # Pivot block
    pb_15m = pivot_block(df_15m).reindex(base.index, method="ffill").fillna(0)
    base["pivot"] = pb_15m

    # OBV flow
    obv_15m = obv_flow(df_15m, 20).reindex(base.index, method="ffill").fillna(0)
    obv_1h = obv_flow(df_1h, 20).reindex(base.index, method="ffill").fillna(0)
    base["obv"] = (obv_15m + obv_1h).fillna(0)

    # Финальный score: v13 + новые блоки
    base["score_v14"] = (
        base["score_v12"]
        + base["cross_asset"]
        + base["supertrend"]      # ±3
        + base["macd_div"]        # ±3
        + base["pivot"]           # ±2
        + base["obv"]             # ±2
    )

    # Confluence: считаем сколько новых блоков согласны с направлением score
    s_sign = np.sign(base["score_v14"])
    confs = (
        (np.sign(base["supertrend"]) == s_sign).astype(int) +
        (np.sign(base["macd_div"]) == s_sign).astype(int) +
        (np.sign(base["pivot"]) == s_sign).astype(int) +
        (np.sign(base["obv"]) == s_sign).astype(int) +
        (np.sign(base["cross_asset"]) == s_sign).astype(int)
    )
    base["v14_confluence"] = confs  # 0..5

    return base


def backtest_v14(
    scores: pd.DataFrame,
    expiry_hours: int,
    min_abs_score: int,
    min_v14_confluence: int = 0,
    min_old_confluence: int = 0,
    session_hours_utc: tuple | None = None,
) -> pd.DataFrame:
    bars_offset = expiry_hours * 4
    closes = scores["close"].values
    score = scores["score_v14"].values
    v14_conf = scores["v14_confluence"].values
    old_conf = scores["confluence"].values
    times = scores.index

    trades = []
    for i in range(len(scores) - bars_offset):
        s = score[i]
        if abs(s) < min_abs_score: continue
        if v14_conf[i] < min_v14_confluence: continue
        if old_conf[i] < min_old_confluence: continue
        if session_hours_utc:
            h = times[i].hour
            lo, hi = session_hours_utc
            if not (lo <= h <= hi): continue
        direction = 1 if s > 0 else -1
        entry = closes[i]
        exit_ = closes[i + bars_offset]
        win = (exit_ > entry and direction == 1) or (exit_ < entry and direction == -1)
        trades.append({
            "time": times[i], "score": s, "v14_conf": v14_conf[i],
            "direction": "BUY" if direction == 1 else "SELL",
            "entry": entry, "exit": exit_, "win": win,
        })
    return pd.DataFrame(trades)


# ---------------------------------------------------------------------------
# Rolling walk-forward
# ---------------------------------------------------------------------------
def rolling_walk_forward_windows(scores: pd.DataFrame, n_windows: int = 4,
                                   train_days: int = 30, test_days: int = 8):
    """
    Возвращает список (train_df, test_df) с разными временными окнами.
    n_windows: сколько раз сдвигать; для 60 дней 15m данных можно ~4 окон.
    """
    if not isinstance(scores.index, pd.DatetimeIndex):
        return []
    end = scores.index.max()
    windows = []
    for w in range(n_windows):
        test_end = end - pd.Timedelta(days=test_days * w)
        test_start = test_end - pd.Timedelta(days=test_days)
        train_end = test_start
        train_start = train_end - pd.Timedelta(days=train_days)
        if train_start < scores.index.min():
            break
        train = scores[(scores.index >= train_start) & (scores.index < train_end)]
        test = scores[(scores.index >= test_start) & (scores.index < test_end)]
        if len(train) > 100 and len(test) > 50:
            windows.append((train, test, test_start.date(), test_end.date()))
    return windows
