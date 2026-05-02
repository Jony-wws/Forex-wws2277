"""
Forex market hours module.

Forex trades 24/5 — opens Sunday 22:00 UTC (Sydney session) and closes
Friday 22:00 UTC (NY session close). All times are computed in **UTC**;
the dashboard renders them in UTC and UTC+5 for the user.

This module is intentionally dependency-free (only `datetime`) so it loads
fast and can be imported by every trader / dashboard endpoint without
adding latency.

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

Convention used here (UTC):
  - Open  = Sunday 22:00:00 UTC
  - Close = Friday 22:00:00 UTC
This matches the standard FX wholesale week.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

# Friday close: weekday=4 (Mon=0..Sun=6), hour=22:00 UTC
_FRIDAY_CLOSE_WEEKDAY = 4
_FRIDAY_CLOSE_HOUR = 22

# Sunday open: weekday=6, hour=22:00 UTC
_SUNDAY_OPEN_WEEKDAY = 6
_SUNDAY_OPEN_HOUR = 22


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _ensure_utc(dt: datetime | None) -> datetime:
    if dt is None:
        return _utcnow()
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def is_market_open(at: datetime | None = None) -> bool:
    """True if Forex is open at the given UTC time.

    Open: Sunday 22:00 UTC → Friday 22:00 UTC.
    Closed: Friday 22:00 UTC → Sunday 22:00 UTC.
    """
    t = _ensure_utc(at)
    wd = t.weekday()  # Mon=0..Sun=6

    # Saturday — fully closed
    if wd == 5:
        return False

    # Friday — open until 22:00 UTC
    if wd == _FRIDAY_CLOSE_WEEKDAY:
        return t.hour < _FRIDAY_CLOSE_HOUR

    # Sunday — open from 22:00 UTC
    if wd == _SUNDAY_OPEN_WEEKDAY:
        return t.hour >= _SUNDAY_OPEN_HOUR

    # Mon..Thu — fully open
    return True


def next_close(at: datetime | None = None) -> datetime:
    """Next Friday 22:00 UTC ≥ given moment."""
    t = _ensure_utc(at)
    # Days to Friday from current weekday
    days_ahead = (_FRIDAY_CLOSE_WEEKDAY - t.weekday()) % 7
    target = (t + timedelta(days=days_ahead)).replace(
        hour=_FRIDAY_CLOSE_HOUR, minute=0, second=0, microsecond=0)
    if target <= t:
        target += timedelta(days=7)
    return target


def next_open(at: datetime | None = None) -> datetime:
    """Next Sunday 22:00 UTC ≥ given moment.
    If market is currently open, returns the *previous* open boundary's
    next instance (i.e. next Sunday 22:00 strictly in the future).
    """
    t = _ensure_utc(at)
    days_ahead = (_SUNDAY_OPEN_WEEKDAY - t.weekday()) % 7
    target = (t + timedelta(days=days_ahead)).replace(
        hour=_SUNDAY_OPEN_HOUR, minute=0, second=0, microsecond=0)
    if target <= t:
        target += timedelta(days=7)
    return target


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


def market_status(at: datetime | None = None) -> dict:
    """Snapshot for dashboard."""
    t = _ensure_utc(at)
    is_open = is_market_open(t)
    if is_open:
        nc = next_close(t)
        secs = int((nc - t).total_seconds())
        return {
            "as_of_utc": t.isoformat(),
            "is_open": True,
            "status_emoji": "🟢",
            "status_text": "ОТКРЫТ",
            "session": current_session(t),
            "seconds_until_close": secs,
            "seconds_until_open": 0,
            "next_event": "close",
            "next_event_utc": nc.isoformat(),
            "next_event_text_ru": "закроется через",
            "max_safe_expiry_h": max_safe_expiry_hours(t),
        }
    no = next_open(t)
    secs = int((no - t).total_seconds())
    return {
        "as_of_utc": t.isoformat(),
        "is_open": False,
        "status_emoji": "🔴",
        "status_text": "ЗАКРЫТ",
        "session": "Closed",
        "seconds_until_close": 0,
        "seconds_until_open": secs,
        "next_event": "open",
        "next_event_utc": no.isoformat(),
        "next_event_text_ru": "откроется через",
        "max_safe_expiry_h": 0,
    }
