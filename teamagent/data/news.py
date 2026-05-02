"""ForexFactory RSS — high-impact новости (для blackout ±30 мин).

RSS бесплатный, без ключа. Считаем high-impact событие если в title встречается
любое из тикеров и категория Red.
"""
from __future__ import annotations
import logging
import time
from datetime import datetime, timezone, timedelta
from typing import Any

import re

import feedparser
import requests

log = logging.getLogger("news")

FF_RSS = "https://nfs.faireconomy.media/ff_calendar_thisweek.xml"
HIGH_IMPACT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; TeamAgent/1.0)",
    "Accept": "application/rss+xml,application/xml;q=0.9,*/*;q=0.8",
}

_CACHE: dict[str, Any] = {"ts": 0.0, "events": []}
_CACHE_TTL_SEC = 15 * 60   # 15 минут


def _refresh_if_needed() -> None:
    if time.time() - _CACHE["ts"] < _CACHE_TTL_SEC:
        return
    try:
        resp = requests.get(FF_RSS, headers=HIGH_IMPACT_HEADERS, timeout=15)
        resp.raise_for_status()
        feed = feedparser.parse(resp.content)
    except Exception as e:
        log.warning(f"forexfactory fetch failed: {e}")
        return

    events = []
    for entry in feed.entries:
        title = entry.get("title", "")
        # ff RSS даёт описание с impact — Red/Orange/Yellow
        descr = entry.get("description", "") or entry.get("summary", "")
        # whole-word match to avoid false positives (e.g. "red" inside "credit")
        impact = "Yellow"
        for color in ("Red", "Orange", "Yellow"):
            if re.search(rf"\b{color}\b", descr, re.IGNORECASE):
                impact = color
                break
        # event time
        pub = entry.get("published_parsed")
        if pub:
            ts = datetime(*pub[:6], tzinfo=timezone.utc)
        else:
            continue
        events.append({
            "title": title,
            "time": ts,
            "impact": impact,
        })

    _CACHE["ts"] = time.time()
    _CACHE["events"] = events


def is_blackout(pair: str, when: datetime, window_min: int = 30) -> bool:
    """True если сейчас (или в момент when) идёт high-impact событие по любой
    из валют пары — за ±window_min от события.
    """
    _refresh_if_needed()
    base, quote = pair[:3], pair[3:]
    target = when.astimezone(timezone.utc) if when.tzinfo else when.replace(tzinfo=timezone.utc)

    for ev in _CACHE["events"]:
        if ev["impact"] != "Red":
            continue
        title = ev["title"].upper()
        if base in title or quote in title:
            delta = abs((ev["time"] - target).total_seconds()) / 60
            if delta <= window_min:
                return True
    return False


def upcoming_high_impact(pair: str, hours_ahead: int = 6) -> list[dict]:
    """Предстоящие high-impact события по валютам пары — для UI."""
    _refresh_if_needed()
    base, quote = pair[:3], pair[3:]
    now = datetime.now(timezone.utc)
    until = now + timedelta(hours=hours_ahead)
    out = []
    for ev in _CACHE["events"]:
        if ev["impact"] != "Red":
            continue
        title = ev["title"].upper()
        if (base in title or quote in title) and now <= ev["time"] <= until:
            out.append({
                "title": ev["title"],
                "time": ev["time"].isoformat(),
                "impact": ev["impact"],
            })
    return out
