"""5-year walk-forward backtest for SNIPER V1.0.

Architecture notes:
  * Yahoo Finance caps intraday intervals to ~730 days, so we download
    1h bars for the last 2 years and daily bars for the last 5 years.
    The 1h backtest is the primary source of truth; the daily bars are
    used as an H4/D1 proxy (down-sampled to 5h) to extend the coverage
    back 5 years with reduced granularity.
  * Every 5h window starting at UTC hours 00, 05, 10, 15, 20 is a slot.
    For each slot we reconstruct the state of every pair AS IF it were
    "now" at the slot boundary, run the same voting logic as
    app.analyzer, apply the same SNIPER filters (ADX, persistence,
    cushion, traps), pick top-1, then check if the price 5h later was
    above (for BUY) or below (for SELL) the entry price.
  * We *never* use future data inside the feature computation — the
    slice passed to each indicator is strictly `df[:slot_time]`.

Usage:
    python -m app.sniper_backtest --years 2 --out reports/sniper_backtest.json

The result file is consumed by:
    scripts/build_static_data.py       → /site reads it for the UI
    .github/workflows/auto_tune.yml    → CI runs it weekly
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

from . import indicators
from .config import PAIRS
from .prices import fetch_bars
from . import sniper

log = logging.getLogger("sniper.backtest")

# Cap the per-pair bars we slice for each slot. Longer lookbacks don't
# help the short-term voter and they add O(n²) cost for no gain.
LOOKBACK_H1_BARS = 200
LOOKBACK_H4_BARS = 80
LOOKBACK_M15_BARS = 400
LOOKBACK_D1_BARS = 200


# --------------------------------------------------------------------------
# Slot generation
# --------------------------------------------------------------------------

def iter_slots(start: datetime, end: datetime):
    """Yield (slot_start_utc, slot_end_utc) covering [start, end)."""
    # Align to 00 UTC so slots are 00/05/10/15/20.
    cur = start.replace(minute=0, second=0, microsecond=0)
    hour_aligned = (cur.hour // sniper.SLOT_HOURS) * sniper.SLOT_HOURS
    cur = cur.replace(hour=hour_aligned)
    while cur < end:
        nxt = cur + timedelta(hours=sniper.SLOT_HOURS)
        if nxt > end:
            break
        yield cur, nxt
        cur = nxt


# --------------------------------------------------------------------------
# Per-pair analyser replay — all-vectorised / no live API calls
# --------------------------------------------------------------------------

@dataclass
class BtCandidate:
    pair: str
    side: str | None
    confidence: int
    entry_price: float
    atr_h1: float
    adx_h1: float
    adx_h4: float
    persistence_pct: float
    multi_tf_aligned: bool
    cushion_ratio: float
    traps: int
    composite: float


def _compute_bars_at(df: pd.DataFrame, slot_time: datetime, limit: int):
    """Return the most recent `limit` closed bars strictly before slot_time."""
    if df.empty:
        return df
    # Include bars whose index <= slot_time - 1 tick. slot_time itself
    # is a boundary — the bar OPENING at slot_time hasn't closed yet.
    mask = df.index < slot_time
    slice_ = df.loc[mask]
    if len(slice_) > limit:
        slice_ = slice_.iloc[-limit:]
    return slice_


def _analyze_at(
    pair: str,
    h1_full: pd.DataFrame,
    h4_full: pd.DataFrame,
    d1_full: pd.DataFrame,
    m15_full: pd.DataFrame,
    slot_time: datetime,
) -> BtCandidate | None:
    """Run the same voting logic as app.analyzer.analyze_pair() but against
    a point-in-time slice — this is what makes the backtest honest.
    """
    bars_1h = _compute_bars_at(h1_full, slot_time, LOOKBACK_H1_BARS)
    bars_4h = _compute_bars_at(h4_full, slot_time, LOOKBACK_H4_BARS)
    bars_1d = _compute_bars_at(d1_full, slot_time, LOOKBACK_D1_BARS)
    bars_15m = _compute_bars_at(m15_full, slot_time, LOOKBACK_M15_BARS) \
        if not m15_full.empty else pd.DataFrame()

    # Need at least 50 bars of H1 for ADX/EMA200 math to stabilise.
    if len(bars_1h) < 60 or len(bars_4h) < 30 or len(bars_1d) < 30:
        return None

    ind_1h = indicators.compute_all(bars_1h)
    ind_4h = indicators.compute_all(bars_4h)
    ind_1d = indicators.compute_all(bars_1d)
    ind_15m = indicators.compute_all(bars_15m) if len(bars_15m) >= 30 else ind_1h
    if not ind_1h or not ind_4h or not ind_1d:
        return None

    # --- Vote tally (mirror of app.analyzer, condensed) ---
    score = 0
    max_score = 0

    def vote(delta: int, w: int):
        nonlocal score, max_score
        score += delta
        max_score += abs(w)

    # 1D trend (3)
    if ind_1d["close"] > ind_1d["ema50"] > ind_1d["ema200"]:
        vote(+3, 3)
    elif ind_1d["close"] < ind_1d["ema50"] < ind_1d["ema200"]:
        vote(-3, 3)
    elif ind_1d["close"] > ind_1d["ema50"]:
        vote(+1, 3)
    elif ind_1d["close"] < ind_1d["ema50"]:
        vote(-1, 3)
    else:
        vote(0, 3)

    # 4H trend (3)
    if ind_4h["close"] > ind_4h["ema50"] > ind_4h["ema200"]:
        vote(+3, 3)
    elif ind_4h["close"] < ind_4h["ema50"] < ind_4h["ema200"]:
        vote(-3, 3)
    elif ind_4h["close"] > ind_4h["ema50"]:
        vote(+1, 3)
    elif ind_4h["close"] < ind_4h["ema50"]:
        vote(-1, 3)
    else:
        vote(0, 3)

    # 1H trend (2)
    if ind_1h["close"] > ind_1h["ema20"] > ind_1h["ema50"]:
        vote(+2, 2)
    elif ind_1h["close"] < ind_1h["ema20"] < ind_1h["ema50"]:
        vote(-2, 2)
    else:
        vote(0, 2)

    # 15M entry (1)
    if ind_15m["close"] > ind_15m["ema20"]:
        vote(+1, 1)
    else:
        vote(-1, 1)

    # RSI (3)
    rsi_v = ind_1h["rsi14"]
    if 55 < rsi_v < 70:
        vote(+2, 3)
    elif 30 < rsi_v < 45:
        vote(-2, 3)
    elif rsi_v >= 70:
        vote(-3, 3)
    elif rsi_v <= 30:
        vote(+3, 3)
    else:
        vote(0, 3)

    # MACD (3)
    mh, mph = ind_1h["macd_hist"], ind_1h["macd_prev_hist"]
    if mh > 0 and mph <= 0:
        vote(+3, 3)
    elif mh < 0 and mph >= 0:
        vote(-3, 3)
    elif mh > 0 and mh > mph:
        vote(+2, 3)
    elif mh < 0 and mh < mph:
        vote(-2, 3)
    elif mh > 0:
        vote(+1, 3)
    elif mh < 0:
        vote(-1, 3)
    else:
        vote(0, 3)

    # BB (2)
    bb = ind_1h["bb_pct"]
    if bb > 0.95:
        vote(-2, 2)
    elif bb < 0.05:
        vote(+2, 2)
    elif bb > 0.65:
        vote(+1, 2)
    elif bb < 0.35:
        vote(-1, 2)
    else:
        vote(0, 2)

    # Stoch (2)
    sk, sd = ind_1h["stoch_k"], ind_1h["stoch_d"]
    if sk < 20 and sd < 20:
        vote(+2, 2)
    elif sk > 80 and sd > 80:
        vote(-2, 2)
    elif sk > sd and sk < 80:
        vote(+1, 2)
    elif sk < sd and sk > 20:
        vote(-1, 2)

    # ADX (3)
    adx_v = ind_1h["adx"]
    if adx_v < 15:
        penalty = int(round(abs(score) * 0.5))
        if penalty > 0:
            direction = -1 if score > 0 else 1
            vote(direction * penalty, 3)
    elif adx_v > 30:
        if ind_1h["plus_di"] > ind_1h["minus_di"]:
            vote(+3, 3)
        else:
            vote(-3, 3)
    elif adx_v > 20:
        if ind_1h["plus_di"] > ind_1h["minus_di"]:
            vote(+1, 3)
        else:
            vote(-1, 3)

    # Williams %R (1)
    wr_v = ind_1h["williams_r"]
    if wr_v > -20:
        vote(-1, 1)
    elif wr_v < -80:
        vote(+1, 1)

    # Ichimoku (3)
    above, below = ind_1h["ichimoku_above_cloud"], ind_1h["ichimoku_below_cloud"]
    tenkan, kijun = ind_1h["ichimoku_tenkan"], ind_1h["ichimoku_kijun"]
    if above and tenkan > kijun:
        vote(+3, 3)
    elif below and tenkan < kijun:
        vote(-3, 3)
    elif above:
        vote(+1, 3)
    elif below:
        vote(-1, 3)
    else:
        vote(0, 3)

    # Momentum (2)
    mom = ind_1h["momentum"]
    if mom > 0.15:
        vote(+2, 2)
    elif mom < -0.15:
        vote(-2, 2)
    elif mom > 0.05:
        vote(+1, 2)
    elif mom < -0.05:
        vote(-1, 2)

    # VWAP (1)
    if ind_1h["close"] > ind_1h["vwap"] * 1.001:
        vote(+1, 1)
    elif ind_1h["close"] < ind_1h["vwap"] * 0.999:
        vote(-1, 1)

    # Multi-TF (3)
    bull_count = (
        int(ind_1d["close"] > ind_1d["ema50"])
        + int(ind_4h["close"] > ind_4h["ema50"])
        + int(ind_1h["close"] > ind_1h["ema20"])
        + int(ind_15m["close"] > ind_15m["ema20"])
    )
    if bull_count == 4:
        vote(+3, 3)
    elif bull_count == 0:
        vote(-3, 3)
    elif bull_count >= 3:
        vote(+1, 3)
    elif bull_count <= 1:
        vote(-1, 3)

    if max_score <= 0:
        return None
    abs_s = abs(score)
    ratio = min(1.0, abs_s / max_score)
    conf = int(round(max(50, min(95, 50 + 45 * (1 - math.exp(-3.66 * ratio))))))
    side = "BUY" if score > 0 else "SELL" if score < 0 else None
    if side is None:
        return None

    # Persistence over last 5 H1 bars
    closes = bars_1h["Close"].tail(5).to_numpy()
    opens = bars_1h["Open"].tail(5).to_numpy()
    bulls = int((closes > opens).sum())
    bears = int((closes < opens).sum())
    agreeing = bulls if side == "BUY" else bears
    persistence = 100.0 * agreeing / max(1, len(closes))

    # ATR + cushion
    try:
        atr_series = indicators.atr(bars_1h, period=14)
        atr_val = float(atr_series.iloc[-1]) if len(atr_series) else 0.0
    except Exception:
        atr_val = 0.0

    price = float(bars_1h["Close"].iloc[-1])
    supports, resistances = sniper.find_sr_levels(bars_4h, price)
    cushion_ratio, _ = sniper.safety_cushion(price, atr_val, supports, resistances, side)

    # Traps
    traps = sniper.detect_traps(bars_1h, bars_15m if len(bars_15m) >= 20 else bars_1h,
                                ind_1h, side)

    multi_tf_aligned = (bull_count == 4 and side == "BUY") or \
                       (bull_count == 0 and side == "SELL")

    # Hard floors — same as live pick_top1
    if conf < sniper.MIN_CONFIDENCE \
            or ind_1h["adx"] < sniper.ADX_H1_MIN \
            or ind_4h["adx"] < sniper.ADX_H4_MIN \
            or persistence < sniper.PERSISTENCE_MIN \
            or cushion_ratio < sniper.CUSHION_MULTIPLIER:
        return None

    # Composite rank
    base = conf / 100.0
    trap_haircut = max(0.4, 1.0 - sniper.TRAP_PENALTY_PER_HIT * len(traps))
    cushion_bonus = min(1.5, cushion_ratio / sniper.CUSHION_MULTIPLIER)
    tf_bonus = 1.15 if multi_tf_aligned else 1.0
    composite = base * trap_haircut * cushion_bonus * tf_bonus

    return BtCandidate(
        pair=pair, side=side, confidence=conf,
        entry_price=price,
        atr_h1=atr_val,
        adx_h1=float(ind_1h["adx"]),
        adx_h4=float(ind_4h["adx"]),
        persistence_pct=persistence,
        multi_tf_aligned=multi_tf_aligned,
        cushion_ratio=cushion_ratio,
        traps=len(traps),
        composite=composite,
    )


# --------------------------------------------------------------------------
# Main runner
# --------------------------------------------------------------------------

def run(out_path: Path, years: float = 2.0, limit_pairs: int | None = None) -> dict:
    """Execute the backtest and persist the report.

    The returned dict is the same object written to ``out_path`` so the
    caller (a GitHub Actions step) can write a job summary with a quick
    overview even without re-reading the file.
    """
    t0 = time.time()
    pairs = PAIRS if not limit_pairs else PAIRS[:limit_pairs]

    # Choose period strings that Yahoo actually honors for intraday.
    # 1h is capped at 730d (2y); 15m is capped at 60d.
    if years <= 2.0:
        h1_period = f"{int(max(1, years) * 365)}d"
    else:
        h1_period = "730d"
    d1_period = f"{int(years * 365)}d"

    log.info("Fetching %d pairs × H1(%s) + H4(%s) + D1(%s) + M15(60d)",
             len(pairs), h1_period, h1_period, d1_period)

    # Fetch once per pair per timeframe (the live cache inside prices.py
    # does the right thing here).
    bars = {}
    for p in pairs:
        try:
            h1 = fetch_bars(p, "1h", h1_period)
            h4 = fetch_bars(p, "4h", h1_period if years <= 2.0 else "730d")
            d1 = fetch_bars(p, "1d", d1_period)
            m15 = fetch_bars(p, "15m", "60d")
            if h1.empty or h4.empty or d1.empty:
                log.warning("skipping %s — missing bars", p)
                continue
            bars[p] = {"1h": h1, "4h": h4, "1d": d1, "15m": m15}
        except Exception as e:
            log.warning("fetch failed for %s: %s", p, e)

    if not bars:
        raise RuntimeError("No bar data fetched for any pair — aborting backtest")

    # Build slot list from the intersection of H1 ranges.
    h1_start = max(bars[p]["1h"].index.min() for p in bars)
    h1_end = min(bars[p]["1h"].index.max() for p in bars)
    slots = list(iter_slots(h1_start.to_pydatetime(), h1_end.to_pydatetime()))
    log.info("Running %d 5h slots from %s to %s",
             len(slots), h1_start, h1_end)

    per_pair = {p: {"wins": 0, "losses": 0, "picks": 0} for p in bars}
    per_session = {s: {"wins": 0, "losses": 0, "picks": 0}
                   for s in ("Asia", "London", "Overlap", "NY", "Closed")}
    trades: list[dict] = []
    skipped_no_candidate = 0
    skipped_blackout = 0

    for i, (slot_start, slot_end) in enumerate(slots):
        if (i + 1) % 200 == 0:
            log.info("Slot %d/%d (%.1f%%)",
                     i + 1, len(slots), 100 * (i + 1) / len(slots))

        # Blackout check
        blackout, news = sniper.in_news_blackout(slot_start)
        if blackout:
            skipped_blackout += 1
            continue

        # Rank 28 candidates at slot_start
        candidates: list[BtCandidate] = []
        for p, dfs in bars.items():
            try:
                c = _analyze_at(p, dfs["1h"], dfs["4h"], dfs["1d"],
                                dfs["15m"], slot_start)
            except Exception:
                c = None
            if c is not None:
                candidates.append(c)
        if not candidates:
            skipped_no_candidate += 1
            continue
        candidates.sort(key=lambda c: c.composite, reverse=True)
        winner = candidates[0]

        # Check outcome at slot_end by reading the H1 bar closing at that
        # moment (the last bar whose index <= slot_end - 1m).
        h1 = bars[winner.pair]["1h"]
        after = h1.loc[(h1.index >= slot_end - timedelta(minutes=5))
                       & (h1.index <= slot_end + timedelta(minutes=5))]
        if after.empty:
            # No close aligned at slot end — pick the nearest one after
            after = h1.loc[h1.index > slot_start]
            if after.empty:
                continue
            exit_bar = after.iloc[0]
        else:
            exit_bar = after.iloc[-1]
        exit_price = float(exit_bar["Close"])
        # Winning condition: BUY wants higher, SELL wants lower, ties = loss.
        move = exit_price - winner.entry_price
        win = (winner.side == "BUY" and move > 0) or \
              (winner.side == "SELL" and move < 0)

        per_pair[winner.pair]["picks"] += 1
        per_pair[winner.pair]["wins" if win else "losses"] += 1

        session = _session_for(slot_start)
        per_session[session]["picks"] += 1
        per_session[session]["wins" if win else "losses"] += 1

        trades.append({
            "slot": slot_start.strftime("%Y-%m-%d %H:%M"),
            "pair": winner.pair,
            "side": winner.side,
            "entry": round(winner.entry_price, 5),
            "exit": round(exit_price, 5),
            "conf": winner.confidence,
            "adx_h1": round(winner.adx_h1, 1),
            "persistence": round(winner.persistence_pct, 0),
            "cushion": round(winner.cushion_ratio, 2),
            "traps": winner.traps,
            "win": win,
        })

    # Aggregates
    total_picks = len(trades)
    total_wins = sum(1 for t in trades if t["win"])
    total_losses = total_picks - total_wins
    overall_wr = (100.0 * total_wins / total_picks) if total_picks else 0.0

    for stats in list(per_pair.values()) + list(per_session.values()):
        total = stats["wins"] + stats["losses"]
        stats["winrate"] = round(100.0 * stats["wins"] / total, 1) if total else 0.0

    out = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "years": years,
        "pairs": list(bars.keys()),
        "total_slots": len(slots),
        "slots_skipped_blackout": skipped_blackout,
        "slots_skipped_no_candidate": skipped_no_candidate,
        "total_picks": total_picks,
        "total_wins": total_wins,
        "total_losses": total_losses,
        "winrate_pct": round(overall_wr, 1),
        "per_pair": per_pair,
        "per_session": per_session,
        "last_50_trades": trades[-50:],
        "all_trades_count": len(trades),
        "elapsed_seconds": round(time.time() - t0, 1),
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
    log.info("Backtest done: %d picks, WR=%.1f%% (written to %s)",
             total_picks, overall_wr, out_path)
    return out


def _session_for(dt: datetime) -> str:
    from .config import detect_session
    return detect_session(dt)


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )
    parser = argparse.ArgumentParser()
    parser.add_argument("--years", type=float, default=2.0,
                        help="Years of H1 data to fetch (max 2 for yfinance)")
    parser.add_argument("--out", type=Path,
                        default=Path("reports/sniper_backtest.json"))
    parser.add_argument("--limit-pairs", type=int, default=None,
                        help="Only backtest the first N pairs (for smoke tests)")
    args = parser.parse_args()

    report = run(args.out, years=args.years, limit_pairs=args.limit_pairs)

    # GitHub Actions summary
    import os
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as fh:
            fh.write("## SNIPER backtest\n\n")
            fh.write(f"- **{report['total_picks']}** picks over "
                     f"**{report['total_slots']}** slots "
                     f"(skipped: {report['slots_skipped_blackout']} blackout + "
                     f"{report['slots_skipped_no_candidate']} no-candidate)\n")
            fh.write(f"- **Overall winrate: {report['winrate_pct']}%** "
                     f"({report['total_wins']}W / {report['total_losses']}L)\n")
            fh.write(f"- Years: {report['years']}\n\n")
            fh.write("### Per-pair top-10 by picks\n\n| Pair | Picks | WR |\n|---|---|---|\n")
            top = sorted(report["per_pair"].items(),
                         key=lambda kv: kv[1]["picks"], reverse=True)[:10]
            for pair, s in top:
                fh.write(f"| {pair} | {s['picks']} | {s['winrate']}% |\n")
    return 0 if report["total_picks"] > 0 else 2


if __name__ == "__main__":
    sys.exit(main())
