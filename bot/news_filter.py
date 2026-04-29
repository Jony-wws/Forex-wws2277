"""
News filter using free Forex Factory JSON feed.
Caches for 30 minutes. Returns list of high-impact events for the next 24h.
Bot pauses 60 minutes BEFORE and 30 minutes AFTER any high-impact event
that affects either currency in the trading pair.
"""
import json, time, requests
from datetime import datetime, timezone, timedelta
from pathlib import Path
from dateutil import parser as dtparser

CACHE_FILE = Path("/home/ubuntu/deriv_bot/logs/news_cache.json")
CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
FEED_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
CACHE_TTL_SEC = 1800  # 30 min

# Pause window around high-impact news (in minutes)
PAUSE_BEFORE_MIN = 60
PAUSE_AFTER_MIN = 30


def fetch_calendar(force: bool = False):
    """Returns list of events with parsed UTC datetime."""
    if not force and CACHE_FILE.exists():
        age = time.time() - CACHE_FILE.stat().st_mtime
        if age < CACHE_TTL_SEC:
            with open(CACHE_FILE) as f:
                return json.load(f)
    try:
        r = requests.get(FEED_URL, headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
        r.raise_for_status()
        events = r.json()
        for e in events:
            d = dtparser.parse(e["date"])
            e["dt_utc"] = d.astimezone(timezone.utc).isoformat()
        with open(CACHE_FILE, "w") as f:
            json.dump(events, f)
        return events
    except Exception as ex:
        print(f"[news_filter] fetch failed: {ex}")
        if CACHE_FILE.exists():
            with open(CACHE_FILE) as f:
                return json.load(f)
        return []


def is_blackout_for_pair(pair: str, now_utc: datetime = None) -> tuple[bool, str]:
    """Check if any high-impact news affects this pair within the pause window.
    Returns (is_blocked, reason).
    """
    if now_utc is None:
        now_utc = datetime.now(timezone.utc)
    events = fetch_calendar()
    # Currencies that affect this pair
    pair = pair.upper()
    if len(pair) != 6:
        return False, "invalid_pair"
    base, quote = pair[:3], pair[3:]
    relevant_currencies = {base, quote}

    for e in events:
        if e.get("impact") != "High": continue
        if e.get("country") not in relevant_currencies: continue
        try:
            ev_time = dtparser.parse(e["dt_utc"])
        except Exception:
            continue
        delta_min = (ev_time - now_utc).total_seconds() / 60
        # Block from -PAUSE_BEFORE to +PAUSE_AFTER
        if -PAUSE_AFTER_MIN <= delta_min <= PAUSE_BEFORE_MIN:
            ev_local = ev_time.strftime("%Y-%m-%d %H:%M UTC")
            return True, f"news_blackout: {e.get('country')} {e.get('title')} at {ev_local} (Δ={delta_min:+.0f} min)"
    return False, "ok"


def upcoming_high_impact(hours_ahead: int = 24) -> list:
    """Return high-impact events within next N hours."""
    now = datetime.now(timezone.utc)
    cutoff = now + timedelta(hours=hours_ahead)
    events = fetch_calendar()
    result = []
    for e in events:
        if e.get("impact") != "High": continue
        try:
            ev_time = dtparser.parse(e["dt_utc"])
        except Exception:
            continue
        if now <= ev_time <= cutoff:
            result.append({
                "country": e.get("country"),
                "title": e.get("title"),
                "ev_time_utc": ev_time.isoformat(),
                "minutes_from_now": int((ev_time - now).total_seconds() / 60),
                "forecast": e.get("forecast"),
                "previous": e.get("previous"),
            })
    return sorted(result, key=lambda x: x["minutes_from_now"])


if __name__ == "__main__":
    import sys
    print("=== Upcoming high-impact events (next 48h) ===")
    for e in upcoming_high_impact(48):
        print(f"  {e['minutes_from_now']:>+5}min  {e['country']:<3} {e['title']:<50} fcst={e['forecast']}")
    print("\n=== Blackout check for pairs ===")
    for p in ["USDJPY", "EURUSD", "GBPUSD", "AUDJPY", "EURCHF", "GBPCHF"]:
        blocked, reason = is_blackout_for_pair(p)
        mark = "BLOCKED" if blocked else "ok"
        print(f"  {p}: {mark} - {reason}")
