"""Adaptive auto-optimiser for the strong-trend thresholds.

The strict 5-hour cycle ships with a fixed set of strong-trend
thresholds (confidence ≥ 92, ratio ≥ 0.65, ADX H1 ≥ 30, ADX H4 ≥ 25,
persistence ≥ 90).  Those numbers were calibrated against historical
data, but markets drift.  The auto-optimiser closes the loop:

* It reads the per-session win-rate (Asia / London / NY) from
  ``state/cycle_latest.json`` (produced by the GitHub-Actions cycle).
* If the WR on any session drops below ``WR_TIGHTEN_THRESHOLD = 70 %``
  the thresholds are *tightened* (we want fewer but higher-quality
  signals during a degrading session).
* If the WR on any session rises above ``WR_LOOSEN_THRESHOLD = 80 %``
  the thresholds are *loosened slightly* (we have headroom to publish
  more signals without dropping quality).
* Every adjustment is bounded by an absolute clamp so the optimiser
  can never push the system into either extreme; it nudges, it does
  not reinvent.
* Every change is appended to ``state/auto_optimizer_log.json`` for
  auditability.

The optimiser is *not* invoked automatically by ``app.cycle.tick``; it
is invoked by ``scripts/cycle_5h.py`` once per cycle, after the
backtest sweep has produced fresh per-session WR numbers.  This keeps
the live confidence pipeline deterministic — only the discrete cycle
boundary ever changes a threshold.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

log = logging.getLogger("auto_optimizer")


STATE_DIR = Path("state")
LATEST_PATH = STATE_DIR / "cycle_latest.json"
THRESHOLDS_PATH = STATE_DIR / "auto_thresholds.json"
LOG_PATH = STATE_DIR / "auto_optimizer_log.json"


# ── Tuning knobs ─────────────────────────────────────────────────────
WR_TIGHTEN_THRESHOLD = 70.0
WR_LOOSEN_THRESHOLD = 80.0

# Minimum trades per session before we trust the WR enough to act on it.
MIN_SESSION_TRADES = 30

# Step sizes — small fractional nudges so a single bad cycle never
# moves the system more than a few percent off the baseline.
TIGHTEN_STEP = {
    "confidence": +1,    # confidence floor: +1 percentage point
    "ratio": +0.02,      # score-ratio: +0.02
    "adx_h1": +1.0,
    "adx_h4": +1.0,
    "persistence": +2.0,
}
LOOSEN_STEP = {
    "confidence": -1,
    "ratio": -0.02,
    "adx_h1": -1.0,
    "adx_h4": -1.0,
    "persistence": -2.0,
}

# Absolute clamp — the optimiser is *only* allowed to nudge within
# this band around the baseline.  Anything outside is rejected.
BASELINE = {
    "confidence": 92,
    "ratio": 0.65,
    "adx_h1": 30.0,
    "adx_h4": 25.0,
    "persistence": 90.0,
}
CLAMPS = {
    "confidence": (85, 96),
    "ratio": (0.55, 0.80),
    "adx_h1": (25.0, 38.0),
    "adx_h4": (20.0, 32.0),
    "persistence": (75.0, 100.0),
}


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def load_current_thresholds() -> dict:
    """Return the *current* effective thresholds.

    Reads ``state/auto_thresholds.json`` and falls back to the baseline
    when the file is missing or malformed.  Always returns a dict with
    every key in ``BASELINE`` populated so the caller never has to
    null-check downstream.
    """
    data = _read_json(THRESHOLDS_PATH, default={})
    merged = dict(BASELINE)
    if isinstance(data, dict):
        for k in BASELINE:
            if k in data and isinstance(data[k], (int, float)):
                merged[k] = data[k]
    return merged


def _clamp(name: str, value: float) -> float:
    lo, hi = CLAMPS[name]
    if value < lo:
        return float(lo)
    if value > hi:
        return float(hi)
    return float(value)


def _per_session_wr() -> dict[str, dict]:
    """Read per-session win-rate stats from ``state/cycle_latest.json``.

    Returns a dict like::

        {
            "Asia":   {"wr": 72.5, "trades": 41},
            "London": {"wr": 64.2, "trades": 58},
            "NY":     {"wr": 81.0, "trades": 47},
        }

    Missing or under-sampled sessions are omitted.  Any failure to
    parse the file returns an empty dict so callers no-op gracefully.
    """
    data = _read_json(LATEST_PATH, default=None)
    if not isinstance(data, dict):
        return {}
    sessions = data.get("by_session") or data.get("sessions") or {}
    if not isinstance(sessions, dict):
        return {}
    out: dict[str, dict] = {}
    for name, row in sessions.items():
        if not isinstance(row, dict):
            continue
        try:
            wr = float(row.get("wr") or row.get("win_rate") or 0.0)
            trades = int(row.get("trades") or row.get("n") or 0)
        except (TypeError, ValueError):
            continue
        if trades < MIN_SESSION_TRADES:
            continue
        out[name] = {"wr": wr, "trades": trades}
    return out


def _direction_for_sessions(sessions: dict[str, dict]) -> Optional[str]:
    """Decide whether to tighten, loosen, or no-op.

    Tighten wins if *any* session is below 70 % WR — degradation on any
    side of the world is a reason to tighten globally.  Otherwise we
    loosen only if *every* session is above 80 % (all-cylinders firing
    means we have room to relax).  Anything else is a no-op.
    """
    if not sessions:
        return None
    if any(row["wr"] < WR_TIGHTEN_THRESHOLD for row in sessions.values()):
        return "tighten"
    if all(row["wr"] > WR_LOOSEN_THRESHOLD for row in sessions.values()):
        return "loosen"
    return None


def _apply_step(current: dict, step: dict) -> dict:
    out = {}
    for k, base in BASELINE.items():
        v = current.get(k, base) + step.get(k, 0)
        out[k] = _clamp(k, v)
    return out


def _append_log(entry: dict) -> None:
    log_entries = _read_json(LOG_PATH, default=[])
    if not isinstance(log_entries, list):
        log_entries = []
    log_entries.append(entry)
    # Bound the log to the last 500 entries to keep the file small.
    log_entries = log_entries[-500:]
    _write_json(LOG_PATH, log_entries)


def optimize_thresholds_based_on_performance() -> dict:
    """Inspect the latest cycle's WR-by-session and adjust thresholds.

    Returns a small report dict describing what happened so callers
    (the cycle_5h script, Telegram, etc.) can surface it::

        {
            "action": "tighten" | "loosen" | "no_op",
            "reason": str,
            "before": {...},
            "after":  {...},
            "sessions": {...},
            "timestamp": "...Z",
        }
    """
    before = load_current_thresholds()
    sessions = _per_session_wr()
    direction = _direction_for_sessions(sessions)
    entry: dict = {
        "timestamp": _utcnow_iso(),
        "action": "no_op",
        "reason": "",
        "sessions": sessions,
        "before": before,
        "after": before,
    }
    if direction == "tighten":
        after = _apply_step(before, TIGHTEN_STEP)
        worst = min(sessions.values(), key=lambda r: r["wr"])
        entry.update({
            "action": "tighten",
            "reason": (
                f"WR {worst['wr']:.1f} %% < {WR_TIGHTEN_THRESHOLD:.0f} %% "
                f"(n={worst['trades']}) — ужесточаем"
            ),
            "after": after,
        })
        _write_json(THRESHOLDS_PATH, after)
    elif direction == "loosen":
        after = _apply_step(before, LOOSEN_STEP)
        best = max(sessions.values(), key=lambda r: r["wr"])
        entry.update({
            "action": "loosen",
            "reason": (
                f"WR ≥ {WR_LOOSEN_THRESHOLD:.0f} %% по всем сессиям "
                f"(лучшая {best['wr']:.1f} %%) — слегка ослабляем"
            ),
            "after": after,
        })
        _write_json(THRESHOLDS_PATH, after)
    else:
        entry["reason"] = (
            "WR в норме либо данных не хватает для решения — без изменений"
        )

    _append_log(entry)
    return entry


def reset_to_baseline() -> dict:
    """Reset the thresholds file to the baseline.

    Returns the entry written to the audit log.  Useful as a manual
    "panic" lever — call from a one-off script if the optimiser drifts
    somewhere unexpected.
    """
    before = load_current_thresholds()
    after = dict(BASELINE)
    _write_json(THRESHOLDS_PATH, after)
    entry = {
        "timestamp": _utcnow_iso(),
        "action": "reset",
        "reason": "Manual reset to baseline thresholds",
        "before": before,
        "after": after,
        "sessions": {},
    }
    _append_log(entry)
    return entry
