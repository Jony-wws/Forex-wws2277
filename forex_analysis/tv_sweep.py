"""
TradingView-equivalent visual sweep for 28 forex pairs.

Generates PNG charts on D1, H4, H1 timeframes for each pair with:
- Candlesticks (mplfinance)
- EMA 8/21/55/200
- Bollinger Bands (20, 2)
- Pivot Points (PP, S1, S2, R1, R2) — Standard
- Volume Profile (POC line, VAH, VAL — approximation via histogram on tick volume)
- Last 3 swing highs / lows (HH/HL/LH/LL detection)
- Last bullish/bearish Order Block (last large opposite-direction candle before BOS)
- Last Fair Value Gap (3-candle imbalance)
- ADX(14) value annotation
- ATR(14) / ADR(D1) usage annotation
- Daily VWAP (anchored to D1 open)

Output: tradingview_sweep/{pair}/{tf}.png + tradingview_sweep/index.md (table per pair)
"""
from __future__ import annotations
import json
import math
import os
import sys
from dataclasses import dataclass, asdict
from datetime import datetime, timezone, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import yfinance as yf
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import mplfinance as mpf

PAIRS = [
    "EURUSD", "GBPUSD", "AUDUSD", "NZDUSD", "USDJPY", "USDCHF", "USDCAD",
    "EURGBP", "EURAUD", "EURNZD", "EURJPY", "EURCHF", "EURCAD",
    "GBPAUD", "GBPNZD", "GBPJPY", "GBPCHF", "GBPCAD",
    "AUDNZD", "AUDJPY", "AUDCHF", "AUDCAD",
    "NZDJPY", "NZDCHF", "NZDCAD",
    "CADJPY", "CADCHF", "CHFJPY",
]

# Timeframes: yfinance limits — 1d unlimited, 4h: 60d, 1h: 730d
TFS = {
    "D1": dict(interval="1d", period="1y"),
    "H4": dict(interval="4h", period="60d"),
    "H1": dict(interval="1h", period="60d"),
}

OUT_DIR = Path("tradingview_sweep")
OUT_DIR.mkdir(exist_ok=True)


def yahoo_symbol(pair: str) -> str:
    return f"{pair}=X"


def fetch(pair: str, interval: str, period: str) -> pd.DataFrame:
    try:
        df = yf.download(
            yahoo_symbol(pair),
            interval=interval,
            period=period,
            progress=False,
            auto_adjust=False,
            threads=False,
        )
    except Exception as exc:
        print(f"[err] {pair} {interval}: {exc}")
        return pd.DataFrame()
    if df is None or df.empty:
        return pd.DataFrame()
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    keep = [c for c in ["Open", "High", "Low", "Close", "Volume"] if c in df.columns]
    df = df[keep].dropna()
    if "Volume" not in df.columns or df["Volume"].sum() == 0:
        df["Volume"] = 1.0
    return df


def ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False).mean()


def true_range(df: pd.DataFrame) -> pd.Series:
    h, l, c = df["High"], df["Low"], df["Close"]
    pc = c.shift(1)
    return pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)


def adx(df: pd.DataFrame, n: int = 14) -> pd.Series:
    h, l = df["High"], df["Low"]
    up = h.diff()
    dn = -l.diff()
    plus_dm = ((up > dn) & (up > 0)).astype(float) * up
    minus_dm = ((dn > up) & (dn > 0)).astype(float) * dn
    tr = true_range(df)
    atr_v = tr.ewm(alpha=1.0 / n, adjust=False).mean()
    plus_di = 100.0 * plus_dm.ewm(alpha=1.0 / n, adjust=False).mean() / (atr_v + 1e-12)
    minus_di = 100.0 * minus_dm.ewm(alpha=1.0 / n, adjust=False).mean() / (atr_v + 1e-12)
    dx = 100.0 * (plus_di - minus_di).abs() / (plus_di + minus_di + 1e-12)
    return dx.ewm(alpha=1.0 / n, adjust=False).mean()


def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    return true_range(df).ewm(alpha=1.0 / n, adjust=False).mean()


def bollinger(close: pd.Series, n: int = 20, k: float = 2.0):
    m = close.rolling(n).mean()
    sd = close.rolling(n).std(ddof=0)
    return m, m + k * sd, m - k * sd


def pivot_points(prev_h: float, prev_l: float, prev_c: float):
    pp = (prev_h + prev_l + prev_c) / 3.0
    r1 = 2 * pp - prev_l
    s1 = 2 * pp - prev_h
    r2 = pp + (prev_h - prev_l)
    s2 = pp - (prev_h - prev_l)
    return dict(PP=pp, R1=r1, R2=r2, S1=s1, S2=s2)


def volume_profile(df: pd.DataFrame, bins: int = 30):
    """Return (POC, VAH, VAL) using last N bars and price-volume histogram."""
    if df.empty:
        return None, None, None
    prices = (df["High"] + df["Low"] + df["Close"]) / 3.0
    vols = df["Volume"].astype(float).values
    lo, hi = float(prices.min()), float(prices.max())
    if hi <= lo:
        return None, None, None
    edges = np.linspace(lo, hi, bins + 1)
    centers = 0.5 * (edges[:-1] + edges[1:])
    hist = np.zeros(bins)
    idx = np.clip(np.searchsorted(edges, prices.values) - 1, 0, bins - 1)
    for i, v in zip(idx, vols):
        hist[i] += v
    if hist.sum() == 0:
        return None, None, None
    poc_i = int(np.argmax(hist))
    poc = float(centers[poc_i])
    # Value Area (70%): expand from POC outwards
    target = 0.7 * hist.sum()
    lo_i, hi_i = poc_i, poc_i
    cur = hist[poc_i]
    while cur < target and (lo_i > 0 or hi_i < bins - 1):
        lo_v = hist[lo_i - 1] if lo_i > 0 else -1
        hi_v = hist[hi_i + 1] if hi_i < bins - 1 else -1
        if hi_v >= lo_v and hi_i < bins - 1:
            hi_i += 1
            cur += hist[hi_i]
        elif lo_i > 0:
            lo_i -= 1
            cur += hist[lo_i]
        else:
            break
    vah = float(centers[hi_i])
    val = float(centers[lo_i])
    return poc, vah, val


def swing_points(df: pd.DataFrame, lookback: int = 3):
    """Return (highs_idx, lows_idx) — bars where center is local max/min in [-lookback, +lookback]."""
    h = df["High"].values
    l = df["Low"].values
    n = len(df)
    highs, lows = [], []
    for i in range(lookback, n - lookback):
        win_h = h[i - lookback : i + lookback + 1]
        win_l = l[i - lookback : i + lookback + 1]
        if h[i] == win_h.max() and h[i] > h[i - 1] and h[i] > h[i + 1]:
            highs.append(i)
        if l[i] == win_l.min() and l[i] < l[i - 1] and l[i] < l[i + 1]:
            lows.append(i)
    return highs, lows


def detect_structure(df: pd.DataFrame):
    """Return string: BOS_UP / BOS_DN / CHoCH_UP / CHoCH_DN / RANGE based on last 6 swings."""
    highs, lows = swing_points(df, lookback=3)
    if len(highs) < 2 or len(lows) < 2:
        return "RANGE"
    h_vals = [df["High"].iloc[i] for i in highs[-3:]]
    l_vals = [df["Low"].iloc[i] for i in lows[-3:]]
    last_close = df["Close"].iloc[-1]
    # Last 2 highs/lows
    if len(h_vals) >= 2 and len(l_vals) >= 2:
        if last_close > h_vals[-1]:  # broke last high
            if h_vals[-1] > h_vals[-2] and l_vals[-1] > l_vals[-2]:
                return "BOS_UP"  # continuation up
            return "CHoCH_UP"  # change of character up
        if last_close < l_vals[-1]:
            if l_vals[-1] < l_vals[-2] and h_vals[-1] < h_vals[-2]:
                return "BOS_DN"
            return "CHoCH_DN"
    return "RANGE"


def detect_fvg(df: pd.DataFrame):
    """Return last bullish/bearish FVG (3-candle imbalance) or None."""
    if len(df) < 3:
        return None
    fvg = []
    for i in range(2, len(df)):
        h0, l0 = df["High"].iloc[i - 2], df["Low"].iloc[i - 2]
        h2, l2 = df["High"].iloc[i], df["Low"].iloc[i]
        if l2 > h0:
            fvg.append(("bullish", i, h0, l2))
        elif h2 < l0:
            fvg.append(("bearish", i, h2, l0))
    return fvg[-1] if fvg else None


def detect_last_ob(df: pd.DataFrame):
    """Find last bearish candle before strong bullish move (and vice versa)."""
    if len(df) < 6:
        return None
    closes = df["Close"].values
    opens = df["Open"].values
    highs = df["High"].values
    lows = df["Low"].values
    last = None
    for i in range(2, len(df) - 1):
        # Bullish OB: down candle followed by 2 strong up candles
        if closes[i] < opens[i]:
            if (
                closes[i + 1] > opens[i + 1]
                and (closes[i + 1] - opens[i + 1]) > 1.2 * abs(opens[i] - closes[i])
                and closes[i + 1] > opens[i]
            ):
                last = ("bull_ob", i, lows[i], opens[i])
        elif closes[i] > opens[i]:
            if (
                closes[i + 1] < opens[i + 1]
                and (opens[i + 1] - closes[i + 1]) > 1.2 * abs(closes[i] - opens[i])
                and closes[i + 1] < opens[i]
            ):
                last = ("bear_ob", i, opens[i], highs[i])
    return last


def daily_vwap(df: pd.DataFrame) -> pd.Series:
    """Anchored daily VWAP — cumulative typical_price*volume / cumulative volume,
    reset every UTC day."""
    if df.empty:
        return pd.Series(dtype=float)
    tp = (df["High"] + df["Low"] + df["Close"]) / 3.0
    v = df["Volume"].astype(float)
    df_x = pd.DataFrame({"tp": tp.values, "v": v.values}, index=df.index)
    if not isinstance(df_x.index, pd.DatetimeIndex):
        return pd.Series([np.nan] * len(df_x), index=df_x.index)
    grp = df_x.groupby(df_x.index.date)
    vwap_parts = []
    for day, g in grp:
        cum_pv = (g["tp"] * g["v"]).cumsum()
        cum_v = g["v"].cumsum().replace(0, np.nan)
        vwap_parts.append(cum_pv / cum_v)
    return pd.concat(vwap_parts).reindex(df.index)


@dataclass
class PairTfSummary:
    pair: str
    tf: str
    last_close: float
    ema_align: str  # "BUY" / "SELL" / "FLAT"
    adx_last: float
    atr_last: float
    bb_squeeze: bool  # last BBW < 30th percentile
    structure: str
    fvg: str
    ob: str
    poc: float | None
    vah: float | None
    val: float | None
    pivots: dict
    vwap_position: str  # "ABOVE" / "BELOW" / None


def render(pair: str, tf: str, df: pd.DataFrame) -> PairTfSummary | None:
    if df.empty or len(df) < 50:
        print(f"[skip] {pair} {tf}: not enough data")
        return None

    e8 = ema(df["Close"], 8)
    e21 = ema(df["Close"], 21)
    e55 = ema(df["Close"], 55)
    e200 = ema(df["Close"], 200) if len(df) >= 200 else ema(df["Close"], min(len(df) // 2, 100))
    bb_m, bb_u, bb_l = bollinger(df["Close"])
    bbw = (bb_u - bb_l) / bb_m
    bbw_pct = bbw.rank(pct=True).iloc[-1] if not bbw.dropna().empty else 0.5
    bb_squeeze = bool(bbw_pct < 0.30) if not math.isnan(bbw_pct) else False
    adx_v = adx(df).iloc[-1]
    atr_v = atr(df).iloc[-1]
    poc, vah, val = volume_profile(df.tail(120))
    structure = detect_structure(df.tail(80))
    fvg = detect_fvg(df.tail(50))
    ob = detect_last_ob(df.tail(80))

    # Pivot points from previous bar (for D1 → previous day; for H4/H1 → previous D1 of yfinance separate fetch)
    if tf == "D1" and len(df) >= 2:
        p = pivot_points(df["High"].iloc[-2], df["Low"].iloc[-2], df["Close"].iloc[-2])
    else:
        # use last D1 from previous: just compute over last 24 bars for H1 / 6 for H4
        n_prev = 6 if tf == "H4" else 24
        if len(df) >= n_prev * 2:
            prev = df.iloc[-(n_prev * 2):-n_prev]
            p = pivot_points(prev["High"].max(), prev["Low"].min(), prev["Close"].iloc[-1])
        else:
            p = pivot_points(df["High"].iloc[-2], df["Low"].iloc[-2], df["Close"].iloc[-2])

    last_close = float(df["Close"].iloc[-1])
    if e8.iloc[-1] > e21.iloc[-1] > e55.iloc[-1]:
        ema_align = "BUY"
    elif e8.iloc[-1] < e21.iloc[-1] < e55.iloc[-1]:
        ema_align = "SELL"
    else:
        ema_align = "FLAT"

    vw = daily_vwap(df)
    vwap_position = None
    if not vw.dropna().empty:
        last_vw = vw.iloc[-1]
        if not pd.isna(last_vw):
            vwap_position = "ABOVE" if last_close > last_vw else "BELOW"

    # Plot
    plot_df = df.tail(120 if tf == "D1" else 200).copy()
    addplots = [
        mpf.make_addplot(ema(plot_df["Close"], 8), color="#2962FF", width=1.0),
        mpf.make_addplot(ema(plot_df["Close"], 21), color="#FFB300", width=1.0),
        mpf.make_addplot(ema(plot_df["Close"], 55), color="#9C27B0", width=1.0),
    ]
    if len(plot_df) >= 200:
        addplots.append(mpf.make_addplot(ema(plot_df["Close"], 200), color="#000000", width=1.4))
    bb_m_p, bb_u_p, bb_l_p = bollinger(plot_df["Close"])
    addplots.append(mpf.make_addplot(bb_u_p, color="#9E9E9E", width=0.8, linestyle="--"))
    addplots.append(mpf.make_addplot(bb_l_p, color="#9E9E9E", width=0.8, linestyle="--"))
    vw_p = daily_vwap(plot_df)
    if not vw_p.dropna().empty:
        addplots.append(mpf.make_addplot(vw_p, color="#E91E63", width=1.2, linestyle=":"))

    out_dir = OUT_DIR / pair
    out_dir.mkdir(exist_ok=True)
    out_path = out_dir / f"{tf}.png"

    title = f"{pair}  {tf}  |  EMA8/21/55  BB(20,2)  Daily VWAP"
    hlines_levels = []
    hlines_colors = []
    for k, v in p.items():
        hlines_levels.append(v)
        hlines_colors.append("#0066CC" if k == "PP" else ("#00897B" if k.startswith("R") else "#C62828"))
    if poc is not None:
        hlines_levels.append(poc)
        hlines_colors.append("#FF6F00")
    if vah is not None:
        hlines_levels.append(vah)
        hlines_colors.append("#FFAB40")
    if val is not None:
        hlines_levels.append(val)
        hlines_colors.append("#FFAB40")

    try:
        mpf.plot(
            plot_df,
            type="candle",
            style="charles",
            addplot=addplots,
            volume=False,
            hlines=dict(hlines=hlines_levels, colors=hlines_colors, linewidths=0.8, alpha=0.6),
            title=title,
            ylabel="Price",
            figsize=(14, 7),
            tight_layout=True,
            savefig=dict(fname=str(out_path), dpi=110, bbox_inches="tight"),
        )
    except Exception as exc:
        print(f"[plot-err] {pair} {tf}: {exc}")

    fvg_str = f"{fvg[0]} bar={fvg[1]} ({fvg[2]:.5f}-{fvg[3]:.5f})" if fvg else "none"
    ob_str = f"{ob[0]} bar={ob[1]} ({ob[2]:.5f}-{ob[3]:.5f})" if ob else "none"

    return PairTfSummary(
        pair=pair, tf=tf, last_close=last_close,
        ema_align=ema_align, adx_last=float(adx_v), atr_last=float(atr_v),
        bb_squeeze=bb_squeeze,
        structure=structure, fvg=fvg_str, ob=ob_str,
        poc=poc, vah=vah, val=val,
        pivots=p,
        vwap_position=vwap_position,
    )


def main():
    started = datetime.now(timezone.utc).isoformat()
    print(f"sweep started {started}")
    summaries: list[PairTfSummary] = []
    for pair in PAIRS:
        for tf, params in TFS.items():
            df = fetch(pair, params["interval"], params["period"])
            if df.empty:
                print(f"[no data] {pair} {tf}")
                continue
            s = render(pair, tf, df)
            if s:
                summaries.append(s)
                print(f"  {pair:7s} {tf}  EMA={s.ema_align:4s}  ADX={s.adx_last:5.1f}  "
                      f"struct={s.structure:8s}  vwap={s.vwap_position}  squeeze={s.bb_squeeze}")
    OUT_DIR.mkdir(exist_ok=True)
    with open(OUT_DIR / "sweep.json", "w") as fh:
        json.dump([asdict(s) for s in summaries], fh, indent=2, default=str)
    print(f"saved {len(summaries)} summaries -> {OUT_DIR}/sweep.json")


if __name__ == "__main__":
    main()
