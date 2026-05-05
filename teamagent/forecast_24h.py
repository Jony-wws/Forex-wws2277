"""Phase 12 — 24-hour-ahead forecast engine (added 2026-05-05).

Builds a per-(pair × future hour) directional + magnitude forecast for the
next 24 hours, anchored on the 365-day learned knowledge already captured in
`state/learned_rules.json`:

- `pair_hour_bias` — per-(pair × UTC hour) drift direction + concordance +
  mean signed pip move (49 cells, n>=60, conc>=62%).
- `pair_session_bias` — per-(pair × session) drift (112 cells).
- `high_conviction_rules` — per-(pair × session × event_type) rules with
  freq+concordance.
- `events_365d.json` — 718 events with avg pip moves.

We do NOT invent numbers. If a hour has no learned cell, we report
NEUTRAL with low confidence — the user can SEE which hours have real
365-day evidence and which don't.

Output: `state/forecast_24h.json`
{
  "as_of": "2026-05-05T01:55Z",
  "horizon_hours": 24,
  "expiry_hours_per_signal": 5,        # user requested 5h expiry per Phase 12
  "pairs": {
    "EURUSD": {
      "best_peak": {"hour_utc": 14, "side": "BUY", "expected_pips": 12.3,
                    "confidence_pct": 71, "drivers": ["pair_hour_bias", "session_bias"]},
      "timeline": [
        {"hour_utc": 5,  "side": "BUY",  "score": 4, "expected_pips": 1.5, "confidence_pct": 62, "drivers": [...]},
        ...
      ]
    },
    ...
  }
}

The endpoint `/api/forecast-24h` exposes this and `/api/forecasts` carries
the `forecast_24h_peak` summary on each PROGNOZY-28 card.

The forecast does NOT replace the 1-bar PROGNOZY-28 signal — it adds a
forward look so the user can plan trades for the next 24h, with the
recommended expiry of 5h (matches `MAX_EXPIRY_HOURS = 5`).
"""
from __future__ import annotations

import json
import logging
import signal
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import config

log = logging.getLogger("forecast_24h")

LEARNED_RULES_PATH = config.STATE_DIR / "learned_rules.json"
FORECAST_24H_PATH = config.STATE_DIR / "forecast_24h.json"

DEFAULT_INTERVAL_SEC = 60 * 30  # rebuild every 30 minutes


def _hour_to_session(hour_utc: int) -> str:
    """Map hour UTC to one of the 4 canonical sessions (matches config.SESSIONS)."""
    for name, (lo, hi) in config.SESSIONS.items():
        if lo <= hour_utc <= hi:
            return name
    return "Off"


def _load_learned() -> dict:
    if not LEARNED_RULES_PATH.exists():
        log.warning("learned_rules.json missing; 24h forecast will be empty")
        return {}
    try:
        return json.loads(LEARNED_RULES_PATH.read_text())
    except Exception as e:
        log.error(f"failed to parse learned_rules.json: {e}")
        return {}


def _hour_bias_signal(pair: str, hour_utc: int, learned: dict) -> tuple[float, str | None, dict]:
    """Returns (score_contrib, side, raw_cell). Score range ±3."""
    hb = (learned.get("pair_hour_bias") or {}).get(pair, {}).get(str(hour_utc))
    if not hb:
        return 0.0, None, {}
    direction = hb.get("dominant_direction")
    conc = float(hb.get("concordance_pct") or 0)
    n = int(hb.get("n") or 0)
    if direction not in ("up", "down") or n < 60 or conc < 62:
        return 0.0, None, hb
    side = "BUY" if direction == "up" else "SELL"
    # Stronger conviction → bigger contribution. ±1 at 62%, ±3 at 80%+.
    score = (conc - 60) / 8.0  # 62→0.25, 70→1.25, 80→2.5, 88→3.5
    score = max(0.0, min(3.0, score))
    if side == "SELL":
        score = -score
    return score, side, hb


def _session_bias_signal(pair: str, session: str, learned: dict) -> tuple[float, str | None, dict]:
    """Returns (score_contrib, side, raw_cell). Score range ±2."""
    sb = (learned.get("pair_session_bias") or {}).get(pair, {}).get(session)
    if not sb:
        return 0.0, None, {}
    direction = sb.get("dominant_direction")
    conc = float(sb.get("concordance_pct") or 0)
    n = int(sb.get("n") or 0)
    if direction not in ("up", "down") or n < 80 or conc < 65:
        return 0.0, None, sb
    side = "BUY" if direction == "up" else "SELL"
    score = (conc - 60) / 10.0  # 65→0.5, 75→1.5, 85→2.5
    score = max(0.0, min(2.0, score))
    if side == "SELL":
        score = -score
    return score, side, sb


def _build_pair_24h(pair: str, now_utc: datetime, learned: dict) -> dict:
    """Build a 24-hour timeline for one pair starting at now_utc + 1h."""
    timeline = []
    for h_offset in range(1, 25):
        future = now_utc + timedelta(hours=h_offset)
        hr = future.hour
        sess = _hour_to_session(hr)
        hb_score, hb_side, hb_raw = _hour_bias_signal(pair, hr, learned)
        sb_score, sb_side, sb_raw = _session_bias_signal(pair, sess, learned)
        total = hb_score + sb_score
        side = "BUY" if total > 0 else ("SELL" if total < 0 else "NEUTRAL")
        # Expected pips: weighted-average mean_signed_pips from the cells that voted.
        pip_votes = []
        if hb_raw and isinstance(hb_raw.get("mean_signed_pips"), (int, float)):
            pip_votes.append((abs(hb_score) + 0.5, float(hb_raw["mean_signed_pips"])))
        if sb_raw and isinstance(sb_raw.get("mean_signed_pips"), (int, float)):
            pip_votes.append((abs(sb_score) + 0.5, float(sb_raw["mean_signed_pips"])))
        if pip_votes:
            wsum = sum(w for w, _ in pip_votes)
            pips = sum(w * p for w, p in pip_votes) / wsum
        else:
            pips = 0.0
        # Confidence: blend of hour-bias and session-bias concordance, clipped 50..85.
        conc_votes = []
        if hb_raw.get("concordance_pct"):
            conc_votes.append(float(hb_raw["concordance_pct"]))
        if sb_raw.get("concordance_pct"):
            conc_votes.append(float(sb_raw["concordance_pct"]))
        confidence = max(50, min(85, sum(conc_votes) / len(conc_votes))) if conc_votes else 50
        drivers = []
        if hb_score != 0:
            drivers.append(f"pair_hour_bias[{pair},h={hr}] {hb_raw.get('dominant_direction')} conc={hb_raw.get('concordance_pct')}%")
        if sb_score != 0:
            drivers.append(f"pair_session_bias[{pair},{sess}] {sb_raw.get('dominant_direction')} conc={sb_raw.get('concordance_pct')}%")
        timeline.append({
            "hour_utc": hr,
            "h_offset": h_offset,
            "session": sess,
            "side": side,
            "score": round(total, 2),
            "expected_pips": round(pips, 2),
            "confidence_pct": round(confidence, 1),
            "drivers": drivers,
        })

    # Pick the strongest peak (max abs(score)) — that's the "best hour" for this pair.
    peak = max(timeline, key=lambda x: abs(x["score"]))
    if peak["score"] == 0:
        peak = None  # no learned-knowledge support — be honest
    return {
        "best_peak": peak,
        "timeline": timeline,
    }


def build_snapshot() -> dict:
    """Build the full 28-pair × 24-hour forecast snapshot."""
    now_utc = datetime.now(timezone.utc).replace(microsecond=0)
    learned = _load_learned()
    pairs_out = {}
    for pair in config.PAIRS:
        try:
            pairs_out[pair] = _build_pair_24h(pair, now_utc, learned)
        except Exception as e:
            log.exception(f"forecast_24h failed for {pair}: {e}")
            pairs_out[pair] = {"best_peak": None, "timeline": []}
    snap = {
        "as_of": now_utc.isoformat(),
        "horizon_hours": 24,
        "expiry_hours_per_signal": getattr(config, "FORECAST_24H_EXPIRY_HOURS", 5),
        "pairs": pairs_out,
    }
    FORECAST_24H_PATH.write_text(json.dumps(snap, indent=2))
    n_with_peak = sum(1 for p in pairs_out.values() if p.get("best_peak"))
    log.info(f"forecast_24h built: {n_with_peak}/{len(pairs_out)} pairs have 365d-backed peak")
    return snap


def run_loop(interval_sec: int | None = None) -> None:
    interval_sec = interval_sec or DEFAULT_INTERVAL_SEC
    log.info(f"forecast_24h start (interval={interval_sec}s, pairs={len(config.PAIRS)})")
    stop = {"flag": False}

    def _sig(_a, _b):
        stop["flag"] = True
        log.info("forecast_24h: SIGTERM/SIGINT — stopping")

    signal.signal(signal.SIGTERM, _sig)
    signal.signal(signal.SIGINT, _sig)
    while not stop["flag"]:
        try:
            build_snapshot()
        except Exception as e:
            log.exception(f"build_snapshot failed: {e}")
        for _ in range(interval_sec):
            if stop["flag"]:
                break
            time.sleep(1)
    log.info("forecast_24h exit")


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    run_loop()
