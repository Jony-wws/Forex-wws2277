"""Aggregations for the Статистика dashboard tab.

Reads ``state/forecasts.json`` (the on-disk archive of every strict 5-hour
cycle) and computes daily winrate, per-pair and per-tier performance, plus
an overall summary.  The dashboard fetches this lazily when the user
activates the «Статистика» tab.

A single forecast is **counted as a decision** only when its 5-hour
horizon has been evaluated (``result_5h`` is ``"win"`` or ``"loss"``).
Unevaluated picks are still counted in ``picks`` so the user can see how
many ideas the system has produced — only ``wins``/``losses``/``wr_pct``
are restricted to closed trades, mirroring how ``cycle._winrate_over``
already calculates the rolling WR shown on the main page.

The function is pure (no globals) and the FastAPI route layer keeps a
60-second in-memory cache to avoid disk pressure on every request.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .config import TZ_UTC5

# Resolve relative to the repo root so the path matches ``cycle.STATE_FILE``.
STATE_DIR = Path(__file__).resolve().parent.parent / "state"
STATE_FILE = STATE_DIR / "forecasts.json"

DAILY_WINDOW_DAYS = 30
KNOWN_TIERS = ("PREMIUM", "STRONG", "NORMAL")


def _empty_payload() -> dict[str, Any]:
    """Shape returned when ``state/forecasts.json`` is missing or unreadable."""
    today = datetime.now(TZ_UTC5).date()
    daily: list[dict[str, Any]] = []
    for offset in range(DAILY_WINDOW_DAYS - 1, -1, -1):
        d = today - timedelta(days=offset)
        daily.append(
            {
                "date_utc5": d.strftime("%Y-%m-%d"),
                "picks": 0,
                "wins": 0,
                "losses": 0,
                "wr_pct": 0.0,
            }
        )
    per_tier = [
        {"tier": tier, "picks": 0, "wins": 0, "losses": 0, "wr_pct": 0.0}
        for tier in KNOWN_TIERS
    ]
    return {
        "daily_wr": daily,
        "per_pair": [],
        "per_tier": per_tier,
        "summary": {
            "total_picks": 0,
            "total_wins": 0,
            "total_losses": 0,
            "overall_wr_pct": 0.0,
            "last_cycle_utc5": None,
        },
    }


def _parse_iso_utc(s: str | None) -> datetime | None:
    if not s or not isinstance(s, str):
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _wr_pct(wins: int, losses: int) -> float:
    decisions = wins + losses
    if decisions == 0:
        return 0.0
    return round(100.0 * wins / decisions, 1)


def _load_state() -> dict[str, Any]:
    if not STATE_FILE.exists():
        return {}
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    return data


def _iter_cycles(state: dict[str, Any]) -> list[dict[str, Any]]:
    cycles: list[dict[str, Any]] = []
    history = state.get("history") or []
    if isinstance(history, list):
        cycles.extend(c for c in history if isinstance(c, dict))
    current = state.get("current")
    if isinstance(current, dict):
        cycles.append(current)
    return cycles


def _format_utc5(dt: datetime) -> str:
    return dt.astimezone(TZ_UTC5).strftime("%Y-%m-%d %H:%M")


def compute_stats() -> dict[str, Any]:
    """Compute the full /api/stats payload from state/forecasts.json.

    Always returns the full schema described in the docstring of the
    module — even when the file is missing or empty — so the front-end can
    render placeholders without special-casing.
    """
    state = _load_state()
    if not state:
        return _empty_payload()

    cycles = _iter_cycles(state)
    if not cycles:
        return _empty_payload()

    today_utc5 = datetime.now(TZ_UTC5).date()
    earliest_day = today_utc5 - timedelta(days=DAILY_WINDOW_DAYS - 1)

    # Initialise daily buckets so even days without picks appear on the chart.
    daily_map: dict[str, dict[str, int]] = {}
    for offset in range(DAILY_WINDOW_DAYS - 1, -1, -1):
        d = today_utc5 - timedelta(days=offset)
        daily_map[d.strftime("%Y-%m-%d")] = {"picks": 0, "wins": 0, "losses": 0}

    per_pair_map: dict[str, dict[str, Any]] = {}
    per_tier_map: dict[str, dict[str, int]] = {
        tier: {"picks": 0, "wins": 0, "losses": 0} for tier in KNOWN_TIERS
    }

    total_picks = 0
    total_wins = 0
    total_losses = 0
    last_cycle_dt: datetime | None = None

    for cycle in cycles:
        start_utc = _parse_iso_utc(cycle.get("cycle_start_utc"))
        if start_utc is None:
            continue
        if last_cycle_dt is None or start_utc > last_cycle_dt:
            last_cycle_dt = start_utc

        date_utc5 = start_utc.astimezone(TZ_UTC5).date()
        in_daily_window = date_utc5 >= earliest_day
        date_key = date_utc5.strftime("%Y-%m-%d")

        for f in cycle.get("selected") or []:
            if not isinstance(f, dict):
                continue
            pair = f.get("pair")
            if not pair:
                continue
            tier = f.get("tier") if f.get("tier") in KNOWN_TIERS else "NORMAL"
            result = f.get("result_5h")
            is_win = result == "win"
            is_loss = result == "loss"

            total_picks += 1
            if is_win:
                total_wins += 1
            elif is_loss:
                total_losses += 1

            if in_daily_window:
                bucket = daily_map[date_key]
                bucket["picks"] += 1
                if is_win:
                    bucket["wins"] += 1
                elif is_loss:
                    bucket["losses"] += 1

            tier_bucket = per_tier_map[tier]
            tier_bucket["picks"] += 1
            if is_win:
                tier_bucket["wins"] += 1
            elif is_loss:
                tier_bucket["losses"] += 1

            pair_bucket = per_pair_map.setdefault(
                pair,
                {
                    "pair": pair,
                    "picks": 0,
                    "wins": 0,
                    "losses": 0,
                    "_last_seen_dt": start_utc,
                },
            )
            pair_bucket["picks"] += 1
            if is_win:
                pair_bucket["wins"] += 1
            elif is_loss:
                pair_bucket["losses"] += 1
            if start_utc > pair_bucket["_last_seen_dt"]:
                pair_bucket["_last_seen_dt"] = start_utc

    daily_wr = [
        {
            "date_utc5": date_key,
            "picks": v["picks"],
            "wins": v["wins"],
            "losses": v["losses"],
            "wr_pct": _wr_pct(v["wins"], v["losses"]),
        }
        for date_key, v in daily_map.items()
    ]

    per_pair = [
        {
            "pair": v["pair"],
            "picks": v["picks"],
            "wins": v["wins"],
            "losses": v["losses"],
            "wr_pct": _wr_pct(v["wins"], v["losses"]),
            "last_seen": _format_utc5(v["_last_seen_dt"]),
        }
        for v in per_pair_map.values()
    ]
    per_pair.sort(key=lambda r: (-r["picks"], r["pair"]))

    per_tier = [
        {
            "tier": tier,
            "picks": per_tier_map[tier]["picks"],
            "wins": per_tier_map[tier]["wins"],
            "losses": per_tier_map[tier]["losses"],
            "wr_pct": _wr_pct(per_tier_map[tier]["wins"], per_tier_map[tier]["losses"]),
        }
        for tier in KNOWN_TIERS
    ]

    return {
        "daily_wr": daily_wr,
        "per_pair": per_pair,
        "per_tier": per_tier,
        "summary": {
            "total_picks": total_picks,
            "total_wins": total_wins,
            "total_losses": total_losses,
            "overall_wr_pct": _wr_pct(total_wins, total_losses),
            "last_cycle_utc5": _format_utc5(last_cycle_dt) if last_cycle_dt else None,
        },
    }
