"""Master event archive aggregator — combines all sources into one timeline.

Output: list of event dicts sorted by ts (UTC).
Each event has: ts, currency, type, title, impact, plus source-specific extras.

Persisted to `state/events_365d.json` for fast re-use.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import cb_calendar, fred_calendar, cot_calendar, geo_calendar

log = logging.getLogger("events.archive")

STATE_FILE = Path(__file__).resolve().parents[1] / "state" / "events_365d.json"


def build_archive(end: datetime | None = None, days: int = 400) -> dict:
    """Build full event archive for `days` ending at `end` (default = utcnow).

    Slightly oversize the lookback (400 days vs 365) so events near the boundary
    have prior values available for "delta_vs_prev" computation.
    """
    if end is None:
        end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)

    sources = {
        "central_bank": cb_calendar.all_events(start, end),
        "fred_us": fred_calendar.all_events(start, end),
        "cot": cot_calendar.all_events(start, end),
        "geo": geo_calendar.all_events(start, end),
    }
    log.info(
        "archive built: cb=%d fred=%d cot=%d geo=%d",
        len(sources["central_bank"]),
        len(sources["fred_us"]),
        len(sources["cot"]),
        len(sources["geo"]),
    )

    # Flat sorted list
    flat: list[dict] = []
    for src_name, evs in sources.items():
        for ev in evs:
            ev2 = dict(ev)
            ev2["source"] = src_name
            flat.append(ev2)
    flat.sort(key=lambda e: e["ts"])

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "lookback_days": days,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "counts": {k: len(v) for k, v in sources.items()},
        "total": len(flat),
        "events": flat,
    }


def save(archive: dict) -> Path:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(archive, indent=2, ensure_ascii=False))
    return STATE_FILE


def load() -> dict | None:
    if not STATE_FILE.exists():
        return None
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return None


if __name__ == "__main__":
    import logging as _l
    _l.basicConfig(level=_l.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    arch = build_archive()
    out = save(arch)
    print(f"saved {arch['total']} events ({arch['counts']}) → {out}")
