"""Event ↔ move attribution.

For each significant move (Phase-2 cell with significant=True), find every
event from the Phase-1 archive whose ts falls in [session_start - 2h,
session_end + 2h]. Then aggregate per (event-type × pair × session):

- frequency: # of (significant move, matched event) pairs over 365d
- direction_concordance_rate: % where event "predicted" the realised move
  - we use a heuristic: positive surprise on USD-impact event → USD ↑ → for
    pair=BASE/USD it predicts BASE ↓ (sell); for USD/QUOTE it predicts QUOTE ↓
    of inverse (buy).
  - For non-surprise events (CB rate decisions / press conferences) we count
    direction independent of surprise (since hawkish/dovish is unknown).
- mean_signed_move_pips: avg pip move (with direction sign relative to base)
- persistence_24h_pct: avg over matches
- trap_rate_pct: % of matches where the move was flagged as a trap

Output: HISTORY/event_attribution_365d/per_event_response.csv

A persistent driver is one with: count >= 10, persistence >= 60, |mean_move| >= 8 pips.
"""
from __future__ import annotations

import csv
import json
import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

import pandas as pd

log = logging.getLogger("events.attribution")

ROOT = Path(__file__).resolve().parents[2]
STATE_DIR = ROOT / "teamagent" / "state"
EVENTS_FILE = STATE_DIR / "events_365d.json"
MOVES_FILE = STATE_DIR / "moves_365d.jsonl"
OUT_DIR = ROOT / "HISTORY" / "event_attribution_365d"
OUT_DIR.mkdir(parents=True, exist_ok=True)
PER_EVENT_CSV = OUT_DIR / "per_event_response.csv"
PER_EVENT_PAIR_CSV = OUT_DIR / "per_event_pair_session.csv"

# Window around session: events ±2h count as "candidate"
WINDOW_BEFORE = timedelta(hours=2)
WINDOW_AFTER = timedelta(hours=2)

# Pairs whose currency overlaps with event currency
def event_affects_pair(event_ccy: str, pair: str) -> bool:
    """USD event affects all pairs containing USD; EUR event affects pairs containing EUR; etc."""
    return pair[:3] == event_ccy or pair[3:6] == event_ccy


def load_events() -> list[dict]:
    arch = json.loads(EVENTS_FILE.read_text())
    return arch["events"]


def load_moves() -> Iterable[dict]:
    with open(MOVES_FILE) as f:
        for line in f:
            yield json.loads(line)


def attribute() -> pd.DataFrame:
    events = load_events()
    # Pre-parse event ts to timestamp
    for e in events:
        e["_ts"] = pd.Timestamp(e["ts"]).tz_convert("UTC")
    events_sorted = sorted(events, key=lambda e: e["_ts"])

    # Group events by date for fast lookup
    events_by_date: dict[str, list[dict]] = defaultdict(list)
    for e in events_sorted:
        events_by_date[e["_ts"].date().isoformat()].append(e)

    # Iterate over significant moves; match to events; aggregate.
    # key: (pair, session, event_type) → list of (signed_move_pips, persistence, trap, signed_relative)
    agg: dict[tuple[str, str, str], list[dict]] = defaultdict(list)
    n_sig = 0
    for cell in load_moves():
        if not cell.get("significant"):
            continue
        n_sig += 1
        sess_start = pd.Timestamp(cell["session_start_ts"]).tz_convert("UTC")
        sess_end = pd.Timestamp(cell["session_end_ts"]).tz_convert("UTC")
        win_start = sess_start - WINDOW_BEFORE
        win_end = sess_end + WINDOW_AFTER
        # Search events in [win_start - 1d, win_end + 1d] window of dates
        candidate_dates = {
            (sess_start - timedelta(days=1)).date().isoformat(),
            sess_start.date().isoformat(),
            (sess_end + timedelta(days=1)).date().isoformat(),
        }
        matched: list[dict] = []
        for d in candidate_dates:
            for e in events_by_date.get(d, []):
                if win_start <= e["_ts"] <= win_end and event_affects_pair(e["currency"], cell["pair"]):
                    matched.append(e)
        for e in matched:
            key = (cell["pair"], cell["session"], e["type"])
            # Direction relative to event currency:
            # if event currency is BASE of pair → moving up means base strengthens
            # if event currency is QUOTE → moving up means quote weakens (so signed-relative = -signed_move_pips)
            sgn = cell["signed_move_pips"]
            if cell["pair"][:3] == e["currency"]:
                signed_for_event_ccy = sgn
            else:
                signed_for_event_ccy = -sgn
            agg[key].append({
                "signed_move_pips": sgn,
                "signed_for_event_ccy": signed_for_event_ccy,
                "persistence_24h_pct": cell.get("persistence_24h_pct"),
                "max_reversal_pct": cell.get("max_reversal_pct"),
                "trap": cell.get("trap", False),
                "delta_vs_prev": e.get("delta_vs_prev"),
            })

    log.info(f"matched {sum(len(v) for v in agg.values())} (event,move) pairs across {len(agg)} (pair, session, event-type) cells over {n_sig} significant moves")

    # Build CSV
    rows: list[dict] = []
    for (pair, sess, etype), matches in agg.items():
        n = len(matches)
        if n == 0:
            continue
        signed = [m["signed_move_pips"] for m in matches]
        signed_evt = [m["signed_for_event_ccy"] for m in matches]
        pers = [m["persistence_24h_pct"] for m in matches if m["persistence_24h_pct"] is not None]
        traps = sum(1 for m in matches if m["trap"])
        # Direction concordance (signed for event currency): % where it has same sign as the most common
        if signed_evt:
            ups = sum(1 for s in signed_evt if s > 0)
            downs = sum(1 for s in signed_evt if s < 0)
            concord = max(ups, downs) / n * 100.0
            dominant_dir_evt = "up" if ups >= downs else "down"
        else:
            concord = 0
            dominant_dir_evt = "flat"
        # If we have delta_vs_prev (FRED surprise), compute hit-rate: did sign of signed_for_event_ccy match sign of delta?
        hits = total = 0
        for m in matches:
            d = m.get("delta_vs_prev")
            if d is None:
                continue
            total += 1
            # higher actual vs prior → currency stronger expected
            expected = 1 if d > 0 else (-1 if d < 0 else 0)
            actual = 1 if m["signed_for_event_ccy"] > 0 else (-1 if m["signed_for_event_ccy"] < 0 else 0)
            if expected == actual and expected != 0:
                hits += 1
        hit_rate = (hits / total * 100.0) if total > 0 else None

        rows.append({
            "pair": pair,
            "session": sess,
            "event_type": etype,
            "frequency": n,
            "mean_signed_move_pips": sum(signed) / n,
            "mean_signed_for_event_ccy_pips": sum(signed_evt) / n,
            "abs_mean_move_pips": sum(abs(s) for s in signed) / n,
            "direction_concordance_pct": concord,
            "dominant_direction_event_ccy": dominant_dir_evt,
            "persistence_24h_avg_pct": (sum(pers) / len(pers)) if pers else None,
            "trap_rate_pct": traps / n * 100.0,
            "fundamental_hit_rate_pct": hit_rate,
            "fundamental_match_count": total,
        })
    df = pd.DataFrame(rows)
    df = df.sort_values(["pair", "session", "frequency"], ascending=[True, True, False])
    df.to_csv(PER_EVENT_PAIR_CSV, index=False)
    log.info(f"per_event_pair_session: {len(df)} rows → {PER_EVENT_PAIR_CSV}")

    # Aggregate per event-type only (across all pairs/sessions)
    type_rows = []
    for etype, grp in df.groupby("event_type"):
        type_rows.append({
            "event_type": etype,
            "total_matches": int(grp["frequency"].sum()),
            "n_pair_session_cells": len(grp),
            "abs_mean_move_pips": float(grp["abs_mean_move_pips"].mean()),
            "persistence_24h_avg_pct": float(grp["persistence_24h_avg_pct"].dropna().mean()) if grp["persistence_24h_avg_pct"].dropna().size > 0 else None,
            "trap_rate_pct": float(grp["trap_rate_pct"].mean()),
            "direction_concordance_pct": float(grp["direction_concordance_pct"].mean()),
            "fundamental_hit_rate_pct": float(grp["fundamental_hit_rate_pct"].dropna().mean()) if grp["fundamental_hit_rate_pct"].dropna().size > 0 else None,
        })
    type_df = pd.DataFrame(type_rows).sort_values("total_matches", ascending=False)
    type_df.to_csv(PER_EVENT_CSV, index=False)
    log.info(f"per_event_response: {len(type_df)} event-types → {PER_EVENT_CSV}")
    return df


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    attribute()
