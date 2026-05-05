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
    """Layer 1: high-conviction (pair × session × event_type) cells.

    Phase 9 (2026-05-04): thresholds relaxed from (freq≥4, conc≥75%) to
    (freq≥3, conc≥70%) so we capture more learned cells. Per-rule weight
    in `live_weights.learned_rule_score()` already scales with concordance
    and frequency, so weak rules add only a small contribution while strong
    ones still dominate — widening the net does not dilute conviction.
    """
    src = ARTEFACTS_DIR / "per_event_pair_session.csv"
    rows = list(csv.DictReader(open(src)))
    rules: list[dict] = []
    for r in rows:
        n = int(r["frequency"])
        if n < 3:
            continue
        conc = _f(r["direction_concordance_pct"])
        # Either highly bullish OR highly bearish (concordance far from 50%)
        if not (conc >= 70 or conc <= 30):
            continue
        persist = _f(r["persistence_24h_avg_pct"])
        # We accept lower persistence here because intraday binary trades
        # only need 1-5h follow-through, not 24h.
        if persist < 25:
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
            # Phase 9: include raw counts so live_weights can tier the
            # confidence (binomial p-value) without re-reading the JSONL.
            "ups": ups,
            "downs": downs,
        }
    log.info(f"per-pair-session bias: {sum(len(v) for v in out.values())} cells")
    return out


def _build_pair_hour_bias() -> dict[str, dict]:
    """Phase 9 layer 2b: per (pair, hour-of-day) directional drift from hourly bars.

    Source: 365-day Yahoo 1H OHLCV (real, no simulator). For each pair we
    look at all 8760 1H bars and compute, per UTC-hour-of-day, the share of
    bars that closed above their open vs below. Cells with concordance ≥
    62% on n ≥ 60 days qualify; live_weights.hour_bias_score adds a small
    ±1 nudge in the dominant direction.

    Output structure: {pair: {hour_str: {n, ups, downs, concordance_pct,
    dominant_direction, mean_pips}}}.
    """
    try:
        from teamagent.data import yahoo  # type: ignore
    except Exception as e:
        log.warning(f"hour_bias: yahoo import failed ({e}); skipping")
        return {}
    try:
        from teamagent import config  # type: ignore
        pairs = list(config.PAIRS)
    except Exception:
        pairs = []
    if not pairs:
        return {}
    out: dict[str, dict] = {}
    for pair in pairs:
        try:
            df = yahoo.fetch(pair, interval="1h", period="365d")
        except Exception as e:
            log.warning(f"hour_bias {pair}: fetch failed ({e})")
            continue
        if df is None or len(df) < 200:
            continue
        # Group by UTC hour of day. df.index assumed UTC tz-aware.
        try:
            df = df.copy()
            df["_hr"] = df.index.hour  # type: ignore[attr-defined]
            # yahoo.fetch returns Title-Case columns (Open/High/Low/Close).
            close_col = "Close" if "Close" in df.columns else "close"
            open_col = "Open" if "Open" in df.columns else "open"
            df["_signed"] = df[close_col] - df[open_col]
        except Exception as e:
            log.warning(f"hour_bias {pair}: dataframe shape ({e})")
            continue
        per_hour: dict[str, dict] = {}
        for hr in range(24):
            sub = df[df["_hr"] == hr]
            n = len(sub)
            if n < 60:
                continue
            signed_vals = sub["_signed"].tolist()
            ups = sum(1 for v in signed_vals if v > 0)
            downs = sum(1 for v in signed_vals if v < 0)
            tot = ups + downs or 1
            conc = max(ups, downs) / tot * 100
            if conc < 62:
                continue
            mean_signed = sum(signed_vals) / max(1, n)
            # Convert to pips: pips per unit depends on JPY pairs vs others.
            pip_factor = 100.0 if pair.endswith("JPY") else 10000.0
            mean_pips = mean_signed * pip_factor
            per_hour[str(hr)] = {
                "n": n,
                "ups": ups,
                "downs": downs,
                "concordance_pct": round(conc, 2),
                "dominant_direction": "up" if ups >= downs else "down",
                "mean_signed_pips": round(mean_pips, 3),
            }
        if per_hour:
            out[pair] = per_hour
    log.info(
        f"per-pair-hour bias: {sum(len(v) for v in out.values())} cells across {len(out)} pairs"
    )
    return out


def _build_persistent_drivers_set() -> set[str]:
    """Layer 3 prep: set of event-types that are 'persistent drivers'."""
    src = ARTEFACTS_DIR / "persistent_drivers.csv"
    if not src.exists():
        return set()
    rows = list(csv.DictReader(open(src)))
    return {r["event_type"] for r in rows}


def build_all(include_hour_bias: bool = True) -> dict:
    rules = _build_high_conviction_rules()
    pair_bias = _build_pair_session_bias()
    persistent = sorted(_build_persistent_drivers_set())
    hour_bias = _build_pair_hour_bias() if include_hour_bias else {}
    out = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "n_high_conviction_rules": len(rules),
        "n_pair_session_bias_cells": sum(len(v) for v in pair_bias.values()),
        "n_pair_hour_bias_cells": sum(len(v) for v in hour_bias.values()),
        "n_persistent_drivers": len(persistent),
        "high_conviction_rules": rules,
        "pair_session_bias": pair_bias,
        "pair_hour_bias": hour_bias,
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
    import sys
    include_hours = "--no-hours" not in sys.argv
    out = build_all(include_hour_bias=include_hours)
    p = save(out)
    print(
        f"saved learned_rules to {p}: {out['n_high_conviction_rules']} rules, "
        f"{out['n_pair_session_bias_cells']} session-bias cells, "
        f"{out['n_pair_hour_bias_cells']} hour-bias cells, "
        f"{out['n_persistent_drivers']} persistent drivers"
    )
