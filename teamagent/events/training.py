"""Phase 8 — train forecast_scanner on the 365-day archive.

Reads everything Phase 1-6 produced and emits one consolidated knowledge
file: `state/learned_rules.json`. The live-side `live_weights.py` then
loads this file and uses it for stronger, higher-confidence boosts than
plain event-attribution.

Three knowledge layers extracted:

1. **High-conviction event rules** — (pair × session × event_type) cells
   where direction_concordance ≥ 75% on ≥ 4 historical instances. When
   such an event fires in a live ±6h window, score boost is large
   (proportional to concordance × persistence × min(frequency, 8)).

2. **Per-pair persistent directional bias** — for each (pair × session),
   the average signed move across ALL significant moves in 365 days.
   This is a background tendency that nudges the score even when no
   specific event is in window. Bounded ±2 score points.

3. **Multi-event cluster amplifier** — when ≥ 2 persistent-driver events
   co-fire in the same ±6h window AND they all agree on direction (e.g.
   USD CPI + US NFP both bearish for USD), apply a multiplicative
   amplifier to the combined boost (cap +5 extra).

This module is run offline once after the archive is built. Output is
read at forecast_scanner import time.
"""
from __future__ import annotations

import csv
import json
import logging
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("events.training")

ROOT = Path(__file__).resolve().parents[2]
ARTEFACTS_DIR = ROOT / "HISTORY" / "event_attribution_365d"
STATE_FILE = Path(__file__).resolve().parents[1] / "state" / "learned_rules.json"
MOVES_FILE = Path(__file__).resolve().parents[1] / "state" / "moves_365d.jsonl"


def _f(x: str) -> float:
    try:
        return float(x)
    except Exception:
        return 0.0


def _build_high_conviction_rules() -> list[dict]:
    """Layer 1: high-conviction (pair × session × event_type) cells."""
    src = ARTEFACTS_DIR / "per_event_pair_session.csv"
    rows = list(csv.DictReader(open(src)))
    rules: list[dict] = []
    for r in rows:
        n = int(r["frequency"])
        if n < 4:
            continue
        conc = _f(r["direction_concordance_pct"])
        # Either highly bullish OR highly bearish (concordance far from 50%)
        if not (conc >= 75 or conc <= 25):
            continue
        persist = _f(r["persistence_24h_avg_pct"])
        # We accept lower persistence here because intraday binary trades
        # only need 1-5h follow-through, not 24h.
        if persist < 30:
            continue
        rules.append({
            "pair": r["pair"],
            "session": r["session"],
            "event_type": r["event_type"],
            "frequency": n,
            "concordance_pct": conc,
            "dominant_direction_event_ccy": r["dominant_direction_event_ccy"],
            "persistence_24h_pct": persist,
            "abs_move_pips": _f(r["abs_mean_move_pips"]),
            "trap_rate_pct": _f(r["trap_rate_pct"]),
        })
    log.info(f"high-conviction rules: {len(rules)}")
    return rules


def _build_pair_session_bias() -> dict[str, dict]:
    """Layer 2: per (pair, session) average signed move from move detector.

    Reads moves_365d.jsonl directly. We compute the *signed* mean over all
    days for that session — a positive value means the pair tends to drift
    up during that session over the year.
    """
    if not MOVES_FILE.exists():
        log.warning(f"moves file not found: {MOVES_FILE}")
        return {}
    bucket: dict[tuple[str, str], list[float]] = defaultdict(list)
    with MOVES_FILE.open() as f:
        for line in f:
            try:
                cell = json.loads(line)
            except Exception:
                continue
            pair = cell.get("pair")
            sess = cell.get("session")
            sm = cell.get("signed_move_pips")
            if pair and sess and sm is not None:
                bucket[(pair, sess)].append(float(sm))
    out: dict[str, dict] = {}
    for (pair, sess), vals in bucket.items():
        if len(vals) < 30:
            continue
        # Median is robust to outliers; mean shows drift
        n = len(vals)
        mean = sum(vals) / n
        # Direction concordance among non-zero
        ups = sum(1 for v in vals if v > 0.5)
        downs = sum(1 for v in vals if v < -0.5)
        total = ups + downs or 1
        concordance = max(ups, downs) / total * 100
        dom_dir = "up" if ups >= downs else "down"
        out.setdefault(pair, {})[sess] = {
            "n": n,
            "mean_signed_pips": round(mean, 3),
            "concordance_pct": round(concordance, 2),
            "dominant_direction": dom_dir,
        }
    log.info(f"per-pair-session bias: {sum(len(v) for v in out.values())} cells")
    return out


def _build_persistent_drivers_set() -> set[str]:
    """Layer 3 prep: set of event-types that are 'persistent drivers'."""
    src = ARTEFACTS_DIR / "persistent_drivers.csv"
    if not src.exists():
        return set()
    rows = list(csv.DictReader(open(src)))
    return {r["event_type"] for r in rows}


def build_all() -> dict:
    rules = _build_high_conviction_rules()
    pair_bias = _build_pair_session_bias()
    persistent = sorted(_build_persistent_drivers_set())
    out = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "n_high_conviction_rules": len(rules),
        "n_pair_session_bias_cells": sum(len(v) for v in pair_bias.values()),
        "n_persistent_drivers": len(persistent),
        "high_conviction_rules": rules,
        "pair_session_bias": pair_bias,
        "persistent_driver_types": persistent,
    }
    return out


def save(out: dict) -> Path:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(out, indent=2, ensure_ascii=False))
    return STATE_FILE


def load() -> dict | None:
    if not STATE_FILE.exists():
        return None
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return None


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    out = build_all()
    p = save(out)
    print(f"saved learned_rules to {p}: {out['n_high_conviction_rules']} rules, "
          f"{out['n_pair_session_bias_cells']} bias cells, "
          f"{out['n_persistent_drivers']} persistent drivers")
