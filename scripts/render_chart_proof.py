"""Render a TradingView-style chart screenshot to *prove* the indicator works.

Generates a matplotlib PNG that looks like a TradingView M15 chart with the
real ★ ПРЕМИУМ / СТРОГИЙ / ТОП-3 labels overlaid at the bars where the
indicator triggers.

Output:
    reports/chart_proof_<PAIR>.png

Usage:
    python scripts/render_chart_proof.py            # default: EURUSD
    python scripts/render_chart_proof.py GBPUSD
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone

# Headless matplotlib for CI.
import matplotlib
matplotlib.use("Agg")
import matplotlib.dates as mdates
import matplotlib.patches as mpatches
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from backtest_eurusd import (  # noqa: E402
    classify_dataframe,
    evaluate_horizon,
    fetch,
    fresh_only,
    HORIZON_M15_BARS,
    PREMIUM_MIN_MOVE_PIPS_JPY,
    PREMIUM_MIN_MOVE_PIPS_NONJPY,
)


# ── HELPERS ────────────────────────────────────────────────────────────
def is_jpy(pair: str) -> bool:
    return "JPY" in pair


def pip_mult(pair: str) -> float:
    return 100.0 if is_jpy(pair) else 10000.0


def min_move_pips(pair: str) -> float:
    return PREMIUM_MIN_MOVE_PIPS_JPY if is_jpy(pair) else PREMIUM_MIN_MOVE_PIPS_NONJPY


def plot_candles(ax, df: pd.DataFrame) -> None:
    width = 0.012  # ~17 minutes for M15 (matplotlib date units = days)
    for ts, row in df.iterrows():
        x = mdates.date2num(ts)
        is_up = row["Close"] >= row["Open"]
        color = "#26a69a" if is_up else "#ef5350"
        ax.add_line(plt.Line2D([x, x], [row["Low"], row["High"]], color=color, linewidth=0.8, solid_capstyle="butt"))
        body_y = min(row["Open"], row["Close"])
        body_h = abs(row["Close"] - row["Open"]) or (df["High"].iloc[0] - df["Low"].iloc[0]) * 1e-5
        ax.add_patch(mpatches.Rectangle((x - width / 2, body_y), width, body_h,
                                        facecolor=color, edgecolor=color, linewidth=0.5))


def render(pair: str, out_path: str, last_n_bars: int = 480) -> dict:
    """Render the last `last_n_bars` of the pair with overlay markers.
    Returns aggregate stats so the caller can put them in the figure title."""
    symbol = f"{pair}=X"
    print(f"[chart] downloading {symbol} …", flush=True)
    m15 = fetch(period="60d", interval="15m", symbol=symbol)
    h1  = fetch(period="2y",  interval="1h",  symbol=symbol)
    h4 = (
        h1.resample("4h")
        .agg({"Open": "first", "High": "max", "Low": "min", "Close": "last", "Volume": "sum"})
        .dropna()
    )
    d1  = fetch(period="5y",  interval="1d",  symbol=symbol)

    print(f"[chart] classifying {pair} …", flush=True)
    sig = classify_dataframe(
        m15, h1, h4, d1,
        pip_mult=pip_mult(pair),
        min_move_pips=min_move_pips(pair),
    )
    sig = evaluate_horizon(sig, HORIZON_M15_BARS)

    # Re-attach OHLC for the candle renderer (classify_dataframe only keeps Close).
    ohlc = m15[["Open", "High", "Low", "Close"]].rename(columns={"Close": "Close_m15"})
    sig = sig.join(ohlc, how="inner")
    sig["Close"] = sig["Close_m15"]
    sig = sig.drop(columns=["Close_m15"])

    fresh_strict  = fresh_only(sig, "is_strict")
    fresh_premium = fresh_only(sig, "is_premium")

    # Trim to the last `last_n_bars` for a readable chart.
    sig_view = sig.tail(last_n_bars).copy()

    fig, ax = plt.subplots(figsize=(18, 9), facecolor="#131722")
    ax.set_facecolor("#131722")
    plot_candles(ax, sig_view)

    # Overlay markers.
    prem_buy = sig_view[(fresh_premium.reindex(sig_view.index, fill_value=False)) & (sig_view["side"] == "BUY")]
    prem_sell = sig_view[(fresh_premium.reindex(sig_view.index, fill_value=False)) & (sig_view["side"] == "SELL")]
    strict_buy = sig_view[(fresh_strict.reindex(sig_view.index, fill_value=False)) & (sig_view["side"] == "BUY") & ~sig_view["is_premium"]]
    strict_sell = sig_view[(fresh_strict.reindex(sig_view.index, fill_value=False)) & (sig_view["side"] == "SELL") & ~sig_view["is_premium"]]

    if not prem_buy.empty:
        ax.scatter(prem_buy.index, prem_buy["Close"] - (sig_view["High"].mean() - sig_view["Low"].mean()) * 0.10,
                   marker="^", s=260, c="#ffd700", edgecolors="black", linewidths=1.5,
                   label=f"★ ПРЕМИУМ BUY ({len(prem_buy)})", zorder=5)
        for ts, row in prem_buy.iterrows():
            ax.annotate(f"★ {int(row['confidence'])}%/TQ{int(row['trend_quality'])}",
                        xy=(ts, row["Close"]),
                        xytext=(ts, row["Low"] - (sig_view["High"].mean() - sig_view["Low"].mean()) * 0.13),
                        ha="center", fontsize=8, color="#ffd700", weight="bold")
    if not prem_sell.empty:
        ax.scatter(prem_sell.index, prem_sell["Close"] + (sig_view["High"].mean() - sig_view["Low"].mean()) * 0.10,
                   marker="v", s=260, c="#ff8c00", edgecolors="black", linewidths=1.5,
                   label=f"★ ПРЕМИУМ SELL ({len(prem_sell)})", zorder=5)
        for ts, row in prem_sell.iterrows():
            ax.annotate(f"★ {int(row['confidence'])}%/TQ{int(row['trend_quality'])}",
                        xy=(ts, row["Close"]),
                        xytext=(ts, row["High"] + (sig_view["High"].mean() - sig_view["Low"].mean()) * 0.13),
                        ha="center", fontsize=8, color="#ff8c00", weight="bold")
    if not strict_buy.empty:
        ax.scatter(strict_buy.index, strict_buy["Close"] - (sig_view["High"].mean() - sig_view["Low"].mean()) * 0.06,
                   marker="^", s=80, c="#26a69a", edgecolors="white", linewidths=0.8,
                   label=f"СТРОГИЙ BUY ({len(strict_buy)})", zorder=4)
    if not strict_sell.empty:
        ax.scatter(strict_sell.index, strict_sell["Close"] + (sig_view["High"].mean() - sig_view["Low"].mean()) * 0.06,
                   marker="v", s=80, c="#ef5350", edgecolors="white", linewidths=0.8,
                   label=f"СТРОГИЙ SELL ({len(strict_sell)})", zorder=4)

    # Title with stats.
    n_prem = int((fresh_premium & sig["win"].notna()).sum())
    n_strict = int((fresh_strict & sig["win"].notna()).sum())
    wr_prem = float(sig.loc[fresh_premium & sig["win"].notna(), "win"].mean()) * 100 if n_prem else float("nan")
    wr_strict = float(sig.loc[fresh_strict & sig["win"].notna(), "win"].mean()) * 100 if n_strict else float("nan")

    title = (f"{pair}  M15  ·  Forex-wws2277 MAX PRO indicator  ·  "
             f"★ ПРЕМИУМ {n_prem} ({wr_prem:.1f}% WR)   |   СТРОГИЙ {n_strict} ({wr_strict:.1f}% WR)   "
             f"·  full 60-day backtest below; chart shows last {last_n_bars} M15 bars")
    ax.set_title(title, color="#d1d4dc", fontsize=12, weight="bold", pad=14)

    ax.tick_params(colors="#d1d4dc", which="both")
    for spine in ax.spines.values():
        spine.set_color("#363a45")
    ax.grid(True, color="#363a45", linewidth=0.5, linestyle="-", alpha=0.6)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%m-%d %H:%M"))
    plt.setp(ax.xaxis.get_majorticklabels(), rotation=30, ha="right", color="#d1d4dc")
    ax.set_ylabel("Price", color="#d1d4dc")
    ax.legend(loc="upper left", frameon=True, facecolor="#1e222d", edgecolor="#363a45", labelcolor="#d1d4dc", fontsize=10)

    fig.tight_layout()
    fig.savefig(out_path, dpi=110, facecolor=fig.get_facecolor())
    plt.close(fig)

    return {
        "pair": pair,
        "n_prem": n_prem, "wr_prem": wr_prem,
        "n_strict": n_strict, "wr_strict": wr_strict,
        "out_path": out_path,
    }


def main() -> int:
    pair = sys.argv[1] if len(sys.argv) > 1 else "EURUSD"
    os.makedirs("reports", exist_ok=True)
    out = f"reports/chart_proof_{pair}.png"
    stats = render(pair, out)
    print(f"[chart] wrote {stats['out_path']}", flush=True)
    print(f"[chart] {pair}: ★ ПРЕМИУМ {stats['n_prem']} @ {stats['wr_prem']:.1f}%   "
          f"СТРОГИЙ {stats['n_strict']} @ {stats['wr_strict']:.1f}%", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
