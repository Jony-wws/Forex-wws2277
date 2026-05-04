"""Trap detector — find recurring false-breakout / fake-news patterns.

A "trap" cell from Phase-2 is a significant move whose direction reversed
≥80% within 6 hours. Phase-4 aggregates these to find:

1. **Event-trap patterns** — (pair × session × event_type) cells where the
   trap-rate is high (≥50% of times the event coincided with a significant
   move, that move was a trap). These are events that typically generate a
   spike-then-reversal pattern on this pair × session.

2. **Recurring time-of-day trap patterns** — (pair × session × hour-of-day)
   cells where a significant move at this hour reverses more than half the
   time, irrespective of event presence (pure technical trap).

3. **Liquidity traps** — (pair × session) where the % of significant moves
   that are traps overall is high → pair / session combo to AVOID trading
   on news spikes.

Outputs:
- HISTORY/event_attribution_365d/trap_event_patterns.csv
- HISTORY/event_attribution_365d/trap_time_patterns.csv
- HISTORY/event_attribution_365d/trap_pair_session_summary.csv
"""
from __future__ import annotations

import csv
import json
import logging
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

log = logging.getLogger("events.traps")

ROOT = Path(__file__).resolve().parents[2]
STATE_DIR = ROOT / "teamagent" / "state"
EVENTS_FILE = STATE_DIR / "events_365d.json"
MOVES_FILE = STATE_DIR / "moves_365d.jsonl"
OUT_DIR = ROOT / "HISTORY" / "event_attribution_365d"

# Re-use the 2h window from attribution
WINDOW_BEFORE = timedelta(hours=2)
WINDOW_AFTER = timedelta(hours=2)


def event_affects_pair(event_ccy: str, pair: str) -> bool:
    return pair[:3] == event_ccy or pair[3:6] == event_ccy


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    events_arch = json.loads(EVENTS_FILE.read_text())
    events = events_arch["events"]
    for e in events:
        e["_ts"] = pd.Timestamp(e["ts"]).tz_convert("UTC")
    by_date: dict[str, list[dict]] = defaultdict(list)
    for e in events:
        by_date[e["_ts"].date().isoformat()].append(e)

    # Trap data structures
    event_trap_agg: dict[tuple[str, str, str], dict] = defaultdict(lambda: {"n_significant": 0, "n_trap": 0})
    time_trap_agg: dict[tuple[str, str, int], dict] = defaultdict(lambda: {"n_significant": 0, "n_trap": 0})
    pair_session_agg: dict[tuple[str, str], dict] = defaultdict(lambda: {"n_significant": 0, "n_trap": 0, "n_total": 0})

    # All cells (we want overall significant/trap counts)
    with open(MOVES_FILE) as f:
        for line in f:
            cell = json.loads(line)
            ps_key = (cell["pair"], cell["session"])
            pair_session_agg[ps_key]["n_total"] += 1
            if not cell.get("significant"):
                continue
            pair_session_agg[ps_key]["n_significant"] += 1
            if cell.get("trap"):
                pair_session_agg[ps_key]["n_trap"] += 1

            # Time-of-day trap (use session_start_hour_utc — already on the cell)
            tkey = (cell["pair"], cell["session"], cell["session_start_hour_utc"])
            time_trap_agg[tkey]["n_significant"] += 1
            if cell.get("trap"):
                time_trap_agg[tkey]["n_trap"] += 1

            # Match events for event-trap patterns
            sess_start = pd.Timestamp(cell["session_start_ts"]).tz_convert("UTC")
            sess_end = pd.Timestamp(cell["session_end_ts"]).tz_convert("UTC")
            win_start = sess_start - WINDOW_BEFORE
            win_end = sess_end + WINDOW_AFTER
            cand_dates = {
                (sess_start - timedelta(days=1)).date().isoformat(),
                sess_start.date().isoformat(),
                (sess_end + timedelta(days=1)).date().isoformat(),
            }
            for d in cand_dates:
                for e in by_date.get(d, []):
                    if not (win_start <= e["_ts"] <= win_end and event_affects_pair(e["currency"], cell["pair"])):
                        continue
                    ekey = (cell["pair"], cell["session"], e["type"])
                    event_trap_agg[ekey]["n_significant"] += 1
                    if cell.get("trap"):
                        event_trap_agg[ekey]["n_trap"] += 1

    # Build CSVs
    # 1. event-trap patterns
    rows = []
    for (pair, sess, etype), d in event_trap_agg.items():
        if d["n_significant"] < 3:
            continue
        rate = d["n_trap"] / d["n_significant"] * 100.0
        rows.append({
            "pair": pair,
            "session": sess,
            "event_type": etype,
            "n_event_significant_matches": d["n_significant"],
            "n_traps": d["n_trap"],
            "trap_rate_pct": rate,
        })
    df_evt = pd.DataFrame(rows).sort_values(["trap_rate_pct", "n_event_significant_matches"], ascending=[False, False])
    df_evt.to_csv(OUT_DIR / "trap_event_patterns.csv", index=False)
    log.info(f"trap_event_patterns: {len(df_evt)} rows (≥3 matches)")

    # 2. time-of-day trap patterns
    rows = []
    for (pair, sess, hour), d in time_trap_agg.items():
        if d["n_significant"] < 5:
            continue
        rate = d["n_trap"] / d["n_significant"] * 100.0
        rows.append({
            "pair": pair,
            "session": sess,
            "session_start_hour_utc": hour,
            "n_significant": d["n_significant"],
            "n_traps": d["n_trap"],
            "trap_rate_pct": rate,
        })
    df_time = pd.DataFrame(rows).sort_values(["trap_rate_pct", "n_significant"], ascending=[False, False])
    df_time.to_csv(OUT_DIR / "trap_time_patterns.csv", index=False)
    log.info(f"trap_time_patterns: {len(df_time)} rows (≥5 sig moves)")

    # 3. pair-session trap summary (overall liquidity-trap risk)
    rows = []
    for (pair, sess), d in pair_session_agg.items():
        sig_pct = d["n_significant"] / d["n_total"] * 100.0 if d["n_total"] > 0 else 0
        trap_pct_of_sig = d["n_trap"] / d["n_significant"] * 100.0 if d["n_significant"] > 0 else 0
        trap_pct_of_total = d["n_trap"] / d["n_total"] * 100.0 if d["n_total"] > 0 else 0
        rows.append({
            "pair": pair,
            "session": sess,
            "n_total_cells": d["n_total"],
            "n_significant": d["n_significant"],
            "n_traps": d["n_trap"],
            "significant_pct_of_total": sig_pct,
            "trap_pct_of_significant": trap_pct_of_sig,
            "trap_pct_of_total": trap_pct_of_total,
        })
    df_ps = pd.DataFrame(rows).sort_values(["trap_pct_of_significant", "n_significant"], ascending=[False, False])
    df_ps.to_csv(OUT_DIR / "trap_pair_session_summary.csv", index=False)
    log.info(f"trap_pair_session_summary: {len(df_ps)} rows")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    main()
