"""EUR/USD M15 backtest — mirrors the Pine indicator + dashboard logic.

What it does
------------
Pulls multi-timeframe EUR/USD history (M15 + H1 + H4 + D1) from Yahoo Finance,
recomputes the same voting blocks + trend_quality composite that the live
dashboard uses, classifies every bar as ★ ПРЕМИУМ / СТРОГИЙ / no-signal, and
checks whether the *close 5h later* moved in the predicted direction (binary
forecast win/loss).

Output
------
A markdown report with per-tier WR + trade count + average expected move,
written to ``reports/eurusd_backtest_latest.md``. The CI workflow posts that
file as a comment on the commit.

Run locally
-----------
    python scripts/backtest_eurusd.py

Run on CI
---------
    .github/workflows/backtest.yml triggers this script on every push that
    touches scripts/ or tradingview/ or app/.
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


# ── CONFIG ─────────────────────────────────────────────────────────────
PAIR = "EURUSD=X"
HORIZON_HOURS = 5
HORIZON_M15_BARS = HORIZON_HOURS * 4  # 4 bars per hour on M15

# Strict-tier thresholds (mirror app/config.py).
MIN_CONFIDENCE = 82
STRICT_TREND_QUALITY = 75

# Premium-tier thresholds (mirror app/config.py).
PREMIUM_MIN_CONFIDENCE = 88
PREMIUM_TREND_QUALITY = 85
PREMIUM_MIN_ADX = 28.0
PREMIUM_MIN_AROON_OSC = 70.0
PREMIUM_MIN_HA_BULL_EXTREME = 0.83
PREMIUM_MIN_HA_BODY = 0.55
PREMIUM_MIN_MOMENTUM = 0.20
PREMIUM_MIN_MOVE_PIPS_NONJPY = 60.0
PREMIUM_MIN_MOVE_PIPS_JPY = 100.0


# ── INDICATORS ─────────────────────────────────────────────────────────
def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    up = delta.clip(lower=0.0)
    down = (-delta).clip(lower=0.0)
    roll_up = up.ewm(alpha=1.0 / period, adjust=False).mean()
    roll_dn = down.ewm(alpha=1.0 / period, adjust=False).mean()
    rs = roll_up / roll_dn.replace(0.0, np.nan)
    return (100.0 - 100.0 / (1.0 + rs)).fillna(50.0)


def macd_hist(close: pd.Series) -> pd.Series:
    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    macd = ema12 - ema26
    signal = macd.ewm(span=9, adjust=False).mean()
    return macd - signal


def stoch_k(df: pd.DataFrame, period: int = 14) -> pd.Series:
    ll = df["Low"].rolling(period).min()
    hh = df["High"].rolling(period).max()
    return (100.0 * (df["Close"] - ll) / (hh - ll).replace(0.0, np.nan)).fillna(50.0)


def bollinger_pct(close: pd.Series, period: int = 20, k: float = 2.0) -> pd.Series:
    mid = close.rolling(period).mean()
    sd = close.rolling(period).std(ddof=0)
    upper = mid + k * sd
    lower = mid - k * sd
    return ((close - lower) / (upper - lower).replace(0.0, np.nan)).fillna(0.5)


def adx(df: pd.DataFrame, period: int = 14):
    high, low, close = df["High"], df["Low"], df["Close"]
    up_move = high.diff()
    down_move = -low.diff()
    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)
    tr = pd.concat(
        [
            high - low,
            (high - close.shift()).abs(),
            (low - close.shift()).abs(),
        ],
        axis=1,
    ).max(axis=1)
    atr_ = tr.ewm(alpha=1.0 / period, adjust=False).mean()
    plus_di = 100.0 * pd.Series(plus_dm, index=df.index).ewm(alpha=1.0 / period, adjust=False).mean() / atr_
    minus_di = 100.0 * pd.Series(minus_dm, index=df.index).ewm(alpha=1.0 / period, adjust=False).mean() / atr_
    dx = 100.0 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0.0, np.nan)
    adx_ = dx.ewm(alpha=1.0 / period, adjust=False).mean()
    return adx_.fillna(0.0), plus_di.fillna(0.0), minus_di.fillna(0.0), atr_.fillna(0.0)


def williams_r(df: pd.DataFrame, period: int = 14) -> pd.Series:
    hh = df["High"].rolling(period).max()
    ll = df["Low"].rolling(period).min()
    return (-100.0 * (hh - df["Close"]) / (hh - ll).replace(0.0, np.nan)).fillna(-50.0)


def aroon(df: pd.DataFrame, period: int = 25):
    high, low = df["High"], df["Low"]
    days_high = high.rolling(period + 1).apply(
        lambda x: float(period - x.values.argmax()), raw=False
    )
    days_low = low.rolling(period + 1).apply(
        lambda x: float(period - x.values.argmin()), raw=False
    )
    up = 100.0 * (period - days_high) / period
    dn = 100.0 * (period - days_low) / period
    return up.fillna(50.0), dn.fillna(50.0), (up - dn).fillna(0.0)


def cci(df: pd.DataFrame, period: int = 20) -> pd.Series:
    typ = (df["High"] + df["Low"] + df["Close"]) / 3.0
    sma = typ.rolling(period).mean()
    mad = typ.rolling(period).apply(lambda x: float(np.mean(np.abs(x - x.mean()))), raw=False)
    return ((typ - sma) / (0.015 * mad.replace(0.0, np.nan))).fillna(0.0)


def heiken_ashi(df: pd.DataFrame) -> tuple[pd.Series, pd.Series]:
    o, h, l, c = df["Open"], df["High"], df["Low"], df["Close"]
    ha_close = (o + h + l + c) / 4.0
    ha_open = pd.Series(0.0, index=df.index)
    ha_open.iloc[0] = (o.iloc[0] + c.iloc[0]) / 2.0
    for i in range(1, len(df)):
        ha_open.iloc[i] = (ha_open.iloc[i - 1] + ha_close.iloc[i - 1]) / 2.0
    ha_high = pd.concat([h, ha_open, ha_close], axis=1).max(axis=1)
    ha_low = pd.concat([l, ha_open, ha_close], axis=1).min(axis=1)
    bull = (ha_close > ha_open).astype(float)
    body = (ha_close - ha_open).abs()
    rng = (ha_high - ha_low).replace(0.0, np.nan)
    body_pct = (body / rng).fillna(0.0)
    bull_ratio_6 = bull.rolling(6).mean().fillna(0.5)
    body_strength_6 = body_pct.rolling(6).mean().fillna(0.0)
    return bull_ratio_6, body_strength_6


# ── DATA ───────────────────────────────────────────────────────────────
def fetch(period: str, interval: str, symbol: str = PAIR) -> pd.DataFrame:
    import yfinance as yf
    df = yf.download(
        symbol,
        period=period,
        interval=interval,
        progress=False,
        auto_adjust=False,
        threads=False,
    )
    if df.empty:
        raise RuntimeError(f"yfinance returned empty {period}/{interval} for {symbol}")
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = [c[0] for c in df.columns]
    df = df[["Open", "High", "Low", "Close", "Volume"]].dropna()
    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC")
    else:
        df.index = df.index.tz_convert("UTC")
    return df


def tf_dir(close_now: float, ema20: float, ema50: float) -> int:
    if close_now > ema20 > ema50:
        return 1
    if close_now < ema20 < ema50:
        return -1
    return 0


def reindex_dir(htf_close: pd.Series, htf_ema20: pd.Series, htf_ema50: pd.Series, target_index: pd.DatetimeIndex) -> pd.Series:
    """Project a higher-timeframe direction onto the M15 timeline (forward fill)."""
    direction = pd.Series(
        [tf_dir(c, e20, e50) for c, e20, e50 in zip(htf_close, htf_ema20, htf_ema50)],
        index=htf_close.index,
        dtype=float,
    )
    return direction.reindex(target_index, method="ffill").fillna(0.0)


# ── CORE LOGIC ─────────────────────────────────────────────────────────
@dataclass
class BarSignal:
    side: Optional[str]      # "BUY" / "SELL" / None
    confidence: float
    trend_quality: float
    expected_move_pips_5h: float
    is_strict: bool
    is_premium: bool


def classify_dataframe(
    m15: pd.DataFrame,
    h1: pd.DataFrame,
    h4: pd.DataFrame,
    d1: pd.DataFrame,
    pip_mult: float = 10000.0,
    min_move_pips: float = PREMIUM_MIN_MOVE_PIPS_NONJPY,
) -> pd.DataFrame:
    """Compute the full system output for every M15 bar.

    The decision indicators run on H1 (matching app/analyzer.py), but the
    multi-TF check uses D1+H4+H1+M15. Heiken Ashi is computed on M15.

    `pip_mult` is the multiplier to convert price moves into pips:
      * 10000 for non-JPY pairs (1 pip = 0.0001)
      * 100   for JPY pairs     (1 pip = 0.01)
    `min_move_pips` is the premium-tier minimum expected 5h ATR move."""
    # Per-TF EMAs for alignment.
    for df in (d1, h4, h1, m15):
        df["EMA20"] = df["Close"].ewm(span=20, adjust=False).mean()
        df["EMA50"] = df["Close"].ewm(span=50, adjust=False).mean()

    dir_d1  = reindex_dir(d1["Close"],  d1["EMA20"],  d1["EMA50"],  m15.index)
    dir_h4  = reindex_dir(h4["Close"],  h4["EMA20"],  h4["EMA50"],  m15.index)
    dir_h1  = reindex_dir(h1["Close"],  h1["EMA20"],  h1["EMA50"],  m15.index)
    dir_m15 = pd.Series(
        [tf_dir(c, e20, e50) for c, e20, e50 in zip(m15["Close"], m15["EMA20"], m15["EMA50"])],
        index=m15.index, dtype=float,
    )
    bull_count = (dir_d1.eq(1).astype(int) + dir_h4.eq(1).astype(int) + dir_h1.eq(1).astype(int) + dir_m15.eq(1).astype(int))
    bear_count = (dir_d1.eq(-1).astype(int) + dir_h4.eq(-1).astype(int) + dir_h1.eq(-1).astype(int) + dir_m15.eq(-1).astype(int))
    multi_tf_aligned = (bull_count >= 3) | (bear_count >= 3)
    mtf_dir = pd.Series(0, index=m15.index, dtype=float)
    mtf_dir[bull_count >= 3] = 1.0
    mtf_dir[bear_count >= 3] = -1.0

    # H1 indicator block (decision TF).
    h1_rsi   = rsi(h1["Close"], 14)
    h1_macdh = macd_hist(h1["Close"])
    h1_k     = stoch_k(h1, 14)
    h1_d     = h1_k.rolling(3).mean()
    h1_bb    = bollinger_pct(h1["Close"], 20, 2.0)
    h1_adx, h1_dp, h1_dm, h1_atr = adx(h1, 14)
    h1_atr_avg = h1_atr.rolling(50, min_periods=10).mean()
    h1_wpr   = williams_r(h1, 14)
    h1_mom   = (h1["Close"] - h1["Close"].shift(12)) / h1["Close"].shift(12) * 100.0
    h1_au, h1_ad, h1_ao = aroon(h1, 25)
    h1_cci   = cci(h1, 20)

    # Project H1 outputs onto M15 timeline.
    def proj(s: pd.Series) -> pd.Series:
        return s.reindex(m15.index, method="ffill")

    rsi_  = proj(h1_rsi)
    macdh = proj(h1_macdh)
    k_    = proj(h1_k)
    d_    = proj(h1_d)
    bb_   = proj(h1_bb)
    adx_  = proj(h1_adx)
    dp_   = proj(h1_dp)
    dm_   = proj(h1_dm)
    atr_  = proj(h1_atr)
    atr_avg_ = proj(h1_atr_avg)
    wpr_  = proj(h1_wpr)
    mom_  = proj(h1_mom)
    aro_  = proj(h1_ao)
    cci_  = proj(h1_cci)

    # Heiken Ashi on M15.
    ha_bull_ratio_6, ha_body_strength_6 = heiken_ashi(m15)

    # ── Voting blocks (mirror analyzer.py + Pine indicator) ───────────
    def vote(buy: pd.Series, sell: pd.Series, w: float) -> pd.Series:
        s = pd.Series(0.0, index=m15.index)
        s[buy] = w
        s[sell] = -w
        return s

    score = (
        vote(rsi_ > 55, rsi_ < 45, 2.0)                                # B  RSI
        + vote(macdh > 0, macdh < 0, 2.5)                              # C  MACD
        + vote((k_ > d_) & (k_ < 80), (k_ < d_) & (k_ > 20), 1.5)       # D  Stoch
        + vote(bb_ > 0.6, bb_ < 0.4, 1.5)                              # E  BB
        + vote((adx_ > 20) & (dp_ > dm_), (adx_ > 20) & (dm_ > dp_), 3.0) # F  ADX/DI
        + vote(wpr_ > -20, wpr_ < -80, 1.5)                            # G  WPR
        + vote(mom_ > 0.05, mom_ < -0.05, 2.0)                         # H  Momentum
        + vote(mtf_dir == 1, mtf_dir == -1, 4.0)                       # I  Multi-TF
        + vote(aro_ > 50, aro_ < -50, 2.0)                             # P  Aroon
        + vote(cci_ > 100, cci_ < -100, 2.0)                           # Q  CCI
        + vote(
            (ha_bull_ratio_6 >= 0.83) & (ha_body_strength_6 >= 0.5),
            (ha_bull_ratio_6 <= 0.17) & (ha_body_strength_6 >= 0.5), 2.0
        )                                                              # R  HA
        # Block A (EMA20 vs EMA50 H1) using the projected H1 EMAs.
        + vote(proj(h1["EMA20"]) > proj(h1["EMA50"]),
               proj(h1["EMA20"]) < proj(h1["EMA50"]), 3.0)
    )
    max_score = 2.0 + 2.5 + 1.5 + 1.5 + 3.0 + 1.5 + 2.0 + 4.0 + 2.0 + 2.0 + 2.0 + 3.0
    score_ratio = score.abs() / max_score
    confidence = (50.0 + 45.0 * (1.0 - np.exp(-3.66 * score_ratio))).clip(50.0, 92.0)

    # Trend quality components.
    def scale(v, lo, hi):
        return ((v - lo) * 100.0 / (hi - lo)).clip(0.0, 100.0)

    q_score = scale(score_ratio, 0.0, 0.40)
    q_adx   = scale(adx_, 15.0, 30.0)
    q_aroon = scale(aro_.abs(), 30.0, 70.0)
    q_ha_dir = (ha_bull_ratio_6 - 0.5).abs() * 2.0
    q_ha    = (q_ha_dir * 100.0 * (0.5 + 0.5 * ha_body_strength_6)).clip(0.0, 100.0)
    q_mtf   = pd.Series(0.0, index=m15.index)
    q_mtf[(bull_count == 4) | (bull_count == 0)] = 100.0
    q_mtf[(bull_count == 3) | (bull_count == 1)] = 50.0
    q_atr   = scale(atr_ / atr_avg_.replace(0.0, np.nan), 0.7, 1.5).fillna(0.0)
    q_mom   = scale(mom_.abs(), 0.05, 0.30)
    trend_quality = (q_score + q_adx + q_aroon + q_ha + q_mtf + q_atr + q_mom) / 7.0

    expected_move_pips_5h = atr_ * 5.0 * pip_mult

    # ── Classification ───────────────────────────────────────────────
    side = pd.Series(None, index=m15.index, dtype=object)
    side[score > 0] = "BUY"
    side[score < 0] = "SELL"

    is_strict_buy = (side == "BUY") & (confidence >= MIN_CONFIDENCE) & (trend_quality >= STRICT_TREND_QUALITY) & multi_tf_aligned & (mtf_dir == 1)
    is_strict_sell = (side == "SELL") & (confidence >= MIN_CONFIDENCE) & (trend_quality >= STRICT_TREND_QUALITY) & multi_tf_aligned & (mtf_dir == -1)
    is_strict = is_strict_buy | is_strict_sell

    is_prem_common = (
        (trend_quality >= PREMIUM_TREND_QUALITY)
        & (confidence >= PREMIUM_MIN_CONFIDENCE)
        & (adx_ >= PREMIUM_MIN_ADX)
        & (aro_.abs() >= PREMIUM_MIN_AROON_OSC)
        & (ha_body_strength_6 >= PREMIUM_MIN_HA_BODY)
        & (mom_.abs() >= PREMIUM_MIN_MOMENTUM)
        & (expected_move_pips_5h >= min_move_pips)
        & multi_tf_aligned
    )
    is_prem_buy = is_strict_buy & is_prem_common & (ha_bull_ratio_6 >= PREMIUM_MIN_HA_BULL_EXTREME)
    is_prem_sell = is_strict_sell & is_prem_common & (ha_bull_ratio_6 <= 1.0 - PREMIUM_MIN_HA_BULL_EXTREME)
    is_premium = is_prem_buy | is_prem_sell

    out = pd.DataFrame({
        "Close": m15["Close"],
        "side": side,
        "confidence": confidence,
        "trend_quality": trend_quality,
        "expected_move_pips_5h": expected_move_pips_5h,
        "is_strict": is_strict,
        "is_premium": is_premium,
        "bull_count": bull_count,
        "bear_count": bear_count,
        "adx": adx_,
        "aroon_osc": aro_,
        "ha_bull_ratio_6": ha_bull_ratio_6,
        "ha_body_strength_6": ha_body_strength_6,
        "momentum": mom_,
    })
    return out.dropna(subset=["Close"])


def evaluate_horizon(df: pd.DataFrame, horizon_bars: int) -> pd.DataFrame:
    """Add `future_close` and `win` columns based on a horizon (binary forecast)."""
    df = df.copy()
    df["future_close"] = df["Close"].shift(-horizon_bars)
    won_buy = (df["side"] == "BUY") & (df["future_close"] > df["Close"])
    won_sell = (df["side"] == "SELL") & (df["future_close"] < df["Close"])
    # Use object dtype so we can store NaN for bars without a settled future.
    win = pd.Series(np.where(df["future_close"].isna(), np.nan, (won_buy | won_sell).astype(float)), index=df.index)
    df["win"] = win
    return df


def fresh_only(df: pd.DataFrame, col: str) -> pd.Series:
    """Return a boolean mask that is True only on the bar where `col` first
    flipped from False to True (consecutive runs collapsed into one entry).
    Mirrors the Pine "fresh" trigger so we don't double-count back-to-back
    bars in the same regime."""
    s = df[col].astype(bool).fillna(False)
    return s & ~s.shift(1, fill_value=False)


# ── REPORT ─────────────────────────────────────────────────────────────
def stats_block(df: pd.DataFrame, mask: pd.Series, label: str) -> dict:
    sub = df[mask & df["win"].notna()]
    n = int(len(sub))
    if n == 0:
        return {"label": label, "trades": 0, "wr": float("nan"), "avg_move_pp": float("nan"), "trades_per_day": float("nan")}
    wr = float(sub["win"].mean()) * 100.0
    avg_move = float(sub["expected_move_pips_5h"].mean())
    span_days = max(1.0, (sub.index[-1] - sub.index[0]).total_seconds() / 86400.0)
    return {"label": label, "trades": n, "wr": wr, "avg_move_pp": avg_move, "trades_per_day": n / span_days}


def render_report(stats: list[dict], info: dict) -> str:
    lines = []
    lines.append(f"# EUR/USD M15 backtest — {info['as_of']}")
    lines.append("")
    lines.append(f"**Period:** {info['start']} → {info['end']}  ({info['m15_bars']} M15 bars)")
    lines.append(f"**Horizon:** {HORIZON_HOURS}h binary  ({HORIZON_M15_BARS} bars)")
    lines.append("")
    lines.append("| Tier | Trades | Win Rate | Avg expected move (pips) | Trades/day |")
    lines.append("|---|---:|---:|---:|---:|")
    for s in stats:
        wr = f"{s['wr']:.2f} %" if not math.isnan(s["wr"]) else "n/a"
        avg = f"{s['avg_move_pp']:.1f}" if not math.isnan(s["avg_move_pp"]) else "n/a"
        tpd = f"{s['trades_per_day']:.2f}" if not math.isnan(s["trades_per_day"]) else "n/a"
        lines.append(f"| **{s['label']}** | {s['trades']} | {wr} | {avg} | {tpd} |")
    lines.append("")
    lines.append("**Targets:** WR ≥ 70% (★ ПРЕМИУМ), trades-per-day ≥ 1 on EUR/USD.")
    lines.append("")
    lines.append("Generated by `scripts/backtest_eurusd.py` — pure Python, Yahoo Finance feed.")
    return "\n".join(lines)


def main() -> int:
    print("[backtest] downloading M15 / H1 / H4 / D1 data for EUR/USD …", flush=True)
    m15 = fetch(period="60d", interval="15m")
    h1  = fetch(period="2y",  interval="1h")
    h4  = fetch(period="2y",  interval="1h").resample("4h").agg({"Open": "first", "High": "max", "Low": "min", "Close": "last", "Volume": "sum"}).dropna()
    d1  = fetch(period="5y",  interval="1d")
    print(f"[backtest] bars: M15={len(m15)} H1={len(h1)} H4={len(h4)} D1={len(d1)}", flush=True)

    df = classify_dataframe(m15, h1, h4, d1)
    df = evaluate_horizon(df, HORIZON_M15_BARS)

    fresh_strict = fresh_only(df, "is_strict")
    fresh_premium = fresh_only(df, "is_premium")

    stats = [
        stats_block(df, fresh_premium, "★ ПРЕМИУМ"),
        stats_block(df, fresh_strict & ~df["is_premium"], "СТРОГИЙ (без премиум)"),
        stats_block(df, fresh_strict, "СТРОГИЙ + ПРЕМИУМ"),
    ]

    info = {
        "as_of": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "start": str(df.index[0]),
        "end":   str(df.index[-1]),
        "m15_bars": len(df),
    }
    report_md = render_report(stats, info)
    os.makedirs("reports", exist_ok=True)
    out_path = "reports/eurusd_backtest_latest.md"
    with open(out_path, "w") as f:
        f.write(report_md)
    print("\n" + report_md)
    print(f"\n[backtest] report written to {out_path}", flush=True)

    # Exit code 0 always — this is informational, not gating.
    return 0


if __name__ == "__main__":
    sys.exit(main())
