"""Phase 13 — probability calibration vs realized WR (added 2026-05-05).

The honest path the user asked for:

> «обучи систему что бы у меня был выше мат ожидания на дистанции и преимущества»

Phase 11 made EV math visible. Phase 12 added 24h-ahead forecasts. But the
displayed `probability_pct` was still a theoretical sigmoid of the raw
`score`. Phase 13 closes the loop: we calibrate `probability_pct` against
**realized** WR data so what the dashboard says is what the broker pays.

# How calibration works

The calibrator builds a step function `displayed_probability_pct → realized_WR_pct`
from two real data sources:

1. **`state/closed_trades.json`** — every paper-trader trade we've actually
   closed. Each trade carries `probability_pct_at_open` and `result`.
2. **`strategy_config_locked.json` cells** — per-(pair × session) 365-day
   backtest WR. We pool these cells into the same probability buckets,
   weighted by trade count.

Pipeline:
- Buckets every 5 percentage points: `[50,55), [55,60), …, [90,92]`.
- For each bucket: count `(n, wins)`. Wilson 90% lower bound is the
  conservative calibrated WR — that's what we report.
- A bucket is "active" only when `n >= MIN_BUCKET_N` (default 8). Otherwise
  we return the raw `probability_pct` unchanged.

Output: `state/probability_calibration.json` — the bucket table + meta.

The calibrated probability **only ever LOWERS** the displayed value when
realized WR is below theoretical (lower bound is conservative). It never
inflates probability above what the data supports — that's the same
forbidden inflation rule from Phase 11.

# Why Wilson lower bound, not raw WR

With small n the empirical wins/total is noisy. Wilson 90% lower bound at
n=10, 7 wins gives ~52%, not 70% — that's the right answer for «what is
the WORST realistic WR for this bucket?». Over time as n grows, lower bound
converges to the true rate.

# Wiring

`forecast_scanner` (BLOCK Q) calls `calibrate(probability_pct, score, pair, session)`
and appends:
- `calibrated_probability_pct` — the calibrated value (or raw when no data)
- `calibration_n` — sample size behind the bucket
- `calibration_wilson_lower_pct` — same as calibrated when active
- `calibration_active` — bool

`forecast_scanner` also re-derives `ev_per_trade` from the **calibrated**
probability so the EV badge is honest at the broker payout.

# What this does NOT do

- Does NOT replace the free 70% gate. paper_trader still opens trades on
  `probability_pct ≥ 70` (rule #7 unchanged). Calibration is informational.
- Does NOT inflate probability. Wilson lower bound is conservative.
- Does NOT modify scoring or strategy_search. Pure post-process layer.
"""
from __future__ import annotations

import json
import logging
import math
from pathlib import Path

from . import config

log = logging.getLogger("probability_calibrator")

CLOSED_TRADES_PATH = config.STATE_DIR / "closed_trades.json"
STRATEGY_LOCKED_PATH = config.STATE_DIR / "strategy_config_locked.json"
CALIBRATION_OUT_PATH = config.STATE_DIR / "probability_calibration.json"

# Bucket boundaries: 50,55,60,65,70,75,80,85,90,92 (top kept tight to avoid
# 90-92 collapsing with the cap). Indexed by lower bound.
BUCKET_EDGES = [50, 55, 60, 65, 70, 75, 80, 85, 90, 92]
MIN_BUCKET_N = 8                # bucket needs >= 8 (trades+cell-trades) to be active
WILSON_Z = 1.645                # 90% one-sided lower bound (z for one-tail 95% / two-tail 90%)


def _wilson_lower_pct(wins: int, n: int, z: float = WILSON_Z) -> float:
    """Wilson score interval — lower bound on the true success rate."""
    if n <= 0:
        return 0.0
    p = wins / n
    z2 = z * z
    denom = 1 + z2 / n
    centre = (p + z2 / (2 * n)) / denom
    radius = (z * math.sqrt((p * (1 - p) + z2 / (4 * n)) / n)) / denom
    return max(0.0, min(1.0, centre - radius)) * 100.0


def _bucket_for(pct: float) -> int | None:
    """Return the lower edge of the bucket containing pct, or None if out of range."""
    for i, lo in enumerate(BUCKET_EDGES):
        if i + 1 < len(BUCKET_EDGES) and lo <= pct < BUCKET_EDGES[i + 1]:
            return lo
        if i + 1 == len(BUCKET_EDGES) and lo <= pct <= BUCKET_EDGES[i]:
            return lo
    return None


def build_calibration() -> dict:
    """Build the bucket table from closed_trades + strategy_locked cells.

    Returns the persisted dict (also written to disk).
    """
    buckets: dict[int, dict] = {}
    for lo in BUCKET_EDGES[:-1]:
        buckets[lo] = {"n": 0, "wins": 0, "sources": []}

    # Source 1: closed_trades — empirical realized outcomes.
    n_trades = 0
    try:
        if CLOSED_TRADES_PATH.exists():
            ct = json.loads(CLOSED_TRADES_PATH.read_text())
            trades = ct.get("trades") if isinstance(ct, dict) else ct
            for t in (trades or []):
                p = t.get("probability_pct_at_open") or t.get("probability_pct")
                r = t.get("result")
                if p is None or r not in ("WIN", "LOSS"):
                    continue
                lo = _bucket_for(float(p))
                if lo is None:
                    continue
                buckets[lo]["n"] += 1
                if r == "WIN":
                    buckets[lo]["wins"] += 1
                n_trades += 1
            if "closed_trades" not in [s for b in buckets.values() for s in b["sources"]]:
                for b in buckets.values():
                    if b["n"]:
                        b["sources"].append("closed_trades")
    except Exception as e:
        log.warning(f"closed_trades parse failed: {e}")

    # Source 2: strategy_config_locked cells — pooled per-(pair × session) backtest WR.
    # Each cell gives us `(win_rate_pct, trades)`. We treat the cell as a SINGLE
    # contribution: place it in the bucket corresponding to ITS WR, weighted by its
    # trade count (capped at 30 to avoid one mega-cell dominating).
    n_cells = 0
    try:
        if STRATEGY_LOCKED_PATH.exists():
            data = json.loads(STRATEGY_LOCKED_PATH.read_text())
            for pair, info in (data.get("pairs") or {}).items():
                by_sess = info.get("by_session") or {}
                for sess_name, sess_info in by_sess.items():
                    wr = sess_info.get("win_rate_pct")
                    n = sess_info.get("trades") or 0
                    if wr is None or n < 8:
                        continue
                    lo = _bucket_for(float(wr))
                    if lo is None:
                        continue
                    cap = min(int(n), 30)
                    buckets[lo]["n"] += cap
                    buckets[lo]["wins"] += round(cap * float(wr) / 100.0)
                    n_cells += 1
            for b in buckets.values():
                if b["n"]:
                    b["sources"] = list(set(b["sources"] + ["strategy_locked_cells"]))
    except Exception as e:
        log.warning(f"strategy_locked parse failed: {e}")

    # Compute Wilson lower bounds and active flag.
    table = {}
    for lo, info in buckets.items():
        n = info["n"]
        wins = info["wins"]
        wr_raw = (100.0 * wins / n) if n else None
        wilson_lo = _wilson_lower_pct(wins, n) if n else None
        active = (n >= MIN_BUCKET_N)
        table[str(lo)] = {
            "lo_pct": lo,
            "hi_pct": BUCKET_EDGES[BUCKET_EDGES.index(lo) + 1],
            "n": n,
            "wins": wins,
            "wr_raw_pct": round(wr_raw, 2) if wr_raw is not None else None,
            "wilson_lower_pct": round(wilson_lo, 2) if wilson_lo is not None else None,
            "active": active,
            "sources": info["sources"],
        }

    snap = {
        "as_of": __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat(),
        "min_bucket_n": MIN_BUCKET_N,
        "wilson_z": WILSON_Z,
        "n_closed_trades_used": n_trades,
        "n_locked_cells_used": n_cells,
        "buckets": table,
    }
    try:
        CALIBRATION_OUT_PATH.write_text(json.dumps(snap, indent=2))
    except Exception as e:
        log.warning(f"failed to write {CALIBRATION_OUT_PATH}: {e}")
    n_active = sum(1 for b in table.values() if b["active"])
    log.info(f"calibration built: {n_active}/{len(table)} buckets active "
             f"(closed_trades={n_trades}, locked_cells={n_cells})")
    return snap


# In-memory cache reload-on-demand. forecast_scanner calls calibrate() per-pair.
_cache: dict | None = None


def _load_cache() -> dict:
    global _cache
    if _cache is not None:
        return _cache
    try:
        if CALIBRATION_OUT_PATH.exists():
            _cache = json.loads(CALIBRATION_OUT_PATH.read_text())
            return _cache
    except Exception as e:
        log.warning(f"failed to load calibration cache: {e}")
    # Build on first use.
    _cache = build_calibration()
    return _cache


def reload() -> None:
    global _cache
    _cache = None


def calibrate(probability_pct: float) -> dict:
    """Return calibrated probability info for a given displayed probability.

    {
      "calibrated_probability_pct": float,   # Wilson lower bound when active, raw otherwise
      "calibration_n": int,
      "calibration_wilson_lower_pct": float | None,
      "calibration_active": bool,
      "bucket_lo_pct": int,
      "bucket_hi_pct": int,
    }
    """
    snap = _load_cache()
    table = snap.get("buckets") or {}
    lo = _bucket_for(float(probability_pct))
    if lo is None:
        return {
            "calibrated_probability_pct": float(probability_pct),
            "calibration_n": 0,
            "calibration_wilson_lower_pct": None,
            "calibration_active": False,
            "bucket_lo_pct": None,
            "bucket_hi_pct": None,
        }
    bkt = table.get(str(lo)) or {}
    active = bool(bkt.get("active"))
    wilson = bkt.get("wilson_lower_pct")
    n = int(bkt.get("n") or 0)
    if active and wilson is not None:
        # Use Wilson lower bound as calibrated probability — never above theoretical.
        cal = min(float(probability_pct), float(wilson))
    else:
        cal = float(probability_pct)
    return {
        "calibrated_probability_pct": round(cal, 1),
        "calibration_n": n,
        "calibration_wilson_lower_pct": wilson,
        "calibration_active": active,
        "bucket_lo_pct": int(bkt.get("lo_pct") or lo),
        "bucket_hi_pct": int(bkt.get("hi_pct") or 0),
    }


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    snap = build_calibration()
    print(json.dumps(snap, indent=2))
