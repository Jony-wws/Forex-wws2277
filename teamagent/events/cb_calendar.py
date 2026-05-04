"""Central-bank meeting calendar — programmatic + curated.

Covers 8 major central banks for the past 365 days (rolling). Each entry has:
- bank: Fed / ECB / BoE / BoJ / RBA / RBNZ / BoC / SNB
- currency: USD / EUR / GBP / JPY / AUD / NZD / CAD / CHF
- date_utc: ISO datetime of rate decision
- presser_utc: ISO datetime of press conference (None if no presser)
- type: "rate_decision" / "minutes" / "speech_governor"

Times are wall-clock UTC of the published decision. We hard-code the actual
recent schedule (Sep 2025 → May 2026) because it's small (~70 events) and
having it inline removes any external dependency.

If you re-run this on a different VM date, expand `_RAW` with new entries.
The full official calendars are public:
- Fed: https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm
- ECB: https://www.ecb.europa.eu/press/calendars/mgcgc/html/index.en.html
- BoE: https://www.bankofengland.co.uk/monetary-policy/upcoming-mpc-dates
- BoJ: https://www.boj.or.jp/en/mopo/mpmsche_minu/index.htm
- RBA: https://www.rba.gov.au/schedules-events/calendar.html
- RBNZ: https://www.rbnz.govt.nz/monetary-policy/about-monetary-policy/monetary-policy-meeting-calendar
- BoC: https://www.bankofcanada.ca/2025/09/2026-schedule-for-bank-of-canada-policy-interest-rate-announcements/
- SNB: https://www.snb.ch/en/iabout/monpol/id/monpol_current
"""
from __future__ import annotations
from datetime import datetime, timezone
from typing import Iterable

# Manually curated schedule. (date, time_utc, bank, presser_offset_min)
# presser_offset = None → no separate press conference.
_RAW: list[tuple[str, str, str, int | None]] = [
    # ========= 2025 =========
    # Fed FOMC (8 meetings/year, decision 18:00 UTC + presser 18:30)
    ("2025-04-30", "18:00", "Fed",  30),
    ("2025-06-18", "18:00", "Fed",  30),
    ("2025-07-30", "18:00", "Fed",  30),
    ("2025-09-17", "18:00", "Fed",  30),
    ("2025-10-29", "18:00", "Fed",  30),
    ("2025-12-10", "19:00", "Fed",  30),  # winter UTC

    # ECB (8/year, decision 12:15 UTC, presser 12:45)
    ("2025-04-17", "12:15", "ECB",  30),
    ("2025-06-05", "12:15", "ECB",  30),
    ("2025-07-24", "12:15", "ECB",  30),
    ("2025-09-11", "12:15", "ECB",  30),
    ("2025-10-30", "13:15", "ECB",  30),  # post-DST
    ("2025-12-18", "13:15", "ECB",  30),

    # BoE (8/year, decision 11:00 UTC summer / 12:00 winter)
    ("2025-05-08", "11:00", "BoE",  None),
    ("2025-06-19", "11:00", "BoE",  None),
    ("2025-08-07", "11:00", "BoE",  None),
    ("2025-09-18", "11:00", "BoE",  None),
    ("2025-11-06", "12:00", "BoE",  None),  # winter
    ("2025-12-18", "12:00", "BoE",  None),

    # BoJ (8/year, decision unscheduled time ~03:00 UTC)
    ("2025-05-01", "03:30", "BoJ",  None),
    ("2025-06-17", "03:30", "BoJ",  None),
    ("2025-07-31", "03:30", "BoJ",  None),
    ("2025-09-19", "03:30", "BoJ",  None),
    ("2025-10-30", "03:30", "BoJ",  None),
    ("2025-12-19", "03:30", "BoJ",  None),

    # RBA (Mtg ~11/year, decision 03:30 UTC summer / 03:30 UTC winter)
    ("2025-05-20", "04:30", "RBA",  None),
    ("2025-07-08", "04:30", "RBA",  None),
    ("2025-08-12", "04:30", "RBA",  None),
    ("2025-09-30", "04:30", "RBA",  None),
    ("2025-11-04", "03:30", "RBA",  None),
    ("2025-12-09", "03:30", "RBA",  None),

    # RBNZ (7/year, decision 02:00 UTC NZST = 01:00 UTC NZDT)
    ("2025-04-09", "02:00", "RBNZ", None),
    ("2025-05-28", "02:00", "RBNZ", None),
    ("2025-07-09", "02:00", "RBNZ", None),
    ("2025-08-20", "02:00", "RBNZ", None),
    ("2025-10-08", "01:00", "RBNZ", None),
    ("2025-11-26", "01:00", "RBNZ", None),

    # BoC (8/year, decision 14:00 UTC)
    ("2025-04-16", "13:45", "BoC",  None),
    ("2025-06-04", "13:45", "BoC",  None),
    ("2025-07-30", "13:45", "BoC",  None),
    ("2025-09-17", "13:45", "BoC",  None),
    ("2025-10-29", "13:45", "BoC",  None),
    ("2025-12-10", "14:45", "BoC",  None),

    # SNB (Quarterly, decision 07:30 UTC summer / 08:30 winter)
    ("2025-06-19", "07:30", "SNB",  None),
    ("2025-09-25", "07:30", "SNB",  None),
    ("2025-12-11", "08:30", "SNB",  None),

    # ========= 2026 (YTD) =========
    ("2026-01-28", "19:00", "Fed",  30),
    ("2026-03-18", "18:00", "Fed",  30),
    ("2026-04-29", "18:00", "Fed",  30),

    ("2026-01-29", "13:15", "ECB",  30),
    ("2026-03-12", "13:15", "ECB",  30),
    ("2026-04-23", "12:15", "ECB",  30),

    ("2026-02-05", "12:00", "BoE",  None),
    ("2026-03-19", "12:00", "BoE",  None),
    ("2026-05-07", "11:00", "BoE",  None),

    ("2026-01-23", "03:30", "BoJ",  None),
    ("2026-03-19", "03:30", "BoJ",  None),
    ("2026-04-28", "03:30", "BoJ",  None),

    ("2026-02-10", "03:30", "RBA",  None),
    ("2026-03-31", "04:30", "RBA",  None),
    ("2026-04-29", "04:30", "RBA",  None),

    ("2026-02-18", "01:00", "RBNZ", None),
    ("2026-04-08", "02:00", "RBNZ", None),

    ("2026-01-21", "14:45", "BoC",  None),
    ("2026-03-04", "14:45", "BoC",  None),
    ("2026-04-15", "13:45", "BoC",  None),

    ("2026-03-19", "08:30", "SNB",  None),
]


_BANK_TO_CCY = {
    "Fed": "USD", "ECB": "EUR", "BoE": "GBP", "BoJ": "JPY",
    "RBA": "AUD", "RBNZ": "NZD", "BoC": "CAD", "SNB": "CHF",
}


def all_events(start: datetime, end: datetime) -> list[dict]:
    """Return every CB event in [start, end] as flat dict list."""
    out: list[dict] = []
    for date_str, time_str, bank, presser_off in _RAW:
        dec_str = f"{date_str}T{time_str}:00+00:00"
        try:
            dec = datetime.fromisoformat(dec_str)
        except ValueError:
            continue
        if not (start <= dec <= end):
            continue
        ccy = _BANK_TO_CCY[bank]
        out.append({
            "ts": dec.isoformat(),
            "currency": ccy,
            "bank": bank,
            "type": "cb_rate_decision",
            "title": f"{bank} rate decision",
            "impact": "Red",
        })
        if presser_off is not None:
            from datetime import timedelta
            ps = dec + timedelta(minutes=presser_off)
            out.append({
                "ts": ps.isoformat(),
                "currency": ccy,
                "bank": bank,
                "type": "cb_press_conference",
                "title": f"{bank} press conference",
                "impact": "Red",
            })
    out.sort(key=lambda e: e["ts"])
    return out


def currencies() -> Iterable[str]:
    return sorted(set(_BANK_TO_CCY.values()))
