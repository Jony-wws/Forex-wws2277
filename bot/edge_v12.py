"""
EDGE v12 — сессия-адаптивная система с дополнительными методами.

Ключевые улучшения:
- Candlestick patterns (pin, engulfing, inside)
- Bollinger Band position
- Previous session high/low
- Regime classifier (trending / ranging / compression)
- Reversal mode для ranging сессий
- Volatility-normalized scoring
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import yfinance as yf

from edge_v10 import session_vwap, bbp1000, atr
from edge_v11 import score_v11 as score_v11_base, combined_v11 as combined_v11_base
from edge_v11 import adx, ema_stack, squeeze, momentum_persistence


# ---------------------------------------------------------------------------
# Новые индикаторы
# ---------------------------------------------------------------------------
def bollinger(df: pd.DataFrame, n: int = 20, mult: float = 2.0):
    sma = df["Close"].rolling(n).mean()
    std = df["Close"].rolling(n).std()
    up = sma + mult * std
    dn = sma - mult * std
    return sma, up, dn


def bb_position(df: pd.DataFrame, n: int = 20):
    """Позиция цены относительно полос Боллинджера: -1..+1."""
    sma, up, dn = bollinger(df, n)
    # позиция: -1 = у нижней полосы, 0 = середина, +1 = у верхней
    return (df["Close"] - sma) / (up - sma).replace(0, np.nan)


def candlestick_patterns(df: pd.DataFrame) -> pd.DataFrame:
    """
    Возвращает таблицу где +1 = bullish pattern, -1 = bearish.
    - pin_bar
    - engulfing
    - inside_bar (нейтральный, сигнал следующей свечи)
    """
    body = (df["Close"] - df["Open"]).abs()
    rng = (df["High"] - df["Low"]).replace(0, np.nan)
    upper_wick = df["High"] - df[["Close", "Open"]].max(axis=1)
    lower_wick = df[["Close", "Open"]].min(axis=1) - df["Low"]

    # Pin bar: маленькое тело + один длинный хвост
    small_body = body < rng * 0.33
    bull_pin = small_body & (lower_wick > body * 2) & (upper_wick < body)
    bear_pin = small_body & (upper_wick > body * 2) & (lower_wick < body)

    # Engulfing: тело текущей свечи перекрывает тело предыдущей
    prev_body_abs = body.shift(1)
    prev_bull = df["Close"].shift(1) > df["Open"].shift(1)
    prev_bear = df["Close"].shift(1) < df["Open"].shift(1)
    curr_bull = df["Close"] > df["Open"]
    curr_bear = df["Close"] < df["Open"]
    bull_engulf = prev_bear & curr_bull & (body > prev_body_abs) & (df["Close"] > df["Open"].shift(1))
    bear_engulf = prev_bull & curr_bear & (body > prev_body_abs) & (df["Close"] < df["Open"].shift(1))

    # Inside bar
    inside = (df["High"] < df["High"].shift(1)) & (df["Low"] > df["Low"].shift(1))

    out = pd.DataFrame(index=df.index)
    out["bull_pin"] = bull_pin.astype(int)
    out["bear_pin"] = bear_pin.astype(int)
    out["bull_engulf"] = bull_engulf.astype(int)
    out["bear_engulf"] = bear_engulf.astype(int)
    out["inside"] = inside.astype(int)
    return out


def prev_day_levels(df: pd.DataFrame) -> pd.DataFrame:
    """High/Low предыдущего UTC-дня (forward-fill на каждом баре)."""
    day = df.index.floor("D")
    daily_hi = df["High"].groupby(day).max()
    daily_lo = df["Low"].groupby(day).min()
    prev_hi = daily_hi.shift(1)
    prev_lo = daily_lo.shift(1)
    out = pd.DataFrame(index=df.index)
    out["prev_hi"] = df.index.floor("D").map(prev_hi)
    out["prev_lo"] = df.index.floor("D").map(prev_lo)
    return out


def regime(df: pd.DataFrame):
    """
    Определение режима рынка:
    - "trend": ADX>25
    - "range": ADX<20 и compression
    - "breakout": после squeeze цена пробила полосу
    - "mixed": остальное
    """
    a = adx(df, 14)
    sma, up, dn = bollinger(df, 20)
    sq = squeeze(df, 20)
    expansion = (df["High"] > up) | (df["Low"] < dn)
    breakout = (sq.shift(1) == 1) & expansion
    reg = pd.Series("mixed", index=df.index)
    reg = reg.mask(a > 25, "trend")
    reg = reg.mask((a < 20) & (sq == 1), "range")
    reg = reg.mask(breakout, "breakout")
    return reg, a


# ---------------------------------------------------------------------------
# Reversal block — специально для ranging режима
# ---------------------------------------------------------------------------
def reversal_block(df: pd.DataFrame) -> pd.Series:
    """
    Ищем отскоки от экстремумов (+ отскок от BB extreme + reversal pattern).
    Возвращает ±N очков в направлении ожидаемого отскока.
    """
    cs = candlestick_patterns(df)
    bb_pos = bb_position(df, 20)
    bbp = bbp1000(df)
    # Bull reversal: цена у нижней полосы + bullish pattern + bbp не падает сильно
    at_low = bb_pos < -0.8
    at_high = bb_pos > 0.8
    bull_rev = at_low & (cs["bull_pin"] | cs["bull_engulf"])
    bear_rev = at_high & (cs["bear_pin"] | cs["bear_engulf"])
    return pd.Series(np.where(bull_rev, 4, np.where(bear_rev, -4, 0)), index=df.index)


# ---------------------------------------------------------------------------
# Level-break block — пробой/sweep предыдущих дневных экстремумов
# ---------------------------------------------------------------------------
def level_block(df: pd.DataFrame) -> pd.Series:
    lvl = prev_day_levels(df)
    close = df["Close"]
    hi = lvl["prev_hi"]
    lo = lvl["prev_lo"]
    a = atr(df, 14)

    # Успешный пробой вверх: цена выше prev_hi + закрепилась
    break_up = (close > hi) & (close > hi + a * 0.1)
    # Sweep vs close back: цена зашла за prev_hi но закрылась ниже
    sweep_up = (df["High"] > hi) & (close < hi)
    break_dn = (close < lo) & (close < lo - a * 0.1)
    sweep_dn = (df["Low"] < lo) & (close > lo)

    return pd.Series(np.where(break_up, 2,
                      np.where(sweep_up, -2,
                      np.where(break_dn, -2,
                      np.where(sweep_dn, 2, 0)))), index=df.index)


# ---------------------------------------------------------------------------
# v12 scoring
# ---------------------------------------------------------------------------
def score_v12(df: pd.DataFrame) -> pd.DataFrame:
    out = score_v11_base(df).copy()

    # Candlestick patterns block
    cs = candlestick_patterns(df)
    h1 = 3 * cs["bull_pin"] - 3 * cs["bear_pin"]
    h2 = 3 * cs["bull_engulf"] - 3 * cs["bear_engulf"]
    out["H_pattern"] = h1 + h2

    # Bollinger position block
    bbp_pos = bb_position(df, 20)
    out["bb_pos"] = bbp_pos
    # в тренде: цена у верхней полосы + тренд = +1 (продолжение);
    # в range: цена у крайней полосы = сигнал против (fade) — учитывается в reversal_block
    out["I_bb"] = np.clip(bbp_pos, -1, 1).round()

    # Level-break
    out["J_level"] = level_block(df)

    # Reversal block (для range)
    out["K_reversal"] = reversal_block(df)

    # Regime
    reg, a = regime(df)
    out["regime"] = reg
    out["adx"] = a

    return out


# ---------------------------------------------------------------------------
# Combined v12: режим-адаптивный скор
# ---------------------------------------------------------------------------
def combined_v12(s15: pd.DataFrame, s1h: pd.DataFrame, s4h: pd.DataFrame) -> pd.DataFrame:
    base = combined_v11_base(s15, s1h, s4h)
    aligned1h = s1h.reindex(s15.index, method="ffill")
    aligned4h = s4h.reindex(s15.index, method="ffill")

    # Добавляем новые блоки
    base["H_15m"] = s15["H_pattern"]
    base["H_1h"] = aligned1h["H_pattern"]
    base["J_15m"] = s15["J_level"]
    base["K_15m"] = s15["K_reversal"]
    base["regime_4h"] = aligned4h["regime"]
    base["regime_1h"] = aligned1h["regime"]
    base["regime_15m"] = s15["regime"]

    # v12 score: trend mode использует старый score + H + J; range mode заменяет на K
    is_range = (base["regime_4h"] == "range") | (base["regime_1h"] == "range")
    trend_score = base["score_v11"] + base["H_15m"] * 0.7 + base["J_15m"]
    range_score = base["K_15m"] * 2 + base["H_15m"]  # в range полагаемся на reversal
    base["score_v12"] = np.where(is_range, range_score, trend_score)
    base["mode"] = np.where(is_range, "range", "trend")

    return base


# ---------------------------------------------------------------------------
# Backtest с session-адаптивными правилами
# ---------------------------------------------------------------------------
def backtest_v12(
    scores: pd.DataFrame,
    expiry_hours: int,
    min_abs_score: int,
    min_confluence: int = 0,
    require_trending_4h: bool = False,
    session_hours_utc: tuple | None = None,
    mode_filter: str | None = None,  # "trend" | "range" | None
) -> pd.DataFrame:
    bars_offset = expiry_hours * 4
    closes = scores["close"].values
    score = scores["score_v12"].values
    confluence = scores["confluence"].values
    adx_4h = scores["adx_4h"].values
    mode = scores["mode"].values
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
        if session_hours_utc:
            h = times[i].hour
            lo, hi = session_hours_utc
            if not (lo <= h <= hi):
                continue
        if mode_filter and mode[i] != mode_filter:
            continue

        direction = 1 if s > 0 else -1
        entry = closes[i]
        exit_ = closes[i + bars_offset]
        win = (exit_ > entry and direction == 1) or (exit_ < entry and direction == -1)
        trades.append({
            "time": times[i], "score": s, "confluence": confluence[i],
            "mode": mode[i], "direction": "BUY" if direction == 1 else "SELL",
            "entry": entry, "exit": exit_, "win": win,
        })
    return pd.DataFrame(trades)


def split_train_test(scores, train_frac=0.6):
    n = len(scores); split = int(n * train_frac)
    return scores.iloc[:split].copy(), scores.iloc[split:].copy()
