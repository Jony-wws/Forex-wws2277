"""
EDGE v11 — расширенная система с фильтрами режима и сессии.
Цель: 70%+ Win Rate на out-of-sample.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import yfinance as yf

from edge_v10 import score_timeframe, combined_score, atr, session_vwap, bbp1000


# ---------------------------------------------------------------------------
# Дополнительные индикаторы
# ---------------------------------------------------------------------------
def adx(df: pd.DataFrame, n: int = 14) -> pd.Series:
    """ADX — Average Directional Index. >25 = трендовый режим."""
    high, low, close = df["High"], df["Low"], df["Close"]
    plus_dm = high.diff()
    minus_dm = -low.diff()
    plus_dm = plus_dm.where((plus_dm > minus_dm) & (plus_dm > 0), 0.0)
    minus_dm = minus_dm.where((minus_dm > plus_dm.shift(0).fillna(0)) & (minus_dm > 0), 0.0)

    tr = pd.concat([
        high - low,
        (high - close.shift()).abs(),
        (low - close.shift()).abs()
    ], axis=1).max(axis=1)
    atr_n = tr.rolling(n).mean()

    plus_di = 100 * plus_dm.rolling(n).sum() / atr_n.replace(0, np.nan)
    minus_di = 100 * minus_dm.rolling(n).sum() / atr_n.replace(0, np.nan)
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    return dx.rolling(n).mean()


def ema_stack(df: pd.DataFrame) -> pd.Series:
    """+1 если EMA8>21>50>200 (бычий стек), -1 если наоборот, 0 иначе."""
    e8 = df["Close"].ewm(span=8, adjust=False).mean()
    e21 = df["Close"].ewm(span=21, adjust=False).mean()
    e50 = df["Close"].ewm(span=50, adjust=False).mean()
    e200 = df["Close"].ewm(span=200, adjust=False).mean()
    bull = (e8 > e21) & (e21 > e50) & (e50 > e200)
    bear = (e8 < e21) & (e21 < e50) & (e50 < e200)
    return pd.Series(np.where(bull, 1, np.where(bear, -1, 0)), index=df.index)


def donchian_width(df: pd.DataFrame, n: int = 20) -> pd.Series:
    hi = df["High"].rolling(n).max()
    lo = df["Low"].rolling(n).min()
    return (hi - lo) / df["Close"]


def squeeze(df: pd.DataFrame, n: int = 20) -> pd.Series:
    """1 если Bollinger inside Keltner (squeeze), 0 иначе."""
    sma = df["Close"].rolling(n).mean()
    std = df["Close"].rolling(n).std()
    bb_up = sma + 2 * std
    bb_dn = sma - 2 * std
    a = atr(df, n)
    kc_up = sma + 1.5 * a
    kc_dn = sma - 1.5 * a
    return ((bb_up < kc_up) & (bb_dn > kc_dn)).astype(int)


def momentum_persistence(df: pd.DataFrame, n: int = 5) -> pd.Series:
    """Доля закрытий в одну сторону за n баров."""
    direction = np.sign(df["Close"] - df["Close"].shift(1))
    return direction.rolling(n).sum() / n


# ---------------------------------------------------------------------------
# Расширенный скоринг (v11)
# ---------------------------------------------------------------------------
def score_v11(df: pd.DataFrame) -> pd.DataFrame:
    """v11 = v10 + новые блоки F (regime), G (momentum quality)."""
    out = score_timeframe(df).copy()

    # F1: ADX-режим (трендовый или ranging)
    a = adx(df, 14)
    out["adx"] = a
    out["F1_trending"] = (a > 25).astype(int)
    out["F1_strong_trend"] = (a > 35).astype(int)

    # F2: EMA stack
    es = ema_stack(df)
    out["F2"] = es * 2  # ±2 балла

    # F3: Squeeze (если 1 — пропускаем сигнал)
    out["F3_squeeze"] = squeeze(df, 20)

    # F4: Donchian width regime
    dw = donchian_width(df, 20)
    dw_med = dw.rolling(60).median()
    out["F4_expansion"] = (dw > dw_med * 1.3).astype(int)
    out["F4_compression"] = (dw < dw_med * 0.7).astype(int)

    # G1: Momentum persistence (на разных длинах)
    mp5 = momentum_persistence(df, 5)
    mp10 = momentum_persistence(df, 10)
    out["G1"] = np.where(
        (mp5.abs() > 0.6) & (mp10.abs() > 0.4) & (np.sign(mp5) == np.sign(mp10)),
        2 * np.sign(mp5),
        0
    )

    # G2: Quality of HH/LL — новый экстремум сопровождается новым максимумом BBP
    bbp = bbp1000(df)
    win = 20
    new_hh = df["High"] >= df["High"].shift(1).rolling(win).max()
    new_ll = df["Low"] <= df["Low"].shift(1).rolling(win).min()
    bbp_new_hi = bbp >= bbp.shift(1).rolling(win).max()
    bbp_new_lo = bbp <= bbp.shift(1).rolling(win).min()
    out["G2"] = np.where(new_hh & bbp_new_hi, 2,
                  np.where(new_ll & bbp_new_lo, -2, 0))

    return out


def combined_v11(s15: pd.DataFrame, s1h: pd.DataFrame, s4h: pd.DataFrame) -> pd.DataFrame:
    """Комбинированный скор v11 с фильтрами режима."""
    base = combined_score(s15, s1h, s4h)
    aligned1h = s1h.reindex(s15.index, method="ffill")
    aligned4h = s4h.reindex(s15.index, method="ffill")

    # добавляем F и G блоки (берём с 15m + 1h + 4h)
    base["F1_15m"] = s15["F1_trending"]
    base["F1_1h"] = aligned1h["F1_trending"]
    base["F1_4h"] = aligned4h["F1_trending"]
    base["F1_strong_4h"] = aligned4h["F1_strong_trend"]
    base["F2_15m"] = s15["F2"]
    base["F2_1h"] = aligned1h["F2"]
    base["F2_4h"] = aligned4h["F2"]
    base["F3_squeeze_15m"] = s15["F3_squeeze"]
    base["F4_expansion_1h"] = aligned1h["F4_expansion"]
    base["G1_15m"] = s15["G1"]
    base["G1_1h"] = aligned1h["G1"]
    base["G2_15m"] = s15["G2"]
    base["G2_4h"] = aligned4h["G2"]

    # доп. очки в score
    extra = (
        base["F2_4h"] +              # ±2 EMA-stack 4H
        base["F2_1h"] * 0.5 +        # ±1 EMA-stack 1H
        base["G1_15m"] +             # ±2 momentum persistence
        base["G2_4h"]                # ±2 quality of HH/LL
    )
    base["score_v11"] = base["score"] + extra

    # confluence: сколько блоков (A,B,C,D,E) дают сигнал в направлении score
    a_block = base["A1"] + base["A2"] + base["A3"]
    b_block = base["B1"] + base["B2"] + base["B3"]
    c_block = base["C1"] + base["C2"] + base["C3"]
    d_block = base["D1"] + base["D2"] + base["D3"]
    e_block = base["E1"] + base["E2"] + base["E3"]
    direction = np.sign(base["score_v11"])
    base["confluence"] = (
        (np.sign(a_block) == direction).astype(int) +
        (np.sign(b_block) == direction).astype(int) +
        (np.sign(c_block) == direction).astype(int) +
        (np.sign(d_block) == direction).astype(int) +
        (np.sign(e_block) == direction).astype(int)
    )

    base["adx_4h"] = aligned4h["adx"]
    base["adx_1h"] = aligned1h["adx"]
    base["squeeze_15m"] = base["F3_squeeze_15m"]
    return base


# ---------------------------------------------------------------------------
# Бэктест с фильтрами
# ---------------------------------------------------------------------------
def backtest_v11(
    scores: pd.DataFrame,
    expiry_hours: int,
    min_abs_score: int,
    min_confluence: int = 0,
    require_trending_4h: bool = False,
    require_trending_1h: bool = False,
    skip_squeeze: bool = False,
    session_hours_utc: tuple | None = None,  # (start, end) inclusive
) -> pd.DataFrame:
    bars_offset = expiry_hours * 4
    closes = scores["close"].values
    score = scores["score_v11"].values
    confluence = scores["confluence"].values
    adx_4h = scores["adx_4h"].values
    adx_1h = scores["adx_1h"].values
    squeeze_15m = scores["squeeze_15m"].values
    times = scores.index

    trades = []
    for i in range(len(scores) - bars_offset):
        s = score[i]
        if abs(s) < min_abs_score:
            continue
        if min_confluence and confluence[i] < min_confluence:
            continue
        if require_trending_4h and (np.isnan(adx_4h[i]) or adx_4h[i] < 25):
            continue
        if require_trending_1h and (np.isnan(adx_1h[i]) or adx_1h[i] < 25):
            continue
        if skip_squeeze and squeeze_15m[i] == 1:
            continue
        if session_hours_utc:
            hour = times[i].hour
            lo, hi = session_hours_utc
            if not (lo <= hour <= hi):
                continue

        direction = 1 if s > 0 else -1
        entry = closes[i]
        exit_ = closes[i + bars_offset]
        win = (exit_ > entry and direction == 1) or (exit_ < entry and direction == -1)
        trades.append(
            {
                "time": times[i],
                "score": s,
                "confluence": confluence[i],
                "direction": "BUY" if direction == 1 else "SELL",
                "entry": entry,
                "exit": exit_,
                "win": win,
            }
        )
    return pd.DataFrame(trades)


# ---------------------------------------------------------------------------
# Walk-forward
# ---------------------------------------------------------------------------
def split_train_test(scores: pd.DataFrame, train_frac: float = 0.6):
    n = len(scores)
    split = int(n * train_frac)
    return scores.iloc[:split].copy(), scores.iloc[split:].copy()
