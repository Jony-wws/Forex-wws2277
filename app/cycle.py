"""Strict 5-hour forecast cycle manager.

Every 5 hours (UTC boundaries 00, 05, 10, 15, 20) the system:

1. Picks the top 5 strongest signals from all 28 pairs (always exactly 5,
   even when the market is weak).
2. Records each forecast with the entry price and a quality tier:
   - **PREMIUM** when ``confidence >= 80%`` AND ``score / max_score >= 0.40``
     AND all four senior timeframes (M15+H1+H4+D1) are aligned in the same
     direction.
   - **MEDIUM** otherwise — still in the top-5 but flagged as weaker.
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
TOP_N = 5
MIN_PREMIUM = 3                # always at least N PREMIUM per cycle
WIN_MOVE_PCT = 0.10            # binary-options win threshold (in %)
# Strict PREMIUM thresholds (very strict — «очень и очень строгий контроль»)
PREMIUM_CONFIDENCE = 85
PREMIUM_RATIO = 0.50           # score / max_score
PREMIUM_ADX = 25.0             # min ADX of the H1 trend
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


def _classify_tier(forecast: dict) -> str:
    confidence = int(forecast.get("confidence") or 0)
    score = abs(int(forecast.get("score") or 0))
    max_score = max(1, int(forecast.get("max_score") or 1))
    ratio = score / max_score
    aligned = bool(forecast.get("multi_tf_aligned"))
    adx = float((forecast.get("indicators") or {}).get("ADX", 0) or 0)
    if (
        confidence >= PREMIUM_CONFIDENCE
        and ratio >= PREMIUM_RATIO
        and aligned
        and adx >= PREMIUM_ADX
    ):
        return "PREMIUM"
    return "MEDIUM"


def _ensure_min_premium(selected: list[dict]) -> None:
    """Guarantee at least ``MIN_PREMIUM`` PREMIUM forecasts per cycle.

    The strict criteria above can fail on weak markets. To keep the
    user-promised «at least 3 strong forecasts every 5 hours» contract,
    promote the top-by-score MEDIUMs from the already-selected top-5
    until ``MIN_PREMIUM`` is reached. Promoted entries get a
    ``tier_fallback`` flag so the UI can show that they hit the minimum
    via fallback rather than the strict gate.
    """
    premium_count = sum(1 for f in selected if f.get("tier") == "PREMIUM")
    if premium_count >= MIN_PREMIUM:
        return
    mediums = [f for f in selected if f.get("tier") == "MEDIUM"]
    mediums.sort(
        key=lambda f: (abs(int(f.get("score") or 0)), int(f.get("confidence") or 0)),
        reverse=True,
    )
    for f in mediums[: MIN_PREMIUM - premium_count]:
        f["tier"] = "PREMIUM"
        f["tier_fallback"] = True


def _eligible(forecast: dict) -> bool:
    """A pair must at least have a directional bias to be selectable."""
    return forecast.get("side") in ("BUY", "SELL")


def _select_top5(forecasts_by_pair: dict[str, dict]) -> list[dict]:
    """Pick the top 5 forecasts by ``|score|`` then by confidence."""
    candidates = [f for f in forecasts_by_pair.values() if _eligible(f)]
    candidates.sort(
        key=lambda f: (abs(int(f.get("score") or 0)), int(f.get("confidence") or 0)),
        reverse=True,
    )
    return candidates[:TOP_N]


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
            top5 = _select_top5(forecasts_by_pair)
            session = detect_session(active_start)
            selected: list[dict] = []
            for f in top5:
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
                    "tier_fallback": False,
                    "entry_price": round(float(price), 5),
                    "forecast_5h": f.get("forecast_5h"),
                    "forecast_24h": f.get("forecast_24h"),
                    "evaluated_5h": False,
                    "evaluated_24h": False,
                })
            _ensure_min_premium(selected)

            current = {
                "cycle_start_utc": active_iso,
                "next_cycle_utc": _iso(active_start + timedelta(hours=CYCLE_HOURS)),
                "selected": selected,
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

    if current is None:
        next_start = _next_cycle_start(now)
        return {
            "current_cycle": None,
            "next_cycle_utc": _iso(next_start),
            "seconds_to_next_cycle": int((next_start - now).total_seconds()),
            "winrate_5h": _winrate_over([], "5h"),
            "winrate_24h": _winrate_over([], "24h"),
            "history_cycles": 0,
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
    }
