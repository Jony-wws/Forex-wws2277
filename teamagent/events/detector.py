"""Move detector — per (pair × day × session) realised behaviour.

For each (pair, day, session) we compute on Yahoo 1H bars:
- range_pips: high - low across the session window
- signed_move_pips: close - open across the session window (positive = base ↑)
- direction: 'up' / 'down' / 'flat'
- atr_baseline: rolling 20-day mean of session range
- significance: range_pips / atr_baseline → label 'significant' if > 1.5
- persistence_24h_pct: of the next 24h after session-end, what % of bars held the
  same direction as the session-close bias (closed on the session-close side)
- subsequent_reversal_pct: max retracement in the next 6h vs the session move
- trap_flag: True if reversal in 3-6h after a 'significant' move

Output: jsonl, one line per (pair, day, session) cell. ~28 × 365 × 4 ≈ 40k rows.
"""
from __future__ import annotations

import json
import logging
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

import pandas as pd

from .. import config
from ..data import yahoo

log = logging.getLogger("events.detector")

STATE_DIR = Path(__file__).resolve().parents[1] / "state"
OUT_FILE = STATE_DIR / "moves_365d.jsonl"

# Session windows (UTC hour ranges). Use config but extend so non-overlap.
# config.SESSIONS uses end-exclusive style? Let's use end-inclusive [start, end].
SESSIONS: dict[str, tuple[int, int]] = {
    "Asia":     (0, 6),
    "London":   (7, 12),
    "Overlap":  (13, 16),
    "NY":       (17, 21),
}

# significance threshold (range / 20d-baseline)
SIG_MULT = 1.5

# pip definition: 0.0001 except JPY pairs = 0.01
def pip_size(pair: str) -> float:
    return 0.01 if "JPY" in pair else 0.0001


def _to_pips(pair: str, x: float) -> float:
    return x / pip_size(pair)


def _session_slice(df_day: pd.DataFrame, hour_start: int, hour_end: int) -> pd.DataFrame:
    """Return bars where hour ∈ [hour_start, hour_end] (inclusive)."""
    if df_day.empty:
        return df_day
    h = df_day.index.hour
    mask = (h >= hour_start) & (h <= hour_end)
    return df_day[mask]


def _compute_session_stats(pair: str, sess_bars: pd.DataFrame) -> dict | None:
    """Compute range/direction/signed_move on a single session block."""
    if sess_bars.empty or len(sess_bars) < 2:
        return None
    high = float(sess_bars["High"].max())
    low = float(sess_bars["Low"].min())
    open_ = float(sess_bars["Open"].iloc[0])
    close = float(sess_bars["Close"].iloc[-1])
    rng = high - low
    signed = close - open_
    direction = "up" if signed > 0 else ("down" if signed < 0 else "flat")
    return {
        "open": open_,
        "high": high,
        "low": low,
        "close": close,
        "range_pips": _to_pips(pair, rng),
        "signed_move_pips": _to_pips(pair, signed),
        "direction": direction,
        "session_start_ts": sess_bars.index[0].isoformat(),
        "session_end_ts": sess_bars.index[-1].isoformat(),
    }


def _compute_persistence_and_reversal(
    pair: str,
    df_full: pd.DataFrame,
    session_close_ts: pd.Timestamp,
    signed_move: float,
    horizon_hours: int = 24,
    reversal_window_hours: int = 6,
) -> dict:
    """Return persistence_24h_pct and max_reversal_pct.

    persistence_24h_pct: of next horizon_hours bars after session close,
      % whose close stayed on the same side of session_close.
    max_reversal_pct: max retracement in next reversal_window_hours bars
      relative to abs(signed_move). 1.0 = full give-back; >1.0 = trend
      reversed.
    """
    if signed_move == 0:
        return {"persistence_24h_pct": None, "max_reversal_pct": None}
    close_at_session = float(df_full.loc[session_close_ts, "Close"])
    after = df_full[df_full.index > session_close_ts]
    if after.empty:
        return {"persistence_24h_pct": None, "max_reversal_pct": None}
    horizon_after = after[after.index <= session_close_ts + timedelta(hours=horizon_hours)]
    rev_window = after[after.index <= session_close_ts + timedelta(hours=reversal_window_hours)]
    sign = 1 if signed_move > 0 else -1
    if len(horizon_after) > 0:
        same_side = ((horizon_after["Close"] - close_at_session) * sign >= 0).sum()
        persistence = float(same_side) / len(horizon_after) * 100.0
    else:
        persistence = None
    if len(rev_window) > 0:
        # max retracement against signed_move direction
        if signed_move > 0:
            max_low = float(rev_window["Low"].min())
            retrace = close_at_session - max_low
        else:
            max_high = float(rev_window["High"].max())
            retrace = max_high - close_at_session
        max_reversal_pct = retrace / abs(signed_move) if abs(signed_move) > 0 else 0.0
    else:
        max_reversal_pct = None
    return {
        "persistence_24h_pct": persistence,
        "max_reversal_pct": max_reversal_pct,
    }


def detect_for_pair(pair: str, days: int = 365) -> list[dict]:
    """Run move-detector for one pair → list of per-cell dicts."""
    df = yahoo.fetch(pair, interval="1h", period="2y")
    if df.empty:
        log.warning(f"empty bars for {pair}")
        return []
    end_dt = df.index[-1].normalize()
    start_dt = end_dt - timedelta(days=days)
    df = df[df.index >= start_dt]

    cells: list[dict] = []
    # Group by date
    daily_groups = df.groupby(df.index.date)
    # Pre-compute per-session 20d rolling range baseline
    # → for each session, build a series of session ranges then rolling 20-period mean.
    session_rngs: dict[str, dict] = {sn: {} for sn in SESSIONS}  # session → date → range
    for date_, day_df in daily_groups:
        for sn, (hs, he) in SESSIONS.items():
            sb = _session_slice(day_df, hs, he)
            if sb.empty or len(sb) < 2:
                continue
            high = float(sb["High"].max())
            low = float(sb["Low"].min())
            session_rngs[sn][date_] = _to_pips(pair, high - low)

    # Build rolling baselines (20 sessions = ~4 weeks)
    baselines: dict[str, dict] = {sn: {} for sn in SESSIONS}
    for sn, m in session_rngs.items():
        if not m:
            continue
        s = pd.Series(m).sort_index()
        roll = s.rolling(20, min_periods=10).mean()
        roll_std = s.rolling(20, min_periods=10).std()
        for d in s.index:
            base = roll.get(d)
            sd = roll_std.get(d)
            baselines[sn][d] = (
                None if pd.isna(base) else float(base),
                None if pd.isna(sd) else float(sd),
            )

    for date_, day_df in daily_groups:
        for sn, (hs, he) in SESSIONS.items():
            sb = _session_slice(day_df, hs, he)
            stats = _compute_session_stats(pair, sb)
            if stats is None:
                continue
            base, sd = baselines.get(sn, {}).get(date_, (None, None))
            if base is None or base <= 0:
                continue
            sig_mult = stats["range_pips"] / base
            # Threshold: range > mean + 1.5σ if σ available, else > 1.5×mean
            if sd is not None and sd > 0:
                sig_threshold = base + SIG_MULT * sd
                significant = stats["range_pips"] > sig_threshold
            else:
                significant = stats["range_pips"] > SIG_MULT * base
            # Persistence + reversal (only if significant — saves compute)
            persist_data = {"persistence_24h_pct": None, "max_reversal_pct": None}
            trap = False
            if significant:
                ts_close = pd.Timestamp(stats["session_end_ts"]).tz_convert("UTC")
                persist_data = _compute_persistence_and_reversal(
                    pair, df, ts_close, stats["signed_move_pips"] * pip_size(pair)
                )
                # Trap: significant move BUT reversed ≥80% within 6h
                rev = persist_data.get("max_reversal_pct")
                trap = bool(rev is not None and rev >= 0.8)
            cell = {
                "pair": pair,
                "date": str(date_),
                "session": sn,
                "session_start_hour_utc": hs,
                "session_end_hour_utc": he,
                **stats,
                "atr_baseline_pips": base,
                "atr_baseline_std_pips": sd,
                "significance_mult": sig_mult,
                "significant": significant,
                "trap": trap,
                **persist_data,
            }
            cells.append(cell)
    return cells


def detect_all(days: int = 365) -> Iterator[dict]:
    """Run detector for all 28 pairs (yields cells)."""
    for i, pair in enumerate(config.PAIRS):
        try:
            cells = detect_for_pair(pair, days=days)
            log.info(f"[{i+1}/28] {pair}: {len(cells)} cells, {sum(c['significant'] for c in cells)} significant")
            for c in cells:
                yield c
        except Exception as e:
            log.exception(f"detector failed {pair}: {e}")


def run_and_save(days: int = 365) -> Path:
    OUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    n_total = 0
    n_sig = 0
    n_trap = 0
    with open(OUT_FILE, "w") as f:
        for cell in detect_all(days=days):
            f.write(json.dumps(cell) + "\n")
            n_total += 1
            if cell["significant"]:
                n_sig += 1
            if cell.get("trap"):
                n_trap += 1
    log.info(f"saved {n_total} cells ({n_sig} significant, {n_trap} traps) → {OUT_FILE}")
    return OUT_FILE


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    run_and_save()
