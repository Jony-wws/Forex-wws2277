"""
EDGE v10 backtest engine.
Simulates binary options on EUR/JPY (or any FX pair) using the full
EDGE v10 + v9 + TRAP + Top-Down scoring system.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import yfinance as yf


# ---------------------------------------------------------------------------
# Indicators
# ---------------------------------------------------------------------------
def session_vwap(df: pd.DataFrame) -> pd.Series:
    """VWAP that resets every UTC day. Uses tick-volume proxy (=1 if zero)."""
    typical = (df["High"] + df["Low"] + df["Close"]) / 3.0
    vol = df["Volume"].replace(0, 1).astype(float)
    pv = typical * vol
    day = df.index.floor("D")
    cum_pv = pv.groupby(day).cumsum()
    cum_v = vol.groupby(day).cumsum()
    return cum_pv / cum_v


def bbp1000(df: pd.DataFrame, length: int = 1000) -> pd.Series:
    """Bull-Bear Power with EMA(length). Approximation of TradingView 'BBP 1000'."""
    ema = df["Close"].ewm(span=length, adjust=False).mean()
    bull = df["High"] - ema
    bear = df["Low"] - ema
    return bull + bear


def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    h_l = df["High"] - df["Low"]
    h_pc = (df["High"] - df["Close"].shift()).abs()
    l_pc = (df["Low"] - df["Close"].shift()).abs()
    tr = pd.concat([h_l, h_pc, l_pc], axis=1).max(axis=1)
    return tr.rolling(n).mean()


# ---------------------------------------------------------------------------
# EDGE v10 scoring on a single timeframe
# ---------------------------------------------------------------------------
def score_timeframe(df: pd.DataFrame) -> pd.DataFrame:
    """
    Returns per-bar score components for one timeframe.
    Caller decides how to weight blocks across 4H/1H/15m.
    """
    out = pd.DataFrame(index=df.index)
    vwap = session_vwap(df)
    bbp = bbp1000(df)
    a = atr(df, 14)

    out["close"] = df["Close"]
    out["vwap"] = vwap
    out["bbp"] = bbp
    out["atr"] = a

    # ------------------ BLOCK A: VWAP ------------------
    above = df["Close"] > vwap
    vwap_slope = vwap.diff(5)
    cnt_above = above.rolling(10).sum()

    a1 = pd.Series(0.0, index=df.index)
    a1 = np.where((above) & (vwap_slope > 0) & (cnt_above >= 10), 3, a1)
    a1 = np.where((above) & (vwap_slope > 0) & (cnt_above < 10), 2, a1)
    a1 = np.where((above) & (vwap_slope.abs() <= a * 0.05), 1, a1)
    a1 = np.where((~above) & (vwap_slope < 0) & ((10 - cnt_above) >= 10), -3, a1)
    a1 = np.where((~above) & (vwap_slope < 0), -2, a1)
    a1 = np.where((~above) & (vwap_slope.abs() <= a * 0.05), -1, a1)
    out["A1"] = a1

    # A2 reclaim/lose: 2 closes on same side after side flip
    side = above.astype(int).replace(0, -1)
    flip = side.ne(side.shift())
    a2 = pd.Series(0.0, index=df.index)
    for i in range(2, len(df)):
        if flip.iloc[i - 1] and side.iloc[i] == side.iloc[i - 1]:
            a2.iloc[i] = 2 * side.iloc[i]
    out["A2"] = a2

    # A3 DVZ
    dvz = (df["Close"] - vwap) / a
    a3 = pd.Series(0.0, index=df.index)
    a3 = np.where(dvz > 1.5, -2, a3)
    a3 = np.where((dvz > 0.5) & (dvz <= 1.5), 1, a3)
    a3 = np.where((dvz > -0.5) & (dvz <= 0.5), 0, a3)
    a3 = np.where((dvz <= -0.5) & (dvz > -1.5), -1, a3)
    a3 = np.where(dvz <= -1.5, 2, a3)
    out["A3"] = a3

    # ------------------ BLOCK B: BBP ------------------
    bbp_pos = bbp > 0
    bbp_slope = bbp.diff(3)
    bbp_std = bbp.rolling(20).std()
    rising = bbp_slope > bbp_std * 0.2
    falling = bbp_slope < -bbp_std * 0.2

    b1 = pd.Series(0.0, index=df.index)
    b1 = np.where(bbp_pos & rising, 3, b1)
    b1 = np.where(bbp_pos & ~rising & ~falling, 2, b1)
    b1 = np.where(bbp_pos & falling, 1, b1)
    b1 = np.where(~bbp_pos & falling, -3, b1)
    b1 = np.where(~bbp_pos & ~rising & ~falling, -2, b1)
    b1 = np.where(~bbp_pos & rising, -1, b1)
    out["B1"] = b1

    # B2 divergence over last 20 bars: HH price + LH bbp (bear) or LL price + HL bbp (bull)
    win = 20
    price_hi = df["High"].rolling(win).max()
    price_lo = df["Low"].rolling(win).min()
    bbp_hi = bbp.rolling(win).max()
    bbp_lo = bbp.rolling(win).min()
    # current vs win-ago
    p_hh = df["High"] >= price_hi.shift(1)
    p_ll = df["Low"] <= price_lo.shift(1)
    b_lh = bbp < bbp_hi.shift(1) * 0.9
    b_hl = bbp > bbp_lo.shift(1) * 0.9
    b2 = pd.Series(0.0, index=df.index)
    b2 = np.where(p_hh & b_lh, -3, b2)
    b2 = np.where(p_ll & b_hl, 3, b2)
    out["B2"] = b2

    # B3 impulse/decay
    b3 = pd.Series(0.0, index=df.index)
    b3 = np.where(bbp_pos & rising, 2, b3)
    b3 = np.where(bbp_pos & falling, -2, b3)
    b3 = np.where(~bbp_pos & falling, -2, b3)
    b3 = np.where(~bbp_pos & rising, 2, b3)
    out["B3"] = b3

    # ------------------ BLOCK C: structure ------------------
    # C1 swing structure proxy: 10-bar HH/HL vs LH/LL
    hh = df["High"] > df["High"].shift(1)
    ll = df["Low"] < df["Low"].shift(1)
    hh_count = hh.rolling(10).sum()
    ll_count = ll.rolling(10).sum()
    c1 = pd.Series(0.0, index=df.index)
    c1 = np.where(hh_count >= 7, 3, c1)
    c1 = np.where(ll_count >= 7, -3, c1)
    c1 = np.where((hh_count >= 5) & (hh_count < 7), 2, c1)
    c1 = np.where((ll_count >= 5) & (ll_count < 7), -2, c1)
    out["C1"] = c1

    # C2 CEI + close-count
    body = (df["Close"] - df["Open"]).abs()
    rng = (df["High"] - df["Low"]).replace(0, np.nan)
    cei = (body / rng).rolling(10).mean() * 100
    bull_close = df["Close"] > df["Open"]
    bull_cnt = bull_close.rolling(10).sum()
    direction_recent = np.sign(df["Close"] - df["Close"].shift(5))
    c2 = pd.Series(0.0, index=df.index)
    c2 = np.where((cei > 60) & (direction_recent > 0), 2, c2)
    c2 = np.where((cei > 60) & (direction_recent < 0), -2, c2)
    c2 = np.where((cei >= 40) & (cei <= 60) & (direction_recent > 0), 1, c2)
    c2 = np.where((cei >= 40) & (cei <= 60) & (direction_recent < 0), -1, c2)
    c2 = c2 + np.where(bull_cnt >= 7, 2, 0)
    c2 = c2 + np.where(bull_cnt == 6, 1, 0)
    c2 = c2 + np.where(bull_cnt == 4, -1, 0)
    c2 = c2 + np.where(bull_cnt <= 3, -2, 0)
    c2 = np.clip(c2, -4, 4)
    out["C2"] = c2

    # C3 OB return: simple proxy — pullback into prior 5-bar range with reversal candle
    rng_hi5 = df["High"].shift(1).rolling(5).max()
    rng_lo5 = df["Low"].shift(1).rolling(5).min()
    in_range = (df["Low"] <= rng_lo5) & (df["Close"] > df["Open"])
    bear_ob = (df["High"] >= rng_hi5) & (df["Close"] < df["Open"])
    c3 = pd.Series(0.0, index=df.index)
    c3 = np.where(in_range, 2, c3)
    c3 = np.where(bear_ob, -2, c3)
    out["C3"] = c3

    # ------------------ BLOCK D: traps ------------------
    # D1 sweep: spike beyond prior 10-bar extreme + close back inside
    hi10 = df["High"].shift(1).rolling(10).max()
    lo10 = df["Low"].shift(1).rolling(10).min()
    sweep_up = (df["High"] > hi10) & (df["Close"] < hi10)
    sweep_dn = (df["Low"] < lo10) & (df["Close"] > lo10)
    d1 = pd.Series(0.0, index=df.index)
    d1 = np.where(sweep_up, -3, d1)
    d1 = np.where(sweep_dn, 3, d1)
    out["D1"] = d1

    # D2 fake breakout: close back inside the prior 10-bar range AND BBP doesn't confirm
    fake_up = sweep_up & ~rising
    fake_dn = sweep_dn & ~falling
    d2 = pd.Series(0.0, index=df.index)
    d2 = np.where(fake_up, -3, d2)
    d2 = np.where(fake_dn, 3, d2)
    out["D2"] = d2

    # D3 exhaustion / compression
    body_avg5 = body.rolling(5).mean()
    body_avg10 = body.rolling(10).mean()
    shrinking_bodies = body_avg5 < body_avg10 * 0.7
    same_dir5 = (df["Close"] > df["Close"].shift(5)) | (df["Close"] < df["Close"].shift(5))
    far_from_vwap = (df["Close"] - vwap).abs() > a * 1.5
    bbp_decay = bbp_slope.abs() < bbp_std * 0.1
    exhaustion_up = same_dir5 & (df["Close"] > df["Close"].shift(5)) & shrinking_bodies & far_from_vwap & bbp_decay
    exhaustion_dn = same_dir5 & (df["Close"] < df["Close"].shift(5)) & shrinking_bodies & far_from_vwap & bbp_decay
    rng_std = df["Close"].rolling(10).std()
    compression = (rng_std < a * 0.3) & (vwap_slope.abs() < a * 0.05) & (bbp.abs() < bbp_std)
    d3 = pd.Series(0.0, index=df.index)
    d3 = np.where(exhaustion_up, -2, d3)
    d3 = np.where(exhaustion_dn, 2, d3)
    d3 = np.where(compression & (direction_recent > 0), 1, d3)
    d3 = np.where(compression & (direction_recent < 0), -1, d3)
    out["D3"] = d3

    return out


# ---------------------------------------------------------------------------
# Top-down combine across 4H / 1H / 15m
# ---------------------------------------------------------------------------
def combined_score(s15: pd.DataFrame, s1h: pd.DataFrame, s4h: pd.DataFrame) -> pd.DataFrame:
    """Align 1H and 4H rows to each 15m bar (forward-fill last close)."""
    aligned1h = s1h.reindex(s15.index, method="ffill")
    aligned4h = s4h.reindex(s15.index, method="ffill")

    out = pd.DataFrame(index=s15.index)
    # blocks A-D taken from 15m primarily
    for col in ["A1", "A2", "A3", "B1", "B2", "B3", "C1", "C2", "C3", "D1", "D2", "D3"]:
        out[col] = s15[col]

    # E1 MTBA: BBP alignment across 3 TFs
    sign15 = np.sign(s15["bbp"])
    sign1h = np.sign(aligned1h["bbp"])
    sign4h = np.sign(aligned4h["bbp"])
    aligned = (sign15 == sign1h) & (sign1h == sign4h)
    out["E1"] = np.where(aligned, 3 * sign4h, 0)

    # E2 PQM: simple — if 4H and 1H A1 agree and 15m has shallow pullback (close near vwap)
    a1_4h = aligned4h["A1"]
    a1_1h = aligned1h["A1"]
    same_dir = np.sign(a1_4h) == np.sign(a1_1h)
    pullback_close = (s15["close"] - s15["vwap"]).abs() < s15["atr"] * 1.0
    out["E2"] = np.where(same_dir & pullback_close, 3 * np.sign(a1_4h), 0)

    # E3 TCP: if 4H A1+B1 strongly agree
    tcp_dir = np.sign(a1_4h + aligned4h["B1"])
    strong = (a1_4h.abs() + aligned4h["B1"].abs()) >= 4
    out["E3"] = np.where(strong, 2 * tcp_dir, 0)

    out["score"] = out.sum(axis=1)
    out["close"] = s15["close"]
    return out


# ---------------------------------------------------------------------------
# Backtest binary options
# ---------------------------------------------------------------------------
def backtest(scores: pd.DataFrame, expiry_hours: int, min_abs_score: int) -> pd.DataFrame:
    """
    For each bar, if |score| >= min_abs_score, open a binary option.
    Win if direction matches close at entry+expiry.
    Returns trade list.
    """
    trades = []
    bars_offset = expiry_hours * 4  # 15m bars
    closes = scores["close"].values
    score = scores["score"].values
    times = scores.index

    for i in range(len(scores) - bars_offset):
        s = score[i]
        if abs(s) < min_abs_score:
            continue
        direction = 1 if s > 0 else -1
        entry = closes[i]
        exit_ = closes[i + bars_offset]
        win = (exit_ > entry and direction == 1) or (exit_ < entry and direction == -1)
        trades.append(
            {
                "time": times[i],
                "score": s,
                "direction": "BUY" if direction == 1 else "SELL",
                "entry": entry,
                "exit": exit_,
                "diff_pips": (exit_ - entry) * 100,  # JPY pair: 1 pip = 0.01
                "win": win,
            }
        )
    return pd.DataFrame(trades)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def run(symbol: str = "EURJPY=X") -> None:
    print(f"\n=== EDGE v10 backtest: {symbol} ===")
    print("Downloading data...")
    df15 = yf.download(symbol, period="60d", interval="15m", progress=False, auto_adjust=False)
    df1h = yf.download(symbol, period="730d", interval="1h", progress=False, auto_adjust=False)

    # flatten multiindex columns from yfinance
    if isinstance(df15.columns, pd.MultiIndex):
        df15.columns = df15.columns.get_level_values(0)
    if isinstance(df1h.columns, pd.MultiIndex):
        df1h.columns = df1h.columns.get_level_values(0)

    # build 4H by resampling 1H
    df4h = df1h.resample("4h").agg(
        {"Open": "first", "High": "max", "Low": "min", "Close": "last", "Volume": "sum"}
    ).dropna()

    print(f"  15m bars: {len(df15)}  ({df15.index[0]} -> {df15.index[-1]})")
    print(f"  1H  bars: {len(df1h)}")
    print(f"  4H  bars: {len(df4h)}")

    print("Scoring each timeframe...")
    s15 = score_timeframe(df15)
    s1h = score_timeframe(df1h)
    s4h = score_timeframe(df4h)

    print("Combining top-down (4H+1H+15m)...")
    scores = combined_score(s15, s1h, s4h).dropna()
    print(f"  scored bars: {len(scores)}")
    print(f"  score range: {scores['score'].min():.0f} .. {scores['score'].max():.0f}")
    print(f"  mean |score|: {scores['score'].abs().mean():.2f}")

    # ------------------------------------------------------------------
    # Run backtests for several configurations
    # ------------------------------------------------------------------
    configs = [
        {"expiry": 1, "min_score": 14},
        {"expiry": 2, "min_score": 14},
        {"expiry": 3, "min_score": 14},
        {"expiry": 1, "min_score": 20},
        {"expiry": 2, "min_score": 20},
        {"expiry": 3, "min_score": 20},
        {"expiry": 1, "min_score": 28},
        {"expiry": 2, "min_score": 28},
        {"expiry": 3, "min_score": 28},
    ]
    print("\n" + "=" * 70)
    print(f"{'Expiry':>7} {'MinScore':>9} {'Trades':>7} {'WinRate':>9} {'AvgPips':>9} {'NetPips':>10}")
    print("=" * 70)
    summary_rows = []
    for cfg in configs:
        trades = backtest(scores, expiry_hours=cfg["expiry"], min_abs_score=cfg["min_score"])
        if len(trades) == 0:
            continue
        wr = trades["win"].mean() * 100
        avg_pips = trades["diff_pips"].abs().mean()
        # signed pips (positive if direction correct)
        signed = np.where(trades["direction"] == "BUY", trades["diff_pips"], -trades["diff_pips"])
        net = signed.sum()
        print(f"{cfg['expiry']:>5}h  {cfg['min_score']:>8}  {len(trades):>7}  {wr:>7.2f}%  {avg_pips:>7.2f}  {net:>9.1f}")
        summary_rows.append(
            {
                "expiry": cfg["expiry"],
                "min_score": cfg["min_score"],
                "trades": len(trades),
                "win_rate": wr,
                "avg_pips": avg_pips,
                "net_pips": net,
            }
        )
    print("=" * 70)

    # ------------------------------------------------------------------
    # Win rate by score bucket (expiry=2h)
    # ------------------------------------------------------------------
    print("\n--- Win rate by |Score| bucket (expiry = 2 hours) ---")
    trades = backtest(scores, expiry_hours=2, min_abs_score=8)
    trades["abs_score"] = trades["score"].abs()
    bins = [8, 14, 20, 28, 36, 44]
    labels = ["8-13", "14-19", "20-27", "28-35", "36-44"]
    trades["bucket"] = pd.cut(trades["abs_score"], bins=bins, labels=labels, include_lowest=True)
    grp = trades.groupby("bucket", observed=True).agg(
        trades=("win", "count"),
        win_rate=("win", lambda x: x.mean() * 100),
    )
    print(grp.to_string())

    # save full trade log
    trades.to_csv("/home/ubuntu/edge_backtest/trades.csv", index=False)
    pd.DataFrame(summary_rows).to_csv("/home/ubuntu/edge_backtest/summary.csv", index=False)
    print("\nSaved: /home/ubuntu/edge_backtest/trades.csv and summary.csv")


if __name__ == "__main__":
    import sys

    sym = sys.argv[1] if len(sys.argv) > 1 else "EURJPY=X"
    run(sym)
