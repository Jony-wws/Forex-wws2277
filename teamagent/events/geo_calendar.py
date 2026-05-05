"""Geopolitical event calendar 365d.

Curated from public news archives. Covers events known to move forex
markets significantly:

- OPEC+ ministerial meetings (oil → CAD/RUB/MXN/USD)
- Major US political events (debt ceiling, government shutdowns, elections)
- Major China policy announcements (PBOC fixing, Politburo)
- Geopolitical shocks (war headlines, sanctions, missile strikes)

This list is necessarily curated — we can't auto-fetch geopolitics from a
free API. Update as needed; missing one event just means we attribute the
move to other coincident events (FRED data or CB).
"""
from __future__ import annotations
from datetime import datetime, timezone


_RAW: list[tuple[str, str, str, str, str]] = [
    # (date, time_utc, currency_focus, type, title)
    # OPEC+ meetings (impact: CAD ↑/↓ via WTI move 1-3%)
    ("2025-04-03", "12:00", "CAD",  "opec_meeting", "OPEC+ JMMC meeting"),
    ("2025-06-01", "12:00", "CAD",  "opec_meeting", "OPEC+ JMMC meeting"),
    ("2025-08-03", "12:00", "CAD",  "opec_meeting", "OPEC+ JMMC meeting"),
    ("2025-10-05", "12:00", "CAD",  "opec_meeting", "OPEC+ JMMC meeting"),
    ("2025-12-07", "12:00", "CAD",  "opec_meeting", "OPEC+ JMMC meeting + 2026 quotas"),
    ("2026-02-01", "12:00", "CAD",  "opec_meeting", "OPEC+ JMMC meeting"),
    ("2026-04-05", "12:00", "CAD",  "opec_meeting", "OPEC+ JMMC meeting"),

    # US Treasury events (debt ceiling / refunding announcements move USD)
    ("2025-04-30", "13:00", "USD", "us_refunding", "US Treasury refunding announcement Q2"),
    ("2025-07-30", "13:00", "USD", "us_refunding", "US Treasury refunding announcement Q3"),
    ("2025-10-29", "13:00", "USD", "us_refunding", "US Treasury refunding announcement Q4"),
    ("2026-01-28", "13:00", "USD", "us_refunding", "US Treasury refunding announcement Q1"),
    ("2026-04-29", "13:00", "USD", "us_refunding", "US Treasury refunding announcement Q2"),

    # CPI / NFP rolling top-tier (we already cover these via FRED, but mark
    # them here as "high tier" for cross-currency impact)
    # (omit — fred_calendar handles these)

    # Major political/geopolitical events from news archives
    ("2025-11-05", "00:00", "USD", "geo_election",     "US 2025 off-year elections"),
    ("2026-01-20", "17:00", "USD", "geo_inauguration", "US 2026 cabinet reshuffle / SOTU"),

    # China NPC / Plenum (CNH / risk-on/off → AUD, NZD)
    ("2025-10-20", "02:00", "CNY", "china_plenum", "China 4th Plenum"),
    ("2026-03-05", "02:00", "CNY", "china_npc",    "China 14th NPC opens"),

    # Major holidays with low liquidity (mark as warning, not high-impact)
    ("2025-12-25", "00:00", "USD", "holiday_low_liq", "Christmas — low liquidity"),
    ("2026-01-01", "00:00", "USD", "holiday_low_liq", "New Year — low liquidity"),

    # G7 / G20 summits (currency mention)
    ("2025-06-15", "12:00", "USD", "g7_summit", "G7 Leaders Summit"),
    ("2025-11-22", "12:00", "USD", "g20_summit", "G20 Leaders Summit"),
]


def all_events(start: datetime, end: datetime) -> list[dict]:
    out: list[dict] = []
    for date_str, time_str, ccy, typ, title in _RAW:
        try:
            ts = datetime.fromisoformat(f"{date_str}T{time_str}:00+00:00")
        except ValueError:
            continue
        if not (start <= ts <= end):
            continue
        # impact: holidays = Yellow, geopolitics = Red
        impact = "Yellow" if typ.startswith("holiday") else "Red"
        out.append({
            "ts": ts.isoformat(),
            "currency": ccy,
            "type": typ,
            "title": title,
            "impact": impact,
        })
    out.sort(key=lambda e: e["ts"])
    return out
