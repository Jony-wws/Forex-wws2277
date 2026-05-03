"""
Forex market hours module.

Forex trades 24/5 — opens **Sunday 17:00 New-York-local time** (Sydney
session) and closes **Friday 17:00 New-York-local time** (NY close).

New York observes US daylight saving (EDT = UTC-4 from 2nd Sunday of
March through 1st Sunday of November, EST = UTC-5 the rest of the
year), so the same 5 pm NY moment is **21:00 UTC in summer** and
**22:00 UTC in winter**. Hard-coding 22:00 UTC year-round (the previous
implementation) made the countdown 1 hour off during DST. This module
now anchors to `America/New_York` via `zoneinfo` and converts to UTC
automatically.

The dashboard renders times in UTC, NY local, and UTC+5 (user's local).

This module is intentionally light-weight — only stdlib (`datetime`,
`zoneinfo`) so it loads fast and can be imported by every trader /
dashboard endpoint without adding latency.

Public API:
  - is_market_open(at: datetime | None = None) -> bool
  - seconds_until_close(at: datetime | None = None) -> int
      Returns 0 if market is currently closed.
  - seconds_until_open(at: datetime | None = None) -> int
      Returns 0 if market is currently open.
  - next_close(at: datetime | None = None) -> datetime
  - next_open(at: datetime | None = None) -> datetime
  - max_safe_expiry_hours(at: datetime | None = None,
                          min_buffer_minutes: int = 15) -> int
      Maximum expiry (in whole hours) that still settles before market close.
      Returns 0 if market is closed or closing soon.
  - clip_expiry_hours(desired_hours: int,
                      at: datetime | None = None,
                      min_buffer_minutes: int = 15) -> int
      Clips desired expiry so the trade settles before market close.
  - market_status() -> dict
      Snapshot for dashboard: {is_open, status_emoji, status_text,
      seconds_until_close, seconds_until_open, next_event_iso, ...}

Convention used here (NY-local, DST-aware):
  - Open  = Sunday 17:00 America/New_York  (= 21:00 UTC in EDT, 22:00 UTC in EST)
  - Close = Friday 17:00 America/New_York  (= 21:00 UTC in EDT, 22:00 UTC in EST)
This matches the standard FX wholesale week.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

_NY = ZoneInfo("America/New_York")

# Friday close: weekday=4 (Mon=0..Sun=6), hour=17:00 New-York-local
_FRIDAY_CLOSE_WEEKDAY = 4
_FRIDAY_CLOSE_HOUR_NY = 17

# Sunday open: weekday=6, hour=17:00 New-York-local
_SUNDAY_OPEN_WEEKDAY = 6
_SUNDAY_OPEN_HOUR_NY = 17


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _ensure_utc(dt: datetime | None) -> datetime:
    if dt is None:
        return _utcnow()
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def is_market_open(at: datetime | None = None) -> bool:
    """True if Forex is open at the given moment (DST-aware).

    Open: Sunday 17:00 NY-local → Friday 17:00 NY-local.
    Closed: Friday 17:00 NY-local → Sunday 17:00 NY-local.
    """
    t_utc = _ensure_utc(at)
    t = t_utc.astimezone(_NY)
    wd = t.weekday()  # Mon=0..Sun=6 — uses NY-local weekday

    # Saturday — fully closed
    if wd == 5:
        return False

    # Friday — open until 17:00 NY-local
    if wd == _FRIDAY_CLOSE_WEEKDAY:
        return t.hour < _FRIDAY_CLOSE_HOUR_NY

    # Sunday — open from 17:00 NY-local
    if wd == _SUNDAY_OPEN_WEEKDAY:
        return t.hour >= _SUNDAY_OPEN_HOUR_NY

    # Mon..Thu — fully open
    return True


def next_close(at: datetime | None = None) -> datetime:
    """Next Friday 17:00 NY-local (returned as UTC) ≥ given moment."""
    t_utc = _ensure_utc(at)
    t_ny = t_utc.astimezone(_NY)
    days_ahead = (_FRIDAY_CLOSE_WEEKDAY - t_ny.weekday()) % 7
    target_ny = (t_ny + timedelta(days=days_ahead)).replace(
        hour=_FRIDAY_CLOSE_HOUR_NY, minute=0, second=0, microsecond=0)
    if target_ny <= t_ny:
        target_ny += timedelta(days=7)
    return target_ny.astimezone(timezone.utc)


def next_open(at: datetime | None = None) -> datetime:
    """Next Sunday 17:00 NY-local (returned as UTC) strictly > given moment.
    DST-aware: in May the result is 21:00 UTC, in December 22:00 UTC.
    """
    t_utc = _ensure_utc(at)
    t_ny = t_utc.astimezone(_NY)
    days_ahead = (_SUNDAY_OPEN_WEEKDAY - t_ny.weekday()) % 7
    target_ny = (t_ny + timedelta(days=days_ahead)).replace(
        hour=_SUNDAY_OPEN_HOUR_NY, minute=0, second=0, microsecond=0)
    if target_ny <= t_ny:
        target_ny += timedelta(days=7)
    return target_ny.astimezone(timezone.utc)


def seconds_until_close(at: datetime | None = None) -> int:
    """How many seconds until market closes. 0 if already closed."""
    t = _ensure_utc(at)
    if not is_market_open(t):
        return 0
    return max(0, int((next_close(t) - t).total_seconds()))


def seconds_until_open(at: datetime | None = None) -> int:
    """How many seconds until market opens. 0 if already open."""
    t = _ensure_utc(at)
    if is_market_open(t):
        return 0
    return max(0, int((next_open(t) - t).total_seconds()))


def max_safe_expiry_hours(at: datetime | None = None,
                          min_buffer_minutes: int = 15) -> int:
    """Maximum expiry in WHOLE hours that still settles before market close
    with a safety buffer. Returns 0 if market is closed or closing too soon
    to fit even a 1h trade with the buffer."""
    secs = seconds_until_close(at)
    if secs == 0:
        return 0
    safe_secs = secs - (min_buffer_minutes * 60)
    if safe_secs < 3600:  # less than 1h left
        return 0
    return safe_secs // 3600  # whole hours


def clip_expiry_hours(desired_hours: int,
                      at: datetime | None = None,
                      min_buffer_minutes: int = 15) -> int:
    """Clip desired expiry so the trade settles before market close.

    Returns 0 if no safe expiry is possible (market closed or closing within
    `min_buffer_minutes + 60` minutes). Caller should treat 0 as "skip".
    """
    if desired_hours <= 0:
        return 0
    cap = max_safe_expiry_hours(at, min_buffer_minutes=min_buffer_minutes)
    if cap == 0:
        return 0
    return int(min(desired_hours, cap))


def current_session(at: datetime | None = None) -> str:
    """Return the dominant FX session at given UTC time.

    Asia    : 22:00 — 08:00 UTC (Sydney+Tokyo)
    London  : 08:00 — 13:00 UTC
    Overlap : 13:00 — 17:00 UTC (London + NY)
    NY      : 17:00 — 22:00 UTC

    On Saturday returns "Closed". On Sunday before 22:00 returns "Closed".
    """
    if not is_market_open(at):
        return "Closed"
    t = _ensure_utc(at)
    h = t.hour
    if h < 8 or h >= 22:
        return "Asia"
    if h < 13:
        return "London"
    if h < 17:
        return "Overlap"
    return "NY"


_UTC_PLUS_5 = timezone(timedelta(hours=5))


def _to_utc_plus_5_iso(dt: datetime) -> str:
    """Convert a UTC datetime to UTC+5 ISO string for the user's local view."""
    return dt.astimezone(_UTC_PLUS_5).strftime("%Y-%m-%d %H:%M (UTC+5)")


def _to_ny_iso(dt: datetime) -> str:
    return dt.astimezone(_NY).strftime("%Y-%m-%d %H:%M (NY)")


def market_status(at: datetime | None = None) -> dict:
    """Snapshot for dashboard. DST-aware. Includes UTC+5 user-local view."""
    t = _ensure_utc(at)
    is_open = is_market_open(t)
    if is_open:
        nc = next_close(t)
        secs = int((nc - t).total_seconds())
        return {
            "as_of_utc": t.isoformat(),
            "as_of_utc_plus_5": _to_utc_plus_5_iso(t),
            "is_open": True,
            "status_emoji": "🟢",
            "status_text": "ОТКРЫТ",
            "session": current_session(t),
            "seconds_until_close": secs,
            "seconds_until_open": 0,
            "next_event": "close",
            "next_event_utc": nc.isoformat(),
            "next_event_utc_plus_5": _to_utc_plus_5_iso(nc),
            "next_event_ny": _to_ny_iso(nc),
            "next_event_text_ru": "закроется через",
            "max_safe_expiry_h": max_safe_expiry_hours(t),
        }
    no = next_open(t)
    secs = int((no - t).total_seconds())
    return {
        "as_of_utc": t.isoformat(),
        "as_of_utc_plus_5": _to_utc_plus_5_iso(t),
        "is_open": False,
        "status_emoji": "🔴",
        "status_text": "ЗАКРЫТ",
        "session": "Closed",
        "seconds_until_close": 0,
        "seconds_until_open": secs,
        "next_event": "open",
        "next_event_utc": no.isoformat(),
        "next_event_utc_plus_5": _to_utc_plus_5_iso(no),
        "next_event_ny": _to_ny_iso(no),
        "next_event_text_ru": "откроется через",
        "max_safe_expiry_h": 0,
    }
