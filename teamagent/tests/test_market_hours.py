"""Тесты для market_hours.py — проверяем что Forex окно
Sun 22:00 UTC → Fri 22:00 UTC обрабатывается корректно во всех краях."""
from __future__ import annotations
from datetime import datetime, timedelta, timezone

import pytest

from teamagent import market_hours as mh


UTC = timezone.utc


def _t(year: int, month: int, day: int, hour: int = 12, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, 0, tzinfo=UTC)


# ───────── is_market_open ─────────

class TestIsMarketOpen:
    def test_monday_noon_open(self):
        # Monday 4 May 2026
        assert mh.is_market_open(_t(2026, 5, 4, 12, 0)) is True

    def test_thursday_2300_open(self):
        assert mh.is_market_open(_t(2026, 5, 7, 23, 0)) is True

    def test_friday_2159_open(self):
        # Last minute before close
        assert mh.is_market_open(_t(2026, 5, 8, 21, 59)) is True

    def test_friday_2200_closed(self):
        # Exactly at close — closed
        assert mh.is_market_open(_t(2026, 5, 8, 22, 0)) is False

    def test_friday_2300_closed(self):
        assert mh.is_market_open(_t(2026, 5, 8, 23, 0)) is False

    def test_saturday_full_closed(self):
        for h in (0, 6, 12, 18, 23):
            assert mh.is_market_open(_t(2026, 5, 9, h, 0)) is False, f"Sat {h}h"

    def test_sunday_before_open_closed(self):
        # Sunday 10 May 2026, 21:00 UTC (still 1h before open)
        assert mh.is_market_open(_t(2026, 5, 10, 21, 0)) is False

    def test_sunday_2200_open(self):
        # Sunday 10 May 2026, 22:00 UTC — moment of open
        assert mh.is_market_open(_t(2026, 5, 10, 22, 0)) is True

    def test_sunday_2300_open(self):
        assert mh.is_market_open(_t(2026, 5, 10, 23, 0)) is True


# ───────── next_close / next_open ─────────

class TestNextEvents:
    def test_next_close_from_monday(self):
        nc = mh.next_close(_t(2026, 5, 4, 10, 0))
        assert nc == _t(2026, 5, 8, 22, 0)

    def test_next_close_from_friday_morning(self):
        nc = mh.next_close(_t(2026, 5, 8, 10, 0))
        assert nc == _t(2026, 5, 8, 22, 0)

    def test_next_close_from_friday_afternoon(self):
        # After close on Friday — should jump to next Friday
        nc = mh.next_close(_t(2026, 5, 8, 23, 0))
        assert nc == _t(2026, 5, 15, 22, 0)

    def test_next_open_from_saturday(self):
        no = mh.next_open(_t(2026, 5, 9, 10, 0))
        assert no == _t(2026, 5, 10, 22, 0)

    def test_next_open_from_sunday_before(self):
        no = mh.next_open(_t(2026, 5, 10, 20, 0))
        assert no == _t(2026, 5, 10, 22, 0)

    def test_next_open_from_monday(self):
        # Already open — next open is next Sunday
        no = mh.next_open(_t(2026, 5, 4, 10, 0))
        assert no == _t(2026, 5, 10, 22, 0)


# ───────── seconds_until_close / open ─────────

class TestSecondsUntil:
    def test_seconds_until_close_when_open(self):
        # Friday 8 May 2026 21:00 UTC — 1h to close
        secs = mh.seconds_until_close(_t(2026, 5, 8, 21, 0))
        assert secs == 3600

    def test_seconds_until_close_when_closed(self):
        # Saturday — 0
        assert mh.seconds_until_close(_t(2026, 5, 9, 12, 0)) == 0

    def test_seconds_until_open_when_closed(self):
        # Saturday 0:00 UTC → Sunday 22:00 UTC = 46h
        secs = mh.seconds_until_open(_t(2026, 5, 9, 0, 0))
        assert secs == 46 * 3600

    def test_seconds_until_open_when_open(self):
        # Already open — 0
        assert mh.seconds_until_open(_t(2026, 5, 4, 12, 0)) == 0


# ───────── max_safe_expiry_hours ─────────

class TestMaxSafeExpiry:
    def test_full_session_5h(self):
        # Mon 12:00 UTC → Fri 22:00 UTC = ~106h
        h = mh.max_safe_expiry_hours(_t(2026, 5, 4, 12, 0), min_buffer_minutes=15)
        assert h >= 5

    def test_friday_20h_is_safe(self):
        # Fri 20:00 UTC — 2h to close. 2h - 15min = 1h45min → safe = 1h
        h = mh.max_safe_expiry_hours(_t(2026, 5, 8, 20, 0), min_buffer_minutes=15)
        assert h == 1

    def test_friday_2130_unsafe(self):
        # Fri 21:30 UTC — 30 min to close → 0
        h = mh.max_safe_expiry_hours(_t(2026, 5, 8, 21, 30), min_buffer_minutes=15)
        assert h == 0

    def test_when_closed(self):
        h = mh.max_safe_expiry_hours(_t(2026, 5, 9, 12, 0), min_buffer_minutes=15)
        assert h == 0


# ───────── clip_expiry_hours ─────────

class TestClipExpiry:
    def test_keeps_when_safe(self):
        assert mh.clip_expiry_hours(5, _t(2026, 5, 4, 12, 0)) == 5

    def test_clips_to_max(self):
        # Fri 19:00 UTC, 3h to close, want 5h → clipped to 2 (3h - 15min = 2h45min → 2h whole)
        assert mh.clip_expiry_hours(5, _t(2026, 5, 8, 19, 0), 15) == 2

    def test_zero_when_market_closed(self):
        assert mh.clip_expiry_hours(5, _t(2026, 5, 9, 12, 0)) == 0

    def test_zero_when_too_close_to_close(self):
        # Fri 21:30 UTC — 30min to close
        assert mh.clip_expiry_hours(5, _t(2026, 5, 8, 21, 30), 15) == 0


# ───────── current_session ─────────

class TestCurrentSession:
    @pytest.mark.parametrize("hour, expected", [
        (0, "Asia"), (4, "Asia"), (7, "Asia"),
        (8, "London"), (10, "London"), (12, "London"),
        (13, "Overlap"), (15, "Overlap"), (16, "Overlap"),
        (17, "NY"), (20, "NY"), (21, "NY"),
        (22, "Asia"), (23, "Asia"),
    ])
    def test_session_by_hour(self, hour, expected):
        # Mon 4 May 2026 at given hour
        assert mh.current_session(_t(2026, 5, 4, hour, 0)) == expected

    def test_closed_when_market_closed(self):
        assert mh.current_session(_t(2026, 5, 9, 12, 0)) == "Closed"


# ───────── market_status ─────────

class TestMarketStatus:
    def test_open_status_keys(self):
        # Use `at=` kwarg — Mon 12:00 UTC always open
        st = mh.market_status(_t(2026, 5, 4, 12, 0))
        assert st["is_open"] is True
        assert st["status_emoji"] == "🟢"
        assert st["next_event"] == "close"
        assert st["seconds_until_close"] > 0
        assert st["seconds_until_open"] == 0
        assert "max_safe_expiry_h" in st

    def test_closed_status_keys(self):
        st = mh.market_status(_t(2026, 5, 9, 12, 0))
        assert st["is_open"] is False
        assert st["status_emoji"] == "🔴"
        assert st["next_event"] == "open"
        assert st["seconds_until_close"] == 0
        assert st["seconds_until_open"] > 0
        assert st["max_safe_expiry_h"] == 0
