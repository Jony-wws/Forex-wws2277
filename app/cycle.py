"""Strict 5-hour forecast cycle manager.

Every 5 hours (UTC boundaries 00, 05, 10, 15, 20) the system:

1. Picks the **strong sustained trends** from all 28 pairs.  A pair must
   pass the hard `is_strong_trend` gate from ``analyzer.py`` to qualify
   for the STRONG / PREMIUM tier:

       confidence ≥ 88
       score / max_score ≥ 0.55
       all 4 senior timeframes aligned (D1 + H4 + H1 + M15)
       ADX H1 ≥ 25 AND ADX H4 ≥ 20
       trend_persistence_5h ≥ 80 % (≥ 4 of 5 H1 bars in direction)

   The cycle ALWAYS publishes between ``MIN_PICKS = 3`` and
   ``MAX_PICKS = 5`` forecasts so the user is guaranteed at least three
   ideas every 5 hours.  When more than 3 pairs pass the hard gate they
   are ranked by composite quality (persistence × ADX × confidence) and
   the top ``MAX_PICKS`` are kept.  When fewer than 3 pass, the slate is
   topped up with the next-best candidates by the same composite score —
   those backups are tagged with the ``NORMAL`` tier so the UI shows
   honestly that the 5-hour trend isn't perfectly clean.

2. Records each forecast with the entry price and a quality tier:
   - **PREMIUM** — ``is_strong_trend`` AND ADX H1 ≥ 28 AND persistence = 100 %
   - **STRONG**  — ``is_strong_trend`` (passes the hard gate)
   - **NORMAL**  — best of the rest, used to top the slate up to MIN_PICKS
     when fewer than 3 pairs cleared the strong gate.

3. Evaluates earlier cycles (5h-ago for the 5h forecast, 24h-ago for the
   24h forecast) against the current price.
4. Tracks a rolling winrate over the last 10 cycles for both horizons.

Win/loss is **binary** (no push):

* WIN  — at expiry the price has moved at least ``WIN_MOVE_PCT`` (0.10%)
  in the predicted direction.
* LOSS — anything else (against us, sideways, or in the right direction
  but below threshold — too close to the entry to be safe for binary
  options).

State is persisted to ``state/forecasts.json`` so winrate survives a
restart.
"""
from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .config import detect_session
from .prices import get_current_price

log = logging.getLogger("cycle")

CYCLE_HOURS = 5
MIN_PICKS = 3                  # always try to publish at least 3 strong picks
MAX_PICKS = 5                  # never publish more than 5
WIN_MOVE_PCT = 0.10            # binary-options win threshold (in %)

# Hard gate for the STRONG tier (matches analyzer.is_strong_trend) — the
# strict 5-hour cycle promotes a forecast only when every one of these
# holds simultaneously.
STRONG_CONFIDENCE = 88
STRONG_RATIO = 0.55
STRONG_ADX_H1 = 25.0
STRONG_ADX_H4 = 20.0
STRONG_PERSISTENCE = 80.0

# PREMIUM is a strict subset of STRONG with even tighter ADX/persistence.
PREMIUM_ADX_H1 = 28.0
PREMIUM_PERSISTENCE = 100.0

HISTORY_KEEP_CYCLES = 60       # keep up to 60 finished cycles (~12.5 days)
WINRATE_WINDOW_CYCLES = 10     # winrate is computed over the last N cycles

STATE_DIR = Path(__file__).resolve().parent.parent / "state"
STATE_FILE = STATE_DIR / "forecasts.json"

_LOCK = threading.Lock()
_STATE: dict[str, Any] = {
    "current": None,
    "history": [],
}


# ---------- helpers ----------------------------------------------------------


def _round_down_to_cycle(now: datetime) -> datetime:
    """Round ``now`` down to the previous 5-hour cycle boundary in UTC."""
    now = now.astimezone(timezone.utc)
    cycle_hour = (now.hour // CYCLE_HOURS) * CYCLE_HOURS
    return now.replace(hour=cycle_hour, minute=0, second=0, microsecond=0)


def _next_cycle_start(now: datetime) -> datetime:
    return _round_down_to_cycle(now) + timedelta(hours=CYCLE_HOURS)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def _passes_strong_gate(f: dict) -> bool:
    """Return True iff the forecast passes the hard "strong sustained trend" gate."""
    if f.get("side") not in ("BUY", "SELL"):
        return False
    # Trust the analyzer flag when present — it already encodes the same
    # five conditions — but recompute defensively from raw fields so older
    # snapshots stored before the gate existed don't accidentally pass.
    confidence = int(f.get("confidence") or 0)
    score_abs = abs(int(f.get("score") or 0))
    max_score = max(1, int(f.get("max_score") or 1))
    ratio = score_abs / max_score
    aligned = bool(f.get("multi_tf_aligned"))
    adx_h1 = float(f.get("adx_h1") or f.get("adx") or 0.0)
    adx_h4 = float(f.get("adx_h4") or 0.0)
    persistence = float(f.get("trend_persistence_5h") or 0.0)
    return (
        confidence >= STRONG_CONFIDENCE
        and ratio >= STRONG_RATIO
        and aligned
        and adx_h1 >= STRONG_ADX_H1
        and adx_h4 >= STRONG_ADX_H4
        and persistence >= STRONG_PERSISTENCE
    )


def _classify_tier(forecast: dict) -> str:
    """PREMIUM > STRONG > NORMAL.

    PREMIUM is reserved for forecasts that not only pass the strong gate
    but also have a very strong H1 trend (ADX ≥ 28) and *every* one of
    the last five H1 bars in the predicted direction.
    NORMAL is used for top-up picks when fewer than ``MIN_PICKS`` pairs
    pass the strong gate — those still represent the best available
    direction the system can find at that moment.
    """
    if not _passes_strong_gate(forecast):
        return "NORMAL"
    adx_h1 = float(forecast.get("adx_h1") or forecast.get("adx") or 0.0)
    persistence = float(forecast.get("trend_persistence_5h") or 0.0)
    if adx_h1 >= PREMIUM_ADX_H1 and persistence >= PREMIUM_PERSISTENCE:
        return "PREMIUM"
    return "STRONG"


def _eligible(forecast: dict) -> bool:
    """A pair must at least have a directional bias to be selectable."""
    return forecast.get("side") in ("BUY", "SELL")


def _quality_score(f: dict) -> tuple:
    """Sort key — higher is better.

    Order of precedence:
      1. Passes the strong sustained-trend gate (hard yes/no).
      2. Trend persistence over last 5 H1 bars (more bars = better).
      3. ADX H1 (stronger trend = better).
      4. ADX H4 (confirmation on the higher timeframe).
      5. |score| (raw indicator agreement).
      6. confidence.
    """
    return (
        1 if _passes_strong_gate(f) else 0,
        float(f.get("trend_persistence_5h") or 0.0),
        float(f.get("adx_h1") or f.get("adx") or 0.0),
        float(f.get("adx_h4") or 0.0),
        abs(int(f.get("score") or 0)),
        int(f.get("confidence") or 0),
    )


def _select_strict(
    forecasts_by_pair: dict[str, dict],
) -> tuple[list[dict], bool]:
    """Pick up to ``MAX_PICKS`` forecasts, preferring strong sustained trends.

    Returns ``(selected, weak_market)`` — ``weak_market`` is True when
    fewer than ``MIN_PICKS`` candidates pass the strong gate, signalling
    the UI to warn that no clear 5-hour trends exist this cycle.
    """
    candidates = [f for f in forecasts_by_pair.values() if _eligible(f)]
    candidates.sort(key=_quality_score, reverse=True)

    strong = [f for f in candidates if _passes_strong_gate(f)]
    weak_market = len(strong) < MIN_PICKS

    if not weak_market:
        # Plenty of strong trends — publish up to MAX_PICKS of them.
        return strong[:MAX_PICKS], False

    # Not enough strong trends — pad with the next best candidates so the
    # cycle is never empty, but mark weak_market so the UI can warn.
    selected = list(strong)
    for f in candidates:
        if f in selected:
            continue
        if len(selected) >= MIN_PICKS:
            break
        selected.append(f)
    return selected, True


# ---------- persistence -----------------------------------------------------


def _load_state() -> None:
    global _STATE
    if not STATE_FILE.exists():
        return
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict) and "history" in data:
            _STATE = data
            _STATE.setdefault("current", None)
            _STATE.setdefault("history", [])
    except Exception as e:
        log.warning(f"Could not load cycle state: {e}")


def _save_state() -> None:
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(
            json.dumps(_STATE, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception as e:
        log.warning(f"Could not save cycle state: {e}")


# ---------- evaluation ------------------------------------------------------


def _evaluate_forecast(
    forecast: dict, horizon: str, current_price: float | None
) -> None:
    """Mark a single forecast win/loss for the given horizon."""
    key_done = f"evaluated_{horizon}"
    if forecast.get(key_done):
        return

    entry = forecast.get("entry_price")
    side = forecast.get("side")
    if entry is None or side is None or current_price is None:
        return

    move_pct = (current_price - entry) / entry * 100.0
    in_direction = move_pct if side == "BUY" else -move_pct

    forecast[f"exit_price_{horizon}"] = round(float(current_price), 5)
    forecast[f"move_pct_{horizon}"] = round(float(in_direction), 4)
    forecast[f"result_{horizon}"] = "win" if in_direction >= WIN_MOVE_PCT else "loss"
    forecast[key_done] = True


def _evaluate_cycle(cycle: dict, now: datetime) -> bool:
    """Evaluate any 5h/24h forecasts in ``cycle`` whose horizon has elapsed.

    Returns True if anything changed (so we know to persist).
    """
    if not cycle:
        return False
    start = _parse_iso(cycle["cycle_start_utc"])
    age = now - start
    changed = False

    for f in cycle.get("selected", []):
        # 5h horizon
        if not f.get("evaluated_5h") and age >= timedelta(hours=5):
            price = get_current_price(f["pair"])
            if price is not None:
                _evaluate_forecast(f, "5h", price)
                changed = True
        # 24h horizon
        if not f.get("evaluated_24h") and age >= timedelta(hours=24):
            price = get_current_price(f["pair"])
            if price is not None:
                _evaluate_forecast(f, "24h", price)
                changed = True

    return changed


# ---------- winrate ---------------------------------------------------------


def _winrate_over(history: list[dict], horizon: str) -> dict:
    recent = history[-WINRATE_WINDOW_CYCLES:]
    wins = 0
    losses = 0
    for cycle in recent:
        for f in cycle.get("selected", []):
            result = f.get(f"result_{horizon}")
            if result == "win":
                wins += 1
            elif result == "loss":
                losses += 1
    decisions = wins + losses
    pct = round(100.0 * wins / decisions, 1) if decisions else 0.0
    return {
        "wins": wins,
        "losses": losses,
        "decisions": decisions,
        "winrate_pct": pct,
        "cycles": len(recent),
    }


# ---------- public API ------------------------------------------------------


def init() -> None:
    """Load any persisted state from disk (call once at startup)."""
    with _LOCK:
        _load_state()


def tick(forecasts_by_pair: dict[str, dict], now: datetime | None = None) -> None:
    """Advance the cycle state.

    Call this on every scanner pass.  It will:

    1. Evaluate any expired 5h / 24h forecasts in the current and historical
       cycles.
    2. Rotate the cycle when the wall-clock crosses the next 5h boundary
       (or initialise the very first cycle).
    """
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)

    with _LOCK:
        current = _STATE.get("current")
        history = _STATE.get("history", [])

        # 1. evaluate expired horizons in the current and historical cycles
        changed = _evaluate_cycle(current, now) if current else False
        for cyc in history:
            changed = _evaluate_cycle(cyc, now) or changed

        # 2. determine the active 5h bucket and rotate if needed
        active_start = _round_down_to_cycle(now)
        active_iso = _iso(active_start)

        need_new_cycle = (
            current is None
            or current.get("cycle_start_utc") != active_iso
        )

        if need_new_cycle:
            # Move the previous cycle into history (it is at least
            # 5h old at this point — its 5h horizon is ready to score).
            if current is not None:
                _evaluate_cycle(current, now)
                history.append(current)
                # Keep history bounded
                if len(history) > HISTORY_KEEP_CYCLES:
                    history[:] = history[-HISTORY_KEEP_CYCLES:]
                changed = True

            # Build the new cycle from the current snapshot
            top, weak_market = _select_strict(forecasts_by_pair)
            session = detect_session(active_start)
            selected: list[dict] = []
            for f in top:
                price = f.get("price") or get_current_price(f["pair"])
                if price is None:
                    continue
                tier = _classify_tier(f)
                selected.append({
                    "pair": f["pair"],
                    "name_ru": f.get("name_ru"),
                    "side": f["side"],
                    "confidence": int(f.get("confidence") or 0),
                    "score": int(f.get("score") or 0),
                    "max_score": int(f.get("max_score") or 0),
                    "strength": f.get("strength"),
                    "session": session,
                    "tier": tier,
                    "adx_h1": float(f.get("adx_h1") or f.get("adx") or 0.0),
                    "adx_h4": float(f.get("adx_h4") or 0.0),
                    "trend_persistence_5h": float(
                        f.get("trend_persistence_5h") or 0.0
                    ),
                    "trend_persistence_bars": int(
                        f.get("trend_persistence_bars") or 0
                    ),
                    "multi_tf_aligned": bool(f.get("multi_tf_aligned")),
                    "entry_price": round(float(price), 5),
                    "forecast_5h": f.get("forecast_5h"),
                    "forecast_24h": f.get("forecast_24h"),
                    "evaluated_5h": False,
                    "evaluated_24h": False,
                })

            current = {
                "cycle_start_utc": active_iso,
                "next_cycle_utc": _iso(active_start + timedelta(hours=CYCLE_HOURS)),
                "selected": selected,
                "weak_market": weak_market,
                "strong_count": sum(
                    1 for s in selected if s["tier"] in ("PREMIUM", "STRONG")
                ),
            }
            _STATE["current"] = current
            _STATE["history"] = history
            changed = True

        if changed:
            _save_state()


def snapshot(now: datetime | None = None) -> dict:
    """Return the JSON-serialisable view consumed by the API/UI."""
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)

    with _LOCK:
        current = _STATE.get("current")
        history = list(_STATE.get("history", []))

    strong_gate = {
        "confidence": STRONG_CONFIDENCE,
        "ratio": STRONG_RATIO,
        "adx_h1": STRONG_ADX_H1,
        "adx_h4": STRONG_ADX_H4,
        "persistence_5h": STRONG_PERSISTENCE,
    }
    if current is None:
        next_start = _next_cycle_start(now)
        return {
            "current_cycle": None,
            "next_cycle_utc": _iso(next_start),
            "seconds_to_next_cycle": int((next_start - now).total_seconds()),
            "winrate_5h": _winrate_over([], "5h"),
            "winrate_24h": _winrate_over([], "24h"),
            "history_cycles": 0,
            "win_threshold_pct": WIN_MOVE_PCT,
            "min_picks": MIN_PICKS,
            "max_picks": MAX_PICKS,
            "strong_gate": strong_gate,
        }

    next_start = _parse_iso(current["next_cycle_utc"])
    seconds_left = max(0, int((next_start - now).total_seconds()))

    return {
        "current_cycle": current,
        "next_cycle_utc": current["next_cycle_utc"],
        "seconds_to_next_cycle": seconds_left,
        "winrate_5h": _winrate_over(history, "5h"),
        "winrate_24h": _winrate_over(history, "24h"),
        "history_cycles": len(history),
        "win_threshold_pct": WIN_MOVE_PCT,
        "min_picks": MIN_PICKS,
        "max_picks": MAX_PICKS,
        "strong_gate": strong_gate,
    }
