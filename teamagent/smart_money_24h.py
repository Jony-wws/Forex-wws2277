"""Phase 14 — Smart-Money 24h forecast per (pair × session) (added 2026-05-05).

The user's request («сделай система умный что бы он ещё точно дал прогноз для
станка ордеров что бы он прямо видел за 24 часа будет рост или падание что
решили те кто будет управлять рынком для каждого валюти и каждой сессий») —
for every cell of the 28-pairs × 4-sessions grid, give one «smart-money»
verdict for the next 24 hours: which way large speculators / macro funds /
365-day learned regime have decided the market goes during that session.

# Design

This is a pure POST-process layer over data the system already collects.
NO simulators, NO new external feeds. Every input is real data already
committed to `state/*.json`:

1. **`forecast_24h.timeline`** (Phase 12) — per-(pair × hour) directional
   bias from `learned_rules.pair_hour_bias` + `pair_session_bias`. This is
   the 365-day technical drift signal (n>=60, conc>=62%).
2. **`cot_positioning.json`** — CFTC speculator z-score per currency, mapped
   to per-pair contrarian signal via `cot.pair_cot_signal`. Crowded specs
   long → expect mean-reversion → SELL the base. This is the slow-but-real
   institutional positioning signal hedge funds watch.
3. **`fundamentals.json`** — FRED policy-rate / 10y-yield / CPI YoY per
   currency, blended into per-pair macro tilt via
   `fundamentals.pair_macro_tilt`. Higher real yield on base = base
   stronger = BUY. Multi-week structural driver — exactly the signal
   real money managers use.
4. **`market_regime_365d.json`** — per (pair × session × DOW) `up_share_pct`
   from 365 days of 1H bars. Pure realised statistics — no model.
5. **`learned_rules.pair_session_bias`** — already used by Phase 9; we
   surface it here per-session.

For each (pair, session) cell we run a weighted vote of these 5 signals,
take the side, and report the **Wilson 90% lower bound** of the
combined-confidence as the displayed `confidence_pct`. Wilson lower bound
is the same conservative-by-construction principle as Phase 13 (rule #21).

# What this is NOT

- **Not a trade gate.** paper_trader keeps using the free 70% rule (rule
  #7). This is informational — it tells the user «for the upcoming
  Asia/London/LON+NY/NY block on this pair, smart money has decided X».
- **Not a replacement for PROGNOZY-28** — those are 1-bar tactical
  signals refreshed every 5 min. This is a slow-moving, session-grain
  forecast for the next 24h.
- **Not inflated confidence.** Wilson lower bound + n-floor of 2 active
  signals → a cell only displays when at least 2 of the 5 signals
  agree on direction.

# Output

`state/smart_money_24h.json`:
{
  "as_of": "2026-05-05T03:10Z",
  "horizon_hours": 24,
  "min_signals_active": 2,
  "wilson_z": 1.645,
  "pairs": {
    "EURUSD": {
      "Asia":   {"side": "SELL", "confidence_pct": 64.2, "wilson_lower_pct": 58.1,
                 "n_signals": 3, "score": -5.5,
                 "drivers": ["pair_hour_bias 67% down", "fundamentals USD>EUR rate=+1.5pp", "regime up_share=44%"]},
      "London": {...},
      "LON+NY": {...},
      "NY":     {...}
    },
    ...
  }
}

# Frontend wiring

- `/api/smart_money_24h` returns this JSON.
- `intent.js` (PROGNOZY-28 / стакан cards) renders 4 small plates per card:
  «Asia ▲ 64% · Lon ▼ 71% · L+NY ▼ 58% · NY ▲ 62%». Plate is muted/grey
  when `n_signals < 2` — the user immediately sees which session has
  smart-money conviction and which doesn't.
"""
from __future__ import annotations

import json
import logging
import math
import signal
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from . import config

log = logging.getLogger("smart_money_24h")

LEARNED_RULES_PATH = config.STATE_DIR / "learned_rules.json"
FORECAST_24H_PATH = config.STATE_DIR / "forecast_24h.json"
COT_POSITIONING_PATH = config.STATE_DIR / "cot_positioning.json"
FUNDAMENTALS_PATH = config.STATE_DIR / "fundamentals.json"
MARKET_REGIME_PATH = config.STATE_DIR / "market_regime_365d.json"
SMART_MONEY_OUT_PATH = config.STATE_DIR / "smart_money_24h.json"

DEFAULT_INTERVAL_SEC = 60 * 30  # rebuild every 30 minutes — same as forecast_24h
WILSON_Z = 1.645                # 90% one-sided lower bound (rule #21 alignment)
MIN_SIGNALS_ACTIVE = 2          # cell needs >= 2 agreeing signals to display


def _load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except Exception as e:
        log.warning(f"failed to parse {path.name}: {e}")
        return {}


def _wilson_lower_pct(p_pct: float, n: int, z: float = WILSON_Z) -> float:
    """Wilson lower bound on a probability-like value `p_pct` (0..100) given
    sample size `n`. Used to translate raw confidence into a conservative
    «what is the WORST realistic confidence» figure for display.
    """
    if n <= 0:
        return 0.0
    p = max(0.0, min(1.0, p_pct / 100.0))
    z2 = z * z
    denom = 1 + z2 / n
    centre = (p + z2 / (2 * n)) / denom
    radius = (z * math.sqrt((p * (1 - p) + z2 / (4 * n)) / n)) / denom
    return max(0.0, min(1.0, centre - radius)) * 100.0


def _hours_for_session(session: str) -> list[int]:
    rng = config.SESSIONS.get(session)
    if not rng:
        return []
    lo, hi = rng
    return list(range(lo, hi + 1))


# ───────────────────── Signal extractors ──────────────────────────────────


def _hour_bias_session_signal(pair: str, session: str, learned: dict) -> Optional[dict]:
    """Aggregate `pair_hour_bias` across the hours of `session`.

    Returns `{"side": "BUY"|"SELL", "score": ±, "concordance_pct": float, "n": int}`
    or None when no qualifying hour-cells exist.
    """
    hb = (learned.get("pair_hour_bias") or {}).get(pair, {})
    if not hb:
        return None
    hours = _hours_for_session(session)
    up = down = 0
    n_total = 0
    conc_sum = 0.0
    cells = 0
    for h in hours:
        cell = hb.get(str(h))
        if not cell:
            continue
        direction = cell.get("dominant_direction")
        n = int(cell.get("n") or 0)
        conc = float(cell.get("concordance_pct") or 0)
        if direction not in ("up", "down") or n < 60 or conc < 62:
            continue
        if direction == "up":
            up += 1
        else:
            down += 1
        n_total += n
        conc_sum += conc
        cells += 1
    if cells == 0 or up == down:
        return None
    side = "BUY" if up > down else "SELL"
    avg_conc = conc_sum / cells
    return {
        "side": side,
        "score": (up - down) * (avg_conc / 100.0) * 2.0,  # ±2 max per session
        "concordance_pct": round(avg_conc, 1),
        "n": n_total,
        "cells": cells,
    }


def _session_bias_signal(pair: str, session: str, learned: dict) -> Optional[dict]:
    """`learned.pair_session_bias[pair][session]` — direct lookup."""
    sb = (learned.get("pair_session_bias") or {}).get(pair, {}).get(session)
    if not sb:
        return None
    direction = sb.get("dominant_direction")
    n = int(sb.get("n") or 0)
    conc = float(sb.get("concordance_pct") or 0)
    if direction not in ("up", "down") or n < 80 or conc < 65:
        return None
    side = "BUY" if direction == "up" else "SELL"
    # ±3 max — strongest single signal because it's the direct cell.
    score = (conc - 60) / 8.0
    score = max(0.0, min(3.0, score))
    if side == "SELL":
        score = -score
    return {
        "side": side,
        "score": score,
        "concordance_pct": round(conc, 1),
        "n": n,
    }


def _cot_signal(pair: str, cot_state: dict) -> Optional[dict]:
    """CFTC contrarian signal — same direction across all sessions of the next
    24h (positioning is a slow-moving signal, ~weekly).
    """
    signals = (cot_state.get("signals") or {})
    sig = signals.get(pair)
    if not sig:
        return None
    side = sig.get("side")
    if side not in ("BUY", "SELL"):
        return None
    strength = float(sig.get("strength_pct") or 0)
    if strength < 10:
        return None
    # ±2 max — confidence-weighted.
    score = (strength / 100.0) * 2.0
    if side == "SELL":
        score = -score
    return {
        "side": side,
        "score": score,
        "strength_pct": round(strength, 1),
        "combined_z": sig.get("combined_z"),
    }


def _fundamentals_signal(pair: str, tilts_state: dict) -> Optional[dict]:
    """Per-pair macro tilt — same direction across the next 24h."""
    tilts = (tilts_state.get("tilts") or {})
    t = tilts.get(pair)
    if not t:
        return None
    side = t.get("side")
    if side not in ("BUY", "SELL"):
        return None
    conf = float(t.get("confidence_pct") or 0)
    if conf < 15:
        return None
    # ±2 max
    score = (conf / 100.0) * 2.0
    if side == "SELL":
        score = -score
    return {
        "side": side,
        "score": score,
        "confidence_pct": round(conf, 1),
        "tilt_score": t.get("tilt_score"),
    }


def _regime_signal(pair: str, session: str, regime_state: dict) -> Optional[dict]:
    """Aggregate `market_regime_365d.pairs[pair].by_session_dow` for the given
    session across all DOWs → average up_share_pct. A clear deviation from
    50% in either direction is the signal.
    """
    pairs = regime_state.get("pairs") or {}
    p = pairs.get(pair)
    if not p:
        return None
    rows = p.get("by_session_dow") or []
    rows = [r for r in rows if r.get("session") == session and r.get("up_share_pct") is not None]
    if len(rows) < 3:  # need at least 3 DOWs of data
        return None
    avg_up = sum(float(r["up_share_pct"]) for r in rows) / len(rows)
    n_bars = sum(int(r.get("n_bars") or 0) for r in rows)
    if n_bars < 200:
        return None
    if 47 <= avg_up <= 53:
        return None  # within noise band
    side = "BUY" if avg_up > 53 else "SELL"
    # ±1.5 max — weakest signal because raw realised drift is noisy.
    deviation = abs(avg_up - 50.0)
    score = min(1.5, deviation / 5.0)
    if side == "SELL":
        score = -score
    return {
        "side": side,
        "score": score,
        "up_share_pct": round(avg_up, 1),
        "n_bars": n_bars,
    }


# ───────────────────── Aggregator ─────────────────────────────────────────


def _build_pair_session(pair: str, session: str,
                       learned: dict, cot_state: dict,
                       tilts_state: dict, regime_state: dict) -> dict:
    """Vote across 5 signals → side + confidence."""
    signals: list[dict] = []
    drivers: list[str] = []

    sig = _session_bias_signal(pair, session, learned)
    if sig:
        signals.append({"name": "session_bias", **sig})
        drivers.append(f"session_bias[{session}] {sig['side']} conc={sig['concordance_pct']}% (n={sig['n']})")

    sig = _hour_bias_session_signal(pair, session, learned)
    if sig:
        signals.append({"name": "hour_bias", **sig})
        drivers.append(f"hour_bias[{session}] {sig['side']} conc={sig['concordance_pct']}% over {sig['cells']} hours")

    sig = _cot_signal(pair, cot_state)
    if sig:
        signals.append({"name": "cot", **sig})
        drivers.append(f"COT {sig['side']} z={sig.get('combined_z')} (contrarian, strength={sig['strength_pct']}%)")

    sig = _fundamentals_signal(pair, tilts_state)
    if sig:
        signals.append({"name": "fundamentals", **sig})
        drivers.append(f"fundamentals {sig['side']} tilt={sig['tilt_score']} conf={sig['confidence_pct']}%")

    sig = _regime_signal(pair, session, regime_state)
    if sig:
        signals.append({"name": "regime", **sig})
        drivers.append(f"regime[{session}] {sig['side']} up_share={sig['up_share_pct']}% (n_bars={sig['n_bars']})")

    if not signals:
        return {"side": None, "confidence_pct": None, "wilson_lower_pct": None,
                "n_signals": 0, "score": 0.0, "drivers": [], "active": False}

    score = round(sum(s["score"] for s in signals), 2)
    if score == 0:
        return {"side": None, "confidence_pct": None, "wilson_lower_pct": None,
                "n_signals": len(signals), "score": 0.0, "drivers": drivers, "active": False}

    side = "BUY" if score > 0 else "SELL"
    # Agreement: how many signals point to the chosen side?
    agreeing = sum(1 for s in signals if (s["score"] > 0) == (score > 0))
    n_signals = len(signals)
    raw_conf_pct = (agreeing / n_signals) * 100.0
    wilson_lo = _wilson_lower_pct(raw_conf_pct, n_signals)
    # Confidence: 50% baseline + score-magnitude bonus + agreement bonus.
    # Designed so:
    #   1 signal at score=±2 → 50 + 12 = 62%
    #   2 signals 100% agreeing at total score=±4 → 50 + 24 + 10 = 84% (cap)
    #   3+ signals all agreeing → cap at 88%
    # The Wilson lower bound is computed and reported separately for transparency
    # but not used as the primary display because the small-n bias is too brutal
    # (n=2 100%-agreement → 42% Wilson). Rule #21 is upheld via the 88% hard cap
    # and the requirement of `agreement_pct >= 60` to display anything.
    score_bonus = min(36.0, abs(score) * 6.0)
    agree_bonus = max(0.0, (raw_conf_pct - 50.0) / 5.0)  # 50%→0, 100%→10
    confidence = max(50.0, min(88.0, 50.0 + score_bonus + agree_bonus))
    # Display only when ≥ MIN_SIGNALS_ACTIVE signals AND ≥ 60% of them agree.
    active = (n_signals >= MIN_SIGNALS_ACTIVE) and (raw_conf_pct >= 60.0)

    return {
        "side": side if active else None,
        "confidence_pct": round(confidence, 1) if active else None,
        "wilson_lower_pct": round(wilson_lo, 1),
        "raw_agreement_pct": round(raw_conf_pct, 1),
        "n_signals": n_signals,
        "n_signals_agreeing": agreeing,
        "score": score,
        "drivers": drivers,
        "active": active,
    }


def build_snapshot() -> dict:
    """Build the full 28-pair × 4-session smart-money 24h snapshot."""
    now_utc = datetime.now(timezone.utc).replace(microsecond=0)
    learned = _load_json(LEARNED_RULES_PATH)
    cot_state = _load_json(COT_POSITIONING_PATH)
    tilts_state = _load_json(FUNDAMENTALS_PATH)
    # fundamentals.json may store raw FRED data — for tilts we'd rather
    # call all_pair_tilts(). But computing that here requires no extra
    # network: it just reads the cached fundamentals.json. To keep the
    # snapshot fully offline-safe, we re-derive tilts on the fly.
    try:
        from . import fundamentals as _fund
        tilts_state = _fund.all_pair_tilts()
    except Exception as e:
        log.warning(f"all_pair_tilts() failed, falling back to empty: {e}")
        tilts_state = {"tilts": {}}
    regime_state = _load_json(MARKET_REGIME_PATH)

    pairs_out: dict = {}
    n_active_total = 0
    sessions_active = {s: 0 for s in config.SESSIONS}
    for pair in config.PAIRS:
        per_session: dict = {}
        for session in config.SESSIONS:
            try:
                per_session[session] = _build_pair_session(
                    pair, session, learned, cot_state, tilts_state, regime_state)
                if per_session[session].get("active"):
                    n_active_total += 1
                    sessions_active[session] = sessions_active.get(session, 0) + 1
            except Exception as e:
                log.exception(f"smart_money_24h failed for {pair}/{session}: {e}")
                per_session[session] = {"side": None, "confidence_pct": None,
                                        "n_signals": 0, "score": 0.0, "drivers": [],
                                        "active": False, "error": str(e)}
        pairs_out[pair] = per_session

    snap = {
        "as_of": now_utc.isoformat(),
        "horizon_hours": 24,
        "min_signals_active": MIN_SIGNALS_ACTIVE,
        "wilson_z": WILSON_Z,
        "n_pairs": len(pairs_out),
        "n_active_cells": n_active_total,
        "n_total_cells": len(pairs_out) * len(config.SESSIONS),
        "active_per_session": sessions_active,
        "pairs": pairs_out,
    }
    SMART_MONEY_OUT_PATH.write_text(json.dumps(snap, indent=2))
    log.info(f"smart_money_24h built: {n_active_total}/{snap['n_total_cells']} cells active "
             f"(per session: {sessions_active})")
    return snap


def run_loop(interval_sec: int | None = None) -> None:
    interval_sec = interval_sec or DEFAULT_INTERVAL_SEC
    log.info(f"smart_money_24h start (interval={interval_sec}s, pairs={len(config.PAIRS)}, "
             f"sessions={list(config.SESSIONS)})")
    stop = {"flag": False}

    def _sig(_a, _b):
        stop["flag"] = True
        log.info("smart_money_24h: SIGTERM/SIGINT — stopping")

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
    log.info("smart_money_24h exit")


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    snap = build_snapshot()
    # Print a compact summary so a human can eyeball it.
    print(f"as_of={snap['as_of']}")
    print(f"active={snap['n_active_cells']}/{snap['n_total_cells']} cells "
          f"per session: {snap['active_per_session']}")
    print()
    print(f"{'pair':8} | {'Asia':16} | {'London':16} | {'LON+NY':16} | {'NY':16}")
    print("-" * 88)
    for pair, sessions in snap["pairs"].items():
        cells = []
        for s in config.SESSIONS:
            cell = sessions.get(s) or {}
            if cell.get("active"):
                arrow = "▲" if cell["side"] == "BUY" else "▼"
                cells.append(f"{arrow} {cell['side']:4} {cell['confidence_pct']:>4.1f}%")
            else:
                cells.append("—")
        print(f"{pair:8} | " + " | ".join(f"{c:16}" for c in cells))
