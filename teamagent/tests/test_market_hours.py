"""Тесты для market_hours.py — проверяем что Forex-неделя
Sun 17:00 NY-local → Fri 17:00 NY-local обрабатывается корректно во всех краях,
включая DST переход (лето: 21:00 UTC, зима: 22:00 UTC)."""
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

    def test_friday_summer_2059_open(self):
        # 8 May 2026 — EDT, close = 21:00 UTC. Last minute before close.
        assert mh.is_market_open(_t(2026, 5, 8, 20, 59)) is True

    def test_friday_summer_2100_closed(self):
        # EDT: 21:00 UTC = 17:00 NY — close moment
        assert mh.is_market_open(_t(2026, 5, 8, 21, 0)) is False

    def test_friday_summer_2300_closed(self):
        assert mh.is_market_open(_t(2026, 5, 8, 23, 0)) is False

    def test_friday_winter_2200_closed(self):
        # 11 Dec 2026 — EST, close = 22:00 UTC = 17:00 NY
        assert mh.is_market_open(_t(2026, 12, 11, 22, 0)) is False

    def test_friday_winter_2159_open(self):
        # EST: still open at 21:59 UTC
        assert mh.is_market_open(_t(2026, 12, 11, 21, 59)) is True

    def test_saturday_full_closed(self):
        for h in (0, 6, 12, 18, 23):
            assert mh.is_market_open(_t(2026, 5, 9, h, 0)) is False, f"Sat {h}h"

    def test_sunday_summer_before_open_closed(self):
        # 10 May 2026 — EDT, open = 21:00 UTC. At 20:00 UTC still closed.
        assert mh.is_market_open(_t(2026, 5, 10, 20, 0)) is False

    def test_sunday_summer_2100_open(self):
        # 10 May 2026 21:00 UTC = 17:00 NY EDT — moment of open
        assert mh.is_market_open(_t(2026, 5, 10, 21, 0)) is True

    def test_sunday_summer_2300_open(self):
        assert mh.is_market_open(_t(2026, 5, 10, 23, 0)) is True

    def test_sunday_winter_2200_open(self):
        # 13 Dec 2026 — EST, open = 22:00 UTC = 17:00 NY EST
        assert mh.is_market_open(_t(2026, 12, 13, 22, 0)) is True

    def test_sunday_winter_2159_closed(self):
        # EST: still closed at 21:59 UTC
        assert mh.is_market_open(_t(2026, 12, 13, 21, 59)) is False

    def test_user_complaint_2026_05_03_2120_utc_open(self):
        # 2026-05-03 21:20 UTC — the exact moment the user reported the bug:
        # «Система написал что рынок закрыт … но рынок открыт».
        # Sunday EDT, 17:20 NY-local → must be OPEN.
        assert mh.is_market_open(_t(2026, 5, 3, 21, 20)) is True

    def test_user_complaint_status_emoji_green(self):
        st = mh.market_status(_t(2026, 5, 3, 21, 20))
        assert st["is_open"] is True
        assert st["status_emoji"] == "🟢"
        assert st["next_event"] == "close"


# ───────── next_close / next_open ─────────

class TestNextEvents:
    def test_next_close_summer_from_monday(self):
        # Mon 4 May (EDT) → Fri 8 May 21:00 UTC
        nc = mh.next_close(_t(2026, 5, 4, 10, 0))
        assert nc == _t(2026, 5, 8, 21, 0)

    def test_next_close_summer_from_friday_morning(self):
        nc = mh.next_close(_t(2026, 5, 8, 10, 0))
        assert nc == _t(2026, 5, 8, 21, 0)

    def test_next_close_summer_from_friday_afternoon(self):
        # After close on Friday EDT — jump to next Friday EDT
        nc = mh.next_close(_t(2026, 5, 8, 22, 0))
        assert nc == _t(2026, 5, 15, 21, 0)

    def test_next_open_summer_from_saturday(self):
        # Sat 9 May (EDT) → Sun 10 May 21:00 UTC
        no = mh.next_open(_t(2026, 5, 9, 10, 0))
        assert no == _t(2026, 5, 10, 21, 0)

    def test_next_open_summer_from_sunday_before(self):
        no = mh.next_open(_t(2026, 5, 10, 19, 0))
        assert no == _t(2026, 5, 10, 21, 0)

    def test_next_open_summer_from_monday(self):
        # Already open — next open is next Sunday
        no = mh.next_open(_t(2026, 5, 4, 10, 0))
        assert no == _t(2026, 5, 10, 21, 0)

    def test_next_close_winter(self):
        # Mon 7 Dec (EST) → Fri 11 Dec 22:00 UTC
        nc = mh.next_close(_t(2026, 12, 7, 10, 0))
        assert nc == _t(2026, 12, 11, 22, 0)

    def test_next_open_winter(self):
        no = mh.next_open(_t(2026, 12, 12, 10, 0))
        assert no == _t(2026, 12, 13, 22, 0)


# ───────── seconds_until_close / open ─────────

class TestSecondsUntil:
    def test_seconds_until_close_when_open_summer(self):
        # Fri 8 May 2026 20:00 UTC (EDT, close 21:00) — 1h to close
        secs = mh.seconds_until_close(_t(2026, 5, 8, 20, 0))
        assert secs == 3600

    def test_seconds_until_close_when_closed(self):
        # Saturday — 0
        assert mh.seconds_until_close(_t(2026, 5, 9, 12, 0)) == 0

    def test_seconds_until_open_when_closed_summer(self):
        # Sat 9 May 0:00 UTC → Sun 10 May 21:00 UTC (EDT) = 45h
        secs = mh.seconds_until_open(_t(2026, 5, 9, 0, 0))
        assert secs == 45 * 3600

    def test_seconds_until_open_when_open(self):
        # Already open — 0
        assert mh.seconds_until_open(_t(2026, 5, 4, 12, 0)) == 0


# ───────── max_safe_expiry_hours ─────────

class TestMaxSafeExpiry:
    def test_full_session_5h(self):
        # Mon 12:00 UTC → Fri 21:00 UTC EDT = ~105h
        h = mh.max_safe_expiry_hours(_t(2026, 5, 4, 12, 0), min_buffer_minutes=15)
        assert h >= 5

    def test_friday_summer_19h_is_safe(self):
        # Fri 19:00 UTC EDT — 2h to close. 2h - 15min = 1h45min → safe = 1h
        h = mh.max_safe_expiry_hours(_t(2026, 5, 8, 19, 0), min_buffer_minutes=15)
        assert h == 1

    def test_friday_summer_2030_unsafe(self):
        # Fri 20:30 UTC EDT — 30 min to close → 0
        h = mh.max_safe_expiry_hours(_t(2026, 5, 8, 20, 30), min_buffer_minutes=15)
        assert h == 0

    def test_when_closed(self):
        h = mh.max_safe_expiry_hours(_t(2026, 5, 9, 12, 0), min_buffer_minutes=15)
        assert h == 0


# ───────── clip_expiry_hours ─────────

class TestClipExpiry:
    def test_keeps_when_safe(self):
        assert mh.clip_expiry_hours(5, _t(2026, 5, 4, 12, 0)) == 5

    def test_clips_to_max_summer(self):
        # Fri 18:00 UTC EDT, 3h to close, want 5h → clipped to 2
        assert mh.clip_expiry_hours(5, _t(2026, 5, 8, 18, 0), 15) == 2

    def test_zero_when_market_closed(self):
        assert mh.clip_expiry_hours(5, _t(2026, 5, 9, 12, 0)) == 0

    def test_zero_when_too_close_to_close_summer(self):
        # Fri 20:30 UTC EDT — 30min to close
        assert mh.clip_expiry_hours(5, _t(2026, 5, 8, 20, 30), 15) == 0


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
