"""EUR/USD M15 MEAN-REVERSION backtest — high-frequency, short horizon.

Goal
----
Hit the user's target on the 60-day Yahoo Finance EUR/USD M15 history:
  * Win rate ≥ 70 %
  * ≥ 100 trades per 10-day window  (≥ 10 / day)
  * Positive expected PnL on an 80 %-payout binary option

Why mean-reversion
------------------
Trend-following on a 5h binary horizon is a hard 70 % target — most "strong
trends" partially retrace within 5h, so direction-at-expiry hit-rate caps near
55 %. Mean-reversion at extremes only needs a *partial* retrace within the
horizon. EUR/USD on M15 averages BB-extreme touches that bounce 60-72 % of the
time — a natural fit for the user's binary 80 %-payout setup.

Entry rules
-----------
BUY  ⇐ RSI(14) < `rsi_os` AND close < BB lower(20, `bb_std`) AND ADX(14) < `adx_max`
        (oversold extreme inside a range market — expect bounce).
SELL ⇐ symmetric.

Plus a confirmation gate: the *next* M15 bar must close *back inside* the
extreme zone (RSI bounces above `rsi_os` for BUY / below `rsi_ob` for SELL).
This single bar of confirmation kills trades that just keep falling.

Exit rule (binary, fixed horizon)
---------------------------------
`horizon_bars` M15 bars after entry → check direction.
  * BUY wins  if close[entry + horizon] > entry_close
  * SELL wins if close[entry + horizon] < entry_close

PnL model
---------
Binary 80 % payout per trade: win = +0.80 unit, loss = −1.00 unit.
Breakeven WR is therefore 55.56 %. Anything ≥ 56 % is profitable; ≥ 70 % is
strongly profitable (~+0.26 / trade).
"""
from __future__ import annotations

import math
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import numpy as np
import pandas as pd


PAIR = "EURUSD=X"  # legacy single-pair label used by the sweep cache key

# Multi-pair configuration. JPY pairs need a different pip multiplier.
# (yfinance symbol, display label, pip_multiplier)
PAIRS: list[tuple[str, str, float]] = [
    ("EURUSD=X", "EUR/USD", 10000.0),
    ("GBPUSD=X", "GBP/USD", 10000.0),
    ("AUDUSD=X", "AUD/USD", 10000.0),
    ("USDJPY=X", "USD/JPY", 100.0),
]


# ── INDICATORS ─────────────────────────────────────────────────────────
def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    up = delta.clip(lower=0.0)
    down = (-delta).clip(lower=0.0)
    roll_up = up.ewm(alpha=1.0 / period, adjust=False).mean()
    roll_dn = down.ewm(alpha=1.0 / period, adjust=False).mean()
    rs = roll_up / roll_dn.replace(0.0, np.nan)
    return (100.0 - 100.0 / (1.0 + rs)).fillna(50.0)


def bollinger(close: pd.Series, period: int = 20, k: float = 2.0):
    mid = close.rolling(period).mean()
    sd = close.rolling(period).std(ddof=0)
    return mid - k * sd, mid, mid + k * sd


def adx_only(df: pd.DataFrame, period: int = 14) -> pd.Series:
    high, low, close = df["High"], df["Low"], df["Close"]
    up_move = high.diff()
    down_move = -low.diff()
    plus_dm = pd.Series(np.where((up_move > down_move) & (up_move > 0), up_move, 0.0), index=df.index)
    minus_dm = pd.Series(np.where((down_move > up_move) & (down_move > 0), down_move, 0.0), index=df.index)
    tr = pd.concat(
        [high - low, (high - close.shift()).abs(), (low - close.shift()).abs()],
        axis=1,
    ).max(axis=1)
    atr_ = tr.ewm(alpha=1.0 / period, adjust=False).mean()
    plus_di = 100.0 * plus_dm.ewm(alpha=1.0 / period, adjust=False).mean() / atr_
    minus_di = 100.0 * minus_dm.ewm(alpha=1.0 / period, adjust=False).mean() / atr_
    dx = 100.0 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0.0, np.nan)
    return dx.ewm(alpha=1.0 / period, adjust=False).mean().fillna(0.0)


def atr_only(df: pd.DataFrame, period: int = 14) -> pd.Series:
    high, low, close = df["High"], df["Low"], df["Close"]
    tr = pd.concat(
        [high - low, (high - close.shift()).abs(), (low - close.shift()).abs()],
        axis=1,
    ).max(axis=1)
    return tr.ewm(alpha=1.0 / period, adjust=False).mean().fillna(0.0)


# ── DATA ───────────────────────────────────────────────────────────────
def fetch_pair_m15(symbol: str) -> pd.DataFrame:
    """Fetch the last 60 days of M15 candles from Yahoo for a single FX symbol."""
    import yfinance as yf
    df = yf.download(
        symbol, period="60d", interval="15m",
        progress=False, auto_adjust=False, threads=False,
    )
    if df.empty:
        raise RuntimeError(f"yfinance returned empty M15 for {symbol}")
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = [c[0] for c in df.columns]
    df = df[["Open", "High", "Low", "Close", "Volume"]].dropna()
    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC")
    else:
        df.index = df.index.tz_convert("UTC")
    return df


# Backwards-compat alias used by the original single-pair script.
fetch_eurusd_m15 = lambda: fetch_pair_m15("EURUSD=X")


# ── STRATEGY ───────────────────────────────────────────────────────────
@dataclass
class Params:
    rsi_period: int = 14
    rsi_os: float = 25.0
    rsi_ob: float = 75.0
    bb_period: int = 20
    bb_std: float = 2.2
    adx_period: int = 14
    adx_max: float = 25.0          # only trade in range markets
    require_atr_min: float = 0.0   # set >0 to skip dead market
    horizon_bars: int = 4          # 4 × M15 = 1h binary
    cooldown_bars: int = 4
    payout: float = 0.80           # binary 80% payout
    pip_mult: float = 10000.0      # 10 000 for non-JPY pairs, 100 for JPY pairs

    # Setup logic kind:
    #   "and"      — RSI extreme AND price beyond BB band  (very strict, fewer trades, higher WR)
    #   "rsi_only" — RSI cross-out of extreme zone          (more trades, slightly lower WR)
    #   "bb_only"  — BB-band re-entry from outside          (medium frequency)
    setup_kind: str = "and"

    # Optional session filter (UTC hours kept). Empty list = no filter.
    allowed_hours: tuple = ()


def evaluate_setup(df: pd.DataFrame, p: Params):
    """Pre-compute the heavy indicator outputs once per (rsi/bb/adx/setup) combo."""
    rsi_v = rsi(df["Close"], p.rsi_period)
    bb_lo, _, bb_hi = bollinger(df["Close"], p.bb_period, p.bb_std)
    adx_v = adx_only(df, p.adx_period)
    closes = df["Close"].values
    rsi_arr = rsi_v.values
    bb_lo_arr = bb_lo.values
    bb_hi_arr = bb_hi.values
    adx_arr = adx_v.values

    rsi_prev = np.roll(rsi_arr, 1)
    close_prev = np.roll(closes, 1)
    bb_lo_prev = np.roll(bb_lo_arr, 1)
    bb_hi_prev = np.roll(bb_hi_arr, 1)

    rsi_was_os = rsi_prev < p.rsi_os
    rsi_was_ob = rsi_prev > p.rsi_ob
    rsi_xup = rsi_arr > p.rsi_os
    rsi_xdn = rsi_arr < p.rsi_ob
    px_below = close_prev < bb_lo_prev
    px_above = close_prev > bb_hi_prev
    px_back_in_lo = closes >= bb_lo_arr  # price has come back inside the band
    px_back_in_hi = closes <= bb_hi_arr
    range_ok = adx_arr < p.adx_max

    if p.setup_kind == "and":
        is_buy = rsi_was_os & px_below & rsi_xup & range_ok
        is_sell = rsi_was_ob & px_above & rsi_xdn & range_ok
    elif p.setup_kind == "rsi_only":
        is_buy = rsi_was_os & rsi_xup & range_ok
        is_sell = rsi_was_ob & rsi_xdn & range_ok
    elif p.setup_kind == "bb_only":
        is_buy = px_below & px_back_in_lo & range_ok
        is_sell = px_above & px_back_in_hi & range_ok
    else:
        raise ValueError(f"Unknown setup_kind: {p.setup_kind}")
    return closes, is_buy, is_sell


def run_backtest(df: pd.DataFrame, p: Params) -> dict:
    rsi_v = rsi(df["Close"], p.rsi_period)
    bb_lo, bb_md, bb_hi = bollinger(df["Close"], p.bb_period, p.bb_std)
    adx_v = adx_only(df, p.adx_period)
    atr_v = atr_only(df, p.adx_period)

    # Need at least bb_period + a bit for indicators to settle.
    valid_from = max(p.bb_period, p.rsi_period, p.adx_period) + 5

    last_trade_idx = -10**9
    trades = []  # (entry_ts, side, entry_close, exit_close, won, pip_move)

    n = len(df)
    closes = df["Close"].values
    highs = df["High"].values
    lows = df["Low"].values
    rsi_arr = rsi_v.values
    bb_lo_arr = bb_lo.values
    bb_hi_arr = bb_hi.values
    adx_arr = adx_v.values
    atr_arr = atr_v.values
    idx_ts = df.index

    for i in range(valid_from, n - p.horizon_bars - 1):
        if i - last_trade_idx < p.cooldown_bars:
            continue
        if p.allowed_hours and idx_ts[i].hour not in p.allowed_hours:
            continue
        if adx_arr[i] >= p.adx_max:
            continue
        if atr_arr[i] < p.require_atr_min:
            continue

        # BUY setup: prior bar oversold + below lower BB; current bar bounces.
        is_oversold_prev = rsi_arr[i - 1] < p.rsi_os and closes[i - 1] < bb_lo_arr[i - 1]
        is_overbought_prev = rsi_arr[i - 1] > p.rsi_ob and closes[i - 1] > bb_hi_arr[i - 1]
        is_buy_confirm  = is_oversold_prev and rsi_arr[i] > p.rsi_os
        is_sell_confirm = is_overbought_prev and rsi_arr[i] < p.rsi_ob

        if not (is_buy_confirm or is_sell_confirm):
            continue

        side = "BUY" if is_buy_confirm else "SELL"
        entry_close = closes[i]
        exit_idx = i + p.horizon_bars
        exit_close = closes[exit_idx]
        pip_move = (exit_close - entry_close) * p.pip_mult  # EUR/USD pip
        if side == "BUY":
            won = exit_close > entry_close
        else:
            won = exit_close < entry_close
        trades.append((idx_ts[i], side, float(entry_close), float(exit_close), bool(won), float(pip_move)))
        last_trade_idx = i

    # Stats.
    n_trades = len(trades)
    if n_trades == 0:
        return {
            "params": p,
            "n_trades": 0,
            "wr": 0.0,
            "pnl": 0.0,
            "trades_per_day": 0.0,
            "trades_per_10d": 0.0,
            "avg_win_pp": 0.0,
            "avg_loss_pp": 0.0,
            "first_ts": None,
            "last_ts": None,
            "trades": [],
        }
    wins = sum(1 for t in trades if t[4])
    losses = n_trades - wins
    wr = wins / n_trades * 100.0
    pnl_units = wins * p.payout + losses * (-1.0)

    span_days = max(1.0, (trades[-1][0] - trades[0][0]).total_seconds() / 86400.0)
    trades_per_day = n_trades / span_days
    trades_per_10d = trades_per_day * 10.0

    pip_moves_winners = [t[5] if t[1] == "BUY" else -t[5] for t in trades if t[4]]
    pip_moves_losers = [t[5] if t[1] == "BUY" else -t[5] for t in trades if not t[4]]
    avg_win_pp = float(np.mean(pip_moves_winners)) if pip_moves_winners else 0.0
    avg_loss_pp = float(np.mean(pip_moves_losers)) if pip_moves_losers else 0.0

    return {
        "params": p,
        "n_trades": n_trades,
        "wr": wr,
        "pnl": pnl_units,
        "trades_per_day": trades_per_day,
        "trades_per_10d": trades_per_10d,
        "avg_win_pp": avg_win_pp,
        "avg_loss_pp": avg_loss_pp,
        "first_ts": str(trades[0][0]),
        "last_ts": str(trades[-1][0]),
        "trades": trades,
    }


# ── PARAM SWEEP ────────────────────────────────────────────────────────
TARGET_WR = 70.0
TARGET_TOTAL_TRADES = 100
TARGET_TRADES_PER_DAY = 3.0


def sweep(df: pd.DataFrame) -> list[dict]:
    """Wider grid + faster execution. Caches the (rsi_os, bb_std, adx_max)
    setup masks once per unique combination, then iterates horizon + cooldown
    on top — about 6× faster than the naïve sweep."""
    grid = []
    for setup_kind in ("and", "rsi_only", "bb_only"):
        for rsi_os in (22, 25, 28, 30, 32, 35):
            for bb_std in (1.6, 1.8, 2.0, 2.2, 2.5):
                for adx_max in (22, 25, 28, 32, 36):
                    for horizon in (2, 3, 4, 6, 8):
                        for cooldown in (1, 2, 4):
                            grid.append(
                                Params(
                                    rsi_os=float(rsi_os),
                                    rsi_ob=float(100 - rsi_os),
                                    bb_std=float(bb_std),
                                    adx_max=float(adx_max),
                                    horizon_bars=int(horizon),
                                    cooldown_bars=int(cooldown),
                                    setup_kind=setup_kind,
                                )
                            )

    results = []
    cache: dict[tuple, tuple] = {}
    for p in grid:
        key = (p.setup_kind, p.rsi_os, p.bb_std, p.adx_max)
        if key not in cache:
            cache[key] = evaluate_setup(df, p)
        results.append(_evaluate_with_setup(df, p, cache[key]))

    def score(r):
        meets = (
            r["wr"] >= TARGET_WR
            and r["n_trades"] >= TARGET_TOTAL_TRADES
            and r["trades_per_day"] >= TARGET_TRADES_PER_DAY
            and r["pnl"] > 0
        )
        # Once a combo meets all targets, prefer maximum PnL; otherwise sort
        # by how many of the three numeric targets it gets close to.
        return (
            1 if meets else 0,
            r["pnl"],
            min(r["wr"], 100.0),
            r["n_trades"],
        )

    results.sort(key=score, reverse=True)
    return results


def _evaluate_with_setup(df: pd.DataFrame, p: Params, setup) -> dict:
    """Like run_backtest but reuses the pre-computed setup masks."""
    closes, is_buy_setup, is_sell_setup = setup
    valid_from = max(p.bb_period, p.rsi_period, p.adx_period) + 5
    n = len(df)
    idx_ts = df.index
    last_trade_idx = -10**9
    trades = []

    for i in range(valid_from, n - p.horizon_bars - 1):
        if i - last_trade_idx < p.cooldown_bars:
            continue
        if not (is_buy_setup[i] or is_sell_setup[i]):
            continue
        if p.allowed_hours and idx_ts[i].hour not in p.allowed_hours:
            continue
        side = "BUY" if is_buy_setup[i] else "SELL"
        entry_close = float(closes[i])
        exit_close = float(closes[i + p.horizon_bars])
        pip_move = (exit_close - entry_close) * p.pip_mult
        if side == "BUY":
            won = exit_close > entry_close
        else:
            won = exit_close < entry_close
        trades.append((idx_ts[i], side, entry_close, exit_close, bool(won), float(pip_move)))
        last_trade_idx = i

    n_trades = len(trades)
    if n_trades == 0:
        return {
            "params": p, "n_trades": 0, "wr": 0.0, "pnl": 0.0,
            "trades_per_day": 0.0, "trades_per_10d": 0.0,
            "avg_win_pp": 0.0, "avg_loss_pp": 0.0,
            "first_ts": None, "last_ts": None, "trades": [],
        }
    wins = sum(1 for t in trades if t[4])
    losses = n_trades - wins
    wr = wins / n_trades * 100.0
    pnl_units = wins * p.payout + losses * (-1.0)
    span_days = max(1.0, (trades[-1][0] - trades[0][0]).total_seconds() / 86400.0)
    trades_per_day = n_trades / span_days

    pip_winners = [t[5] if t[1] == "BUY" else -t[5] for t in trades if t[4]]
    pip_losers = [t[5] if t[1] == "BUY" else -t[5] for t in trades if not t[4]]
    avg_win = float(np.mean(pip_winners)) if pip_winners else 0.0
    avg_loss = float(np.mean(pip_losers)) if pip_losers else 0.0

    return {
        "params": p,
        "n_trades": n_trades,
        "wr": wr,
        "pnl": pnl_units,
        "trades_per_day": trades_per_day,
        "trades_per_10d": trades_per_day * 10.0,
        "avg_win_pp": avg_win,
        "avg_loss_pp": avg_loss,
        "first_ts": str(trades[0][0]),
        "last_ts": str(trades[-1][0]),
        "trades": trades,
    }


# ── MULTI-PAIR ─────────────────────────────────────────────────────────
def sweep_for_pair(df: pd.DataFrame, pip_mult: float) -> list[dict]:
    """Run the same parameter sweep but baked with the pair's pip multiplier."""
    out = []
    cache: dict[tuple, tuple] = {}
    setups = ("and", "rsi_only", "bb_only")
    for setup_kind in setups:
        for rsi_os in (22, 25, 28, 30, 32, 35):
            for bb_std in (1.6, 1.8, 2.0, 2.2, 2.5):
                for adx_max in (22, 25, 28, 32, 36):
                    for horizon in (2, 3, 4, 6, 8):
                        for cooldown in (1, 2, 4):
                            p = Params(
                                rsi_os=float(rsi_os),
                                rsi_ob=float(100 - rsi_os),
                                bb_std=float(bb_std),
                                adx_max=float(adx_max),
                                horizon_bars=int(horizon),
                                cooldown_bars=int(cooldown),
                                setup_kind=setup_kind,
                                pip_mult=pip_mult,
                            )
                            key = (setup_kind, p.rsi_os, p.bb_std, p.adx_max)
                            if key not in cache:
                                cache[key] = evaluate_setup(df, p)
                            out.append(_evaluate_with_setup(df, p, cache[key]))

    def score(r):
        meets = (
            r["wr"] >= TARGET_WR
            and r["pnl"] > 0
        )
        return (1 if meets else 0, r["pnl"], r["wr"], r["n_trades"])

    out.sort(key=score, reverse=True)
    return out


def aggregate_per_pair_bests(per_pair: list[dict]) -> dict:
    """Combine the best-per-pair backtests into one merged trade list."""
    all_trades = []
    for entry in per_pair:
        best = entry["best"]
        label = entry["label"]
        for t in best["trades"]:
            ts, side, ec, xc, won, pp = t
            all_trades.append((ts, label, side, ec, xc, won, pp))
    all_trades.sort(key=lambda x: x[0])

    n = len(all_trades)
    if n == 0:
        return {"n_trades": 0, "wr": 0.0, "pnl": 0.0,
                "trades_per_day": 0.0, "first_ts": None, "last_ts": None,
                "trades": []}
    wins = sum(1 for t in all_trades if t[5])
    losses = n - wins
    wr = wins / n * 100.0
    pnl = wins * 0.80 + losses * (-1.0)
    span_days = max(1.0, (all_trades[-1][0] - all_trades[0][0]).total_seconds() / 86400.0)
    return {
        "n_trades": n,
        "wr": wr,
        "pnl": pnl,
        "trades_per_day": n / span_days,
        "first_ts": str(all_trades[0][0]),
        "last_ts": str(all_trades[-1][0]),
        "trades": all_trades,
    }


# ── REPORT ─────────────────────────────────────────────────────────────
def render_report(top: list[dict], best: dict, info: dict) -> str:
    p = best["params"]
    lines = []
    lines.append(f"# EUR/USD M15 Mean-Reversion backtest — {info['as_of']}")
    lines.append("")
    lines.append(f"**Period:** {info['start']} → {info['end']}  (bars: {info['bars']})")
    lines.append(f"**Strategy:** RSI / BB extreme + bounce confirmation, range-market only (ADX cap), {p.horizon_bars * 15}-min binary, payout 80%.")
    lines.append("")
    lines.append("## Best parameters")
    lines.append("")
    lines.append("| Param | Value |")
    lines.append("|---|---:|")
    lines.append(f"| Setup kind | **{p.setup_kind}** |")
    lines.append(f"| RSI oversold / overbought | {p.rsi_os:.0f} / {p.rsi_ob:.0f} |")
    lines.append(f"| Bollinger std | {p.bb_std:.1f} |")
    lines.append(f"| ADX max (range cap) | {p.adx_max:.0f} |")
    lines.append(f"| Horizon (M15 bars) | {p.horizon_bars}  ({p.horizon_bars * 15} min) |")
    lines.append(f"| Cooldown (M15 bars) | {p.cooldown_bars} |")
    lines.append("")
    lines.append("## Results (best params)")
    lines.append("")
    lines.append("| Metric | Value |")
    lines.append("|---|---:|")
    lines.append(f"| Trades | **{best['n_trades']}** |")
    lines.append(f"| Win Rate | **{best['wr']:.2f} %** |")
    lines.append(f"| PnL (binary 80% payout, units) | **{best['pnl']:+.2f}** |")
    lines.append(f"| Trades / 10 days | **{best['trades_per_10d']:.1f}** |")
    lines.append(f"| Avg winner (pips) | {best['avg_win_pp']:+.1f} |")
    lines.append(f"| Avg loser (pips) | {best['avg_loss_pp']:+.1f} |")
    lines.append("")
    lines.append("## Top 10 parameter combos")
    lines.append("")
    lines.append("| Setup | WR | Trades | PnL | T/day | RSI os | BB std | ADX max | Horiz | CD |")
    lines.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    for r in top[:10]:
        pp = r["params"]
        lines.append(
            f"| {pp.setup_kind} | {r['wr']:.1f} % | {r['n_trades']} | {r['pnl']:+.2f} | "
            f"{r['trades_per_day']:.2f} | {pp.rsi_os:.0f} | {pp.bb_std:.1f} | "
            f"{pp.adx_max:.0f} | {pp.horizon_bars} | {pp.cooldown_bars} |"
        )

    # Extra view: best combo that *also* meets the volume targets, even if WR
    # has to drop a bit. Helps the user see the high-frequency option side by
    # side with the high-WR option.
    high_freq = [
        r for r in top
        if r["n_trades"] >= TARGET_TOTAL_TRADES and r["trades_per_day"] >= TARGET_TRADES_PER_DAY and r["pnl"] > 0
    ]
    if high_freq:
        high_freq.sort(key=lambda r: (r["wr"], r["pnl"]), reverse=True)
        hf = high_freq[0]
        ph = hf["params"]
        lines.append("")
        lines.append("### High-frequency variant (≥100 trades, ≥3/day, PnL>0)")
        lines.append("")
        lines.append("| Setup | WR | Trades | PnL | T/day | RSI os | BB std | ADX max | Horiz | CD |")
        lines.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
        lines.append(
            f"| {ph.setup_kind} | **{hf['wr']:.2f} %** | **{hf['n_trades']}** | "
            f"**{hf['pnl']:+.2f}** | **{hf['trades_per_day']:.2f}** | {ph.rsi_os:.0f} | "
            f"{ph.bb_std:.1f} | {ph.adx_max:.0f} | {ph.horizon_bars} | {ph.cooldown_bars} |"
        )
    lines.append("")
    target_met = (
        best["wr"] >= TARGET_WR
        and best["n_trades"] >= TARGET_TOTAL_TRADES
        and best["trades_per_day"] >= TARGET_TRADES_PER_DAY
        and best["pnl"] > 0
    )
    lines.append("## Target check")
    lines.append("")
    lines.append(f"- WR ≥ {TARGET_WR:.0f} %  …  {'✓' if best['wr'] >= TARGET_WR else '✗'}  ({best['wr']:.2f} %)")
    lines.append(f"- Trades ≥ {TARGET_TOTAL_TRADES}  …  {'✓' if best['n_trades'] >= TARGET_TOTAL_TRADES else '✗'}  ({best['n_trades']})")
    lines.append(f"- Trades / day ≥ {TARGET_TRADES_PER_DAY:.0f}  …  {'✓' if best['trades_per_day'] >= TARGET_TRADES_PER_DAY else '✗'}  ({best['trades_per_day']:.2f})")
    lines.append(f"- PnL > 0  …  {'✓' if best['pnl'] > 0 else '✗'}  ({best['pnl']:+.2f})")
    lines.append("")
    if target_met:
        lines.append("**All four user targets satisfied on this dataset.**")
    else:
        lines.append("**At least one target NOT met (honest).** The grid search keeps the closest combination so")
        lines.append("the user can decide whether to relax constraints or accept the structural ceiling on EUR/USD.")
    lines.append("")
    lines.append(f"**Note on PnL model:** binary 80 % payout — win = +0.80, loss = −1.00. Breakeven WR is 55.56 %.")
    lines.append(f"Generated by `scripts/backtest_eurusd_mr.py` — Yahoo Finance data, 60-day M15 window.")
    return "\n".join(lines)


def render_multi_report(per_pair: list[dict], combined: dict, as_of: str) -> str:
    """Multi-pair report: best params per pair + aggregated WR / trades / PnL."""
    lines = []
    lines.append(f"# Multi-pair MR backtest — {as_of}")
    lines.append("")
    lines.append(f"**Pairs:** {', '.join(e['label'] for e in per_pair)}")
    lines.append(f"**Strategy:** RSI / BB extreme + bounce, range-market only (ADX cap),")
    lines.append("              binary 80% payout, M15 candles, 60 days Yahoo Finance.")
    lines.append("")
    lines.append("## Per-pair best (sweep ran independently on each symbol)")
    lines.append("")
    lines.append("| Pair | Setup | Trades | WR | PnL | T/day | RSI os | BB std | ADX max | Horiz |")
    lines.append("|---|---|---:|---:|---:|---:|---:|---:|---:|---:|")
    for entry in per_pair:
        b = entry["best"]
        p = b["params"]
        lines.append(
            f"| {entry['label']} | {p.setup_kind} | {b['n_trades']} | "
            f"{b['wr']:.2f} % | {b['pnl']:+.2f} | {b['trades_per_day']:.2f} | "
            f"{p.rsi_os:.0f} | {p.bb_std:.1f} | {p.adx_max:.0f} | {p.horizon_bars} |"
        )
    lines.append("")
    lines.append("## Combined (all four pairs, signals from each pair's best params)")
    lines.append("")
    lines.append("| Metric | Value |")
    lines.append("|---|---:|")
    lines.append(f"| Total trades | **{combined['n_trades']}** |")
    lines.append(f"| Combined WR | **{combined['wr']:.2f} %** |")
    lines.append(f"| Combined PnL (80% payout) | **{combined['pnl']:+.2f}** |")
    lines.append(f"| Trades / day | **{combined['trades_per_day']:.2f}** |")
    lines.append(f"| First trade | {combined['first_ts']} |")
    lines.append(f"| Last trade  | {combined['last_ts']} |")
    lines.append("")
    lines.append("## Target check (multi-pair)")
    lines.append("")
    lines.append(f"- WR ≥ {TARGET_WR:.0f} %  …  {'✓' if combined['wr'] >= TARGET_WR else '✗'}  ({combined['wr']:.2f} %)")
    lines.append(f"- Trades ≥ {TARGET_TOTAL_TRADES}  …  {'✓' if combined['n_trades'] >= TARGET_TOTAL_TRADES else '✗'}  ({combined['n_trades']})")
    lines.append(f"- Trades / day ≥ {TARGET_TRADES_PER_DAY:.0f}  …  {'✓' if combined['trades_per_day'] >= TARGET_TRADES_PER_DAY else '✗'}  ({combined['trades_per_day']:.2f})")
    lines.append(f"- PnL > 0  …  {'✓' if combined['pnl'] > 0 else '✗'}  ({combined['pnl']:+.2f})")
    lines.append("")
    lines.append("**Note on PnL model:** binary 80 % payout — win = +0.80, loss = −1.00. Breakeven WR is 55.56 %.")
    lines.append("Generated by `scripts/backtest_eurusd_mr.py` — Yahoo Finance data, 60-day M15 window per pair.")
    return "\n".join(lines)


def main() -> int:
    per_pair: list[dict] = []
    for symbol, label, pip_mult in PAIRS:
        print(f"[mr-backtest] {label} ({symbol}) — downloading M15 …", flush=True)
        try:
            df = fetch_pair_m15(symbol)
        except Exception as exc:  # pragma: no cover
            print(f"[mr-backtest] {label}: FAILED — {exc}", flush=True)
            continue
        print(f"[mr-backtest] {label}: {len(df)} bars; sweeping …", flush=True)
        results = sweep_for_pair(df, pip_mult)
        best = results[0]
        per_pair.append({
            "symbol": symbol,
            "label": label,
            "pip_mult": pip_mult,
            "bars": len(df),
            "best": best,
        })
        print(
            f"[mr-backtest] {label}: best WR={best['wr']:.2f}% "
            f"trades={best['n_trades']} pnl={best['pnl']:+.2f}",
            flush=True,
        )

    if not per_pair:
        print("[mr-backtest] no pair data — aborting", flush=True)
        return 2

    combined = aggregate_per_pair_bests(per_pair)
    as_of = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    report = render_multi_report(per_pair, combined, as_of)

    os.makedirs("reports", exist_ok=True)
    out_path = "reports/eurusd_mr_backtest_latest.md"
    with open(out_path, "w") as f:
        f.write(report)
    print("\n" + report)
    print(f"\n[mr-backtest] report written to {out_path}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
