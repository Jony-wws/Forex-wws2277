"""
EDGE v13 — с cross-asset контекстом (только реальные данные yfinance).

Источники:
- FX пары: EURJPY, GBPJPY, AUDJPY, USDJPY, USDCAD, USDCHF, AUDUSD, NZDUSD, GBPUSD, EURUSD
- DXY (UUP ETF proxy — 210 баров, DX-Y.NYB — 591 бар)
- VIX (^VIX) — страх / risk regime
- Nikkei (^N225) — для JPY кроссов
- S&P 500 (^GSPC) — risk-on/off
- Gold (GC=F) — safe haven
- WTI Oil (CL=F) — CAD driver
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import yfinance as yf

from edge_v10 import session_vwap, bbp1000, atr
from edge_v11 import adx, ema_stack, squeeze, momentum_persistence, score_v11 as score_v11_base
from edge_v11 import combined_v11 as combined_v11_base
from edge_v12 import candlestick_patterns, bb_position, bollinger, regime, level_block, reversal_block


# Mapping каждой пары к её контекстному активу
CONTEXT_MAP = {
    "EURJPY=X": {"jpy_asset": "^N225", "base_asset": None},
    "GBPJPY=X": {"jpy_asset": "^N225", "base_asset": None},
    "AUDJPY=X": {"jpy_asset": "^N225", "base_asset": "^GSPC"},  # AUD = risk proxy
    "USDJPY=X": {"jpy_asset": "^N225", "base_asset": "DX-Y.NYB"},
    "USDCAD=X": {"jpy_asset": None, "base_asset": "DX-Y.NYB", "extra": "CL=F"},  # Oil
    "USDCHF=X": {"jpy_asset": None, "base_asset": "DX-Y.NYB"},
    "AUDUSD=X": {"jpy_asset": None, "base_asset": "^GSPC"},  # risk proxy
    "NZDUSD=X": {"jpy_asset": None, "base_asset": "^GSPC"},
    "GBPUSD=X": {"jpy_asset": None, "base_asset": "DX-Y.NYB"},
    "EURUSD=X": {"jpy_asset": None, "base_asset": "DX-Y.NYB"},
}


def download_context_assets(period: str = "2y"):
    """Качает все контекстные активы, возвращает словарь."""
    out = {}
    for sym in ["DX-Y.NYB", "^VIX", "^N225", "^GSPC", "GC=F", "CL=F", "^HSI"]:
        try:
            d = yf.download(sym, period=period, interval="1h", progress=False, auto_adjust=False)
            if isinstance(d.columns, pd.MultiIndex):
                d.columns = d.columns.get_level_values(0)
            # Убираем timezone для согласованности
            if d.index.tz is not None:
                d.index = d.index.tz_convert("UTC").tz_localize(None)
            out[sym] = d
        except Exception as e:
            print(f"  Skip {sym}: {e}")
    return out


def cross_asset_score(df: pd.DataFrame, pair: str, ctx: dict) -> pd.Series:
    """
    Доп. очки за согласованность с контекстными активами.
    Положительные = bullish FX pair, отрицательные = bearish.
    """
    cfg = CONTEXT_MAP.get(pair, {})
    score = pd.Series(0.0, index=df.index)

    # JPY-кроссы: корреляция с Nikkei (Nikkei ↑ = JPY ↓ = пара ↑)
    if cfg.get("jpy_asset") and cfg["jpy_asset"] in ctx:
        n = ctx[cfg["jpy_asset"]]
        n_close = n["Close"].reindex(df.index, method="ffill")
        n_change = n_close.pct_change(5)  # 5-часовое изменение
        # Если Nikkei вверх → JPY-пары вверх (+2 за сильный момент)
        score = score + np.where(n_change > 0.003, 2,
                         np.where(n_change < -0.003, -2, 0))

    # USD-пары: корреляция с DXY
    if cfg.get("base_asset") == "DX-Y.NYB" and "DX-Y.NYB" in ctx:
        d = ctx["DX-Y.NYB"]
        d_close = d["Close"].reindex(df.index, method="ffill")
        d_change = d_close.pct_change(5)
        # Для USDJPY/USDCAD/USDCHF/DXY: DXY ↑ = USD ↑ = пара USD/x вверх
        if pair in ["USDJPY=X", "USDCAD=X", "USDCHF=X"]:
            score = score + np.where(d_change > 0.002, 2,
                             np.where(d_change < -0.002, -2, 0))
        # Для EURUSD/GBPUSD: DXY ↑ = USD ↑ = X/USD вниз
        elif pair in ["EURUSD=X", "GBPUSD=X"]:
            score = score + np.where(d_change > 0.002, -2,
                             np.where(d_change < -0.002, 2, 0))

    # AUD/NZD: корреляция с SPX (risk-on)
    if cfg.get("base_asset") == "^GSPC" and "^GSPC" in ctx:
        s = ctx["^GSPC"]
        s_close = s["Close"].reindex(df.index, method="ffill")
        s_change = s_close.pct_change(5)
        if pair in ["AUDUSD=X", "NZDUSD=X", "AUDJPY=X"]:
            score = score + np.where(s_change > 0.002, 2,
                             np.where(s_change < -0.002, -2, 0))

    # VIX: risk regime
    if "^VIX" in ctx:
        v = ctx["^VIX"]
        v_close = v["Close"].reindex(df.index, method="ffill")
        # Low VIX risk-on → AUD/NZD cares, JPY SELL (for JPY crosses BUY more)
        # High VIX risk-off → JPY BUY (JPY crosses SELL), USD BUY
        if pair in ["AUDJPY=X", "AUDUSD=X", "NZDUSD=X"]:
            score = score + np.where(v_close < 15, 1,
                             np.where(v_close > 22, -1, 0))
        elif pair in ["USDJPY=X", "EURJPY=X", "GBPJPY=X"]:
            score = score + np.where(v_close > 22, -1, 0)  # risk-off хорош для JPY, пары вниз

    # Oil для CAD
    if cfg.get("extra") == "CL=F" and "CL=F" in ctx:
        o = ctx["CL=F"]
        o_close = o["Close"].reindex(df.index, method="ffill")
        o_change = o_close.pct_change(5)
        if pair == "USDCAD=X":
            # Oil ↑ = CAD сильнее = USDCAD вниз
            score = score + np.where(o_change > 0.01, -2,
                             np.where(o_change < -0.01, 2, 0))

    return score


# ---------------------------------------------------------------------------
# v13 scoring — использует v12 + cross-asset
# ---------------------------------------------------------------------------
def score_v13(df_15m: pd.DataFrame, df_1h: pd.DataFrame, df_4h: pd.DataFrame,
              pair: str, ctx: dict):
    """Возвращает DataFrame с колонкой score_v13 индексированной по df_1h."""
    from edge_v12 import score_v12, combined_v12

    s15 = score_v12(df_15m) if len(df_15m) > 100 else None
    s1h = score_v12(df_1h)
    s4h = score_v12(df_4h)

    if s15 is not None:
        base = combined_v12(s15, s1h, s4h).dropna()
        base_tf = "15m"
    else:
        # fallback: используем 1h как entry
        base_idx = s1h.index
        aligned4h = s4h.reindex(base_idx, method="ffill")
        base = combined_v11_base(s1h, s1h, s4h).dropna()
        base["score_v12"] = base["score_v11"]
        base["mode"] = "trend"
        base_tf = "1h"

    # Кросс-ассет очки
    ca = cross_asset_score(df_1h if base_tf == "1h" else df_15m, pair, ctx)
    ca = ca.reindex(base.index, method="ffill").fillna(0)
    base["cross_asset"] = ca
    base["score_v13"] = base["score_v12"] + ca

    return base


# ---------------------------------------------------------------------------
# Backtest v13
# ---------------------------------------------------------------------------
def backtest_v13(
    scores: pd.DataFrame,
    expiry_hours: int,
    min_abs_score: int,
    min_confluence: int = 0,
    min_abs_ca: int = 0,  # требуется сильное подтверждение cross-asset
    session_hours_utc: tuple | None = None,
    bars_per_hour: int = 4,  # 4 если 15m, 1 если 1h
) -> pd.DataFrame:
    bars_offset = expiry_hours * bars_per_hour
    closes = scores["close"].values
    score = scores["score_v13"].values
    confluence = scores["confluence"].values
    ca = scores["cross_asset"].values
    times = scores.index

    trades = []
    for i in range(len(scores) - bars_offset):
        s = score[i]
        if abs(s) < min_abs_score:
            continue
        if min_confluence and confluence[i] < min_confluence:
            continue
        if min_abs_ca and abs(ca[i]) < min_abs_ca:
            continue
        if session_hours_utc:
            h = times[i].hour
            lo, hi = session_hours_utc
            if not (lo <= h <= hi):
                continue
        # Требование: cross-asset направление должно совпасть со score
        if ca[i] != 0 and np.sign(ca[i]) != np.sign(s):
            continue

        direction = 1 if s > 0 else -1
        entry = closes[i]
        exit_ = closes[i + bars_offset]
        win = (exit_ > entry and direction == 1) or (exit_ < entry and direction == -1)
        trades.append({
            "time": times[i], "score": s, "ca": ca[i], "confluence": confluence[i],
            "direction": "BUY" if direction == 1 else "SELL",
            "entry": entry, "exit": exit_, "win": win,
        })
    return pd.DataFrame(trades)


def split_train_test(scores, train_frac=0.6):
    n = len(scores); split = int(n * train_frac)
    return scores.iloc[:split].copy(), scores.iloc[split:].copy()
