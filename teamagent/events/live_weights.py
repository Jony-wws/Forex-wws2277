"""Live event-weighting for forecast_scanner / paper_trader.

Loads the Phase-1..6 attribution artefacts once at import and exposes:

- persistent_event_in_window(pair, session, now_utc) → list of dicts
  describing persistent-driver events in ±6h of `now_utc` that affect `pair`.
- trap_risk(pair, session) → 0.0..1.0 (how often a significant move on this
  cell is a trap, derived from `trap_pair_session_summary.csv`).
- event_score_contribution(pair, session, now_utc) → (delta_score, reason)
  ready to feed into forecast_scanner.vote().

If the artefacts don't exist (e.g. on a fresh checkout before phase-6 was
run), all functions degrade to no-op safely.
"""
from __future__ import annotations

import csv
import json
import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

log = logging.getLogger("events.live_weights")

ROOT = Path(__file__).resolve().parents[2]
ART_DIR = ROOT / "HISTORY" / "event_attribution_365d"
EVENTS_FILE = ROOT / "teamagent" / "state" / "events_365d.json"
PERSISTENT_FILE = ART_DIR / "persistent_drivers.csv"
PER_EVENT_PS_FILE = ART_DIR / "per_event_pair_session.csv"
TRAP_PS_FILE = ART_DIR / "trap_pair_session_summary.csv"

WINDOW_HOURS = 6  # ±6h around current time

# Lazy-loaded caches
_loaded = False
_events: list[dict] = []
_events_by_date: dict[str, list[dict]] = defaultdict(list)
_persistent_types: dict[str, dict] = {}  # event_type → {persistence, concordance, total_matches}
_trap_risk: dict[tuple[str, str], float] = {}  # (pair, session) → trap_pct_of_significant
# (pair, session, event_type) → {dominant_direction, concordance, persistence, frequency}
_per_event_ps: dict[tuple[str, str, str], dict] = {}


def _safe_float(x: str | None) -> float | None:
    if x is None or x == "":
        return None
    try:
        return float(x)
    except ValueError:
        return None


def _load() -> None:
    """One-time load of all artefacts. Idempotent."""
    global _loaded
    if _loaded:
        return
    _loaded = True
    # Events
    try:
        if EVENTS_FILE.exists():
            arch = json.loads(EVENTS_FILE.read_text())
            for e in arch.get("events", []):
                e["_ts"] = datetime.fromisoformat(e["ts"])
                if e["_ts"].tzinfo is None:
                    e["_ts"] = e["_ts"].replace(tzinfo=timezone.utc)
                _events.append(e)
                _events_by_date[e["_ts"].date().isoformat()].append(e)
            log.info(f"loaded {len(_events)} events from archive")
        else:
            log.warning(f"events archive not found at {EVENTS_FILE}; live-weights disabled")
    except Exception as e:
        log.warning(f"failed to load events archive: {e}")

    # Persistent drivers
    try:
        if PERSISTENT_FILE.exists():
            with open(PERSISTENT_FILE) as f:
                for row in csv.DictReader(f):
                    _persistent_types[row["event_type"]] = {
                        "persistence_24h_avg_pct": _safe_float(row.get("persistence_24h_avg_pct")) or 0.0,
                        "direction_concordance_pct": _safe_float(row.get("direction_concordance_pct")) or 0.0,
                        "total_matches": int(row.get("total_matches", 0)),
                        "abs_mean_move_pips": _safe_float(row.get("abs_mean_move_pips")) or 0.0,
                    }
            log.info(f"loaded {len(_persistent_types)} persistent event-types")
        else:
            log.info(f"persistent_drivers.csv not found; live-weights disabled")
    except Exception as e:
        log.warning(f"failed to load persistent_drivers: {e}")

    # Trap risk
    try:
        if TRAP_PS_FILE.exists():
            with open(TRAP_PS_FILE) as f:
                for row in csv.DictReader(f):
                    pair = row["pair"]
                    sess = row["session"]
                    rate = _safe_float(row.get("trap_pct_of_significant")) or 0.0
                    _trap_risk[(pair, sess)] = rate
            log.info(f"loaded {len(_trap_risk)} pair-session trap rates")
    except Exception as e:
        log.warning(f"failed to load trap_pair_session_summary: {e}")

    # per-pair-session-event direction lookup
    try:
        if PER_EVENT_PS_FILE.exists():
            with open(PER_EVENT_PS_FILE) as f:
                for row in csv.DictReader(f):
                    key = (row["pair"], row["session"], row["event_type"])
                    _per_event_ps[key] = {
                        "dominant_direction_event_ccy": row.get("dominant_direction_event_ccy", "flat"),
                        "concordance": _safe_float(row.get("direction_concordance_pct")) or 0.0,
                        "persistence_24h_pct": _safe_float(row.get("persistence_24h_avg_pct")) or 0.0,
                        "frequency": int(row.get("frequency", 0)),
                        "trap_rate_pct": _safe_float(row.get("trap_rate_pct")) or 0.0,
                    }
            log.info(f"loaded {len(_per_event_ps)} per-event-pair-session entries")
    except Exception as e:
        log.warning(f"failed to load per_event_pair_session: {e}")


def event_affects_pair(event_ccy: str, pair: str) -> bool:
    return pair[:3] == event_ccy or pair[3:6] == event_ccy


def persistent_events_in_window(pair: str, now_utc: datetime, window_hours: int = WINDOW_HOURS) -> list[dict]:
    """Find persistent-driver events within ±window_hours that affect pair."""
    _load()
    if not _events or not _persistent_types:
        return []
    win_start = now_utc - timedelta(hours=window_hours)
    win_end = now_utc + timedelta(hours=window_hours)
    candidate_dates = {
        (now_utc - timedelta(days=1)).date().isoformat(),
        now_utc.date().isoformat(),
        (now_utc + timedelta(days=1)).date().isoformat(),
    }
    out: list[dict] = []
    for d in candidate_dates:
        for e in _events_by_date.get(d, []):
            if not (win_start <= e["_ts"] <= win_end and event_affects_pair(e["currency"], pair)):
                continue
            etype = e["type"]
            if etype not in _persistent_types:
                continue
            out.append(e)
    return out


def trap_risk(pair: str, session: str) -> float:
    """Return trap-rate (0..100) for pair × session. 0 if unknown."""
    _load()
    return _trap_risk.get((pair, session), 0.0)


def event_score_contribution(
    pair: str,
    session: str,
    now_utc: datetime,
    base_score_pts: int = 4,
    window_hours: int = WINDOW_HOURS,
) -> tuple[int, str | None]:
    """Compute event-weight contribution for forecast_scanner.

    Logic:
    - For each persistent event in window:
      - look up (pair, session, event_type) entry to find dominant_direction
        for the event-currency; convert to pair-direction (BUY if event_ccy is
        BASE and direction='up'; opposite for QUOTE).
      - contribution magnitude = base_score_pts × concordance × persistence
        (both 0..1), then sign by direction.
    - Sum contributions; trim to ±2*base_score_pts to avoid overweighting.

    Returns (delta_score_int, reason_string) or (0, None) if no events.
    """
    _load()
    events = persistent_events_in_window(pair, now_utc, window_hours)
    if not events:
        return 0, None

    total = 0.0
    parts: list[str] = []
    for e in events:
        etype = e["type"]
        meta = _per_event_ps.get((pair, session, etype))
        if not meta or meta["frequency"] < 2:
            continue
        conc = meta["concordance"] / 100.0
        persist = meta["persistence_24h_pct"] / 100.0
        # weight 0..base_score_pts
        weight = base_score_pts * conc * persist
        if weight < 0.5:
            continue
        # Direction: dominant for event-ccy. If event_ccy == base of pair,
        # 'up' means base ↑ → BUY pair. If event_ccy == quote of pair,
        # 'up' means quote ↑ → SELL pair (because quote up = pair down).
        dom = meta["dominant_direction_event_ccy"]
        if dom == "flat":
            continue
        is_base = pair[:3] == e["currency"]
        if dom == "up":
            sign = +1 if is_base else -1
        else:
            sign = -1 if is_base else +1
        contrib = round(weight * sign)
        if contrib == 0:
            continue
        total += contrib
        parts.append(f"{etype}({sign:+d}×{round(weight,1)})")
    # Trim to ±2*base_score_pts (don't let one news cluster overwhelm)
    cap = 2 * base_score_pts
    total = max(-cap, min(cap, total))
    if total == 0:
        return 0, None
    return int(round(total)), "event_attribution: " + ", ".join(parts)


def trap_score_penalty(pair: str, session: str, score: int, threshold_pct: float = 50.0) -> tuple[int, str | None]:
    """Soft trap-filter: if (pair, session) trap-rate ≥ threshold_pct, return
    a small penalty that REDUCES |score|. Does NOT reverse direction or zero
    out trades — paper_trader's free 70% gate decides openings; this only
    nudges probability down a touch on known whipsaw cells.

    Returns (delta_score_int, reason_string) or (0, None).
    """
    _load()
    rate = trap_risk(pair, session)
    if rate < threshold_pct or score == 0:
        return 0, None
    # Penalty proportional to how much trap-rate exceeds threshold.
    # rate=50 → 0pts (boundary); rate=90 → up to 4pts off.
    excess = (rate - threshold_pct) / 50.0  # 0..1 over [50,100]
    penalty = max(1, int(round(4 * excess)))
    penalty = min(penalty, abs(score))  # never flip the sign
    delta = -penalty if score > 0 else penalty
    return delta, f"trap_filter: cell trap_rate={rate:.0f}% (threshold {threshold_pct:.0f}%), reducing |score| by {penalty}"
