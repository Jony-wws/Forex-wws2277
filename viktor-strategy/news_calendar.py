"""Approximate red-news calendar (UTC) for Jun 2025 - Jun 2026.
NFP: first Friday 12:30 UTC (summer DST) / 13:30 (winter).
CPI: typical release days 12:30/13:30 UTC (approx mid-month).
FOMC: 19:00 UTC winter / 18:00 summer. ECB: 13:15 UTC press conf 13:45.
"""
import pandas as pd

def first_friday(y, m):
    d = pd.Timestamp(year=y, month=m, day=1)
    return d + pd.Timedelta(days=(4 - d.weekday()) % 7)

def is_dst(ts):  # US DST approx: 2nd Sunday Mar - 1st Sunday Nov
    return ts.month in (4,5,6,7,8,9,10) or (ts.month==3 and ts.day>=9) or (ts.month==11 and ts.day<=1)

FOMC = ["2025-06-18","2025-07-30","2025-09-17","2025-10-29","2025-12-10",
        "2026-01-28","2026-03-18","2026-04-29","2026-06-17"]
ECB  = ["2025-06-05","2025-07-24","2025-09-11","2025-10-30","2025-12-18",
        "2026-02-05","2026-03-19","2026-04-30","2026-06-11"]
CPI_DAYS = {  # approx US CPI release dates
    "2025-06":11,"2025-07":15,"2025-08":12,"2025-09":11,"2025-10":15,"2025-11":13,"2025-12":10,
    "2026-01":13,"2026-02":11,"2026-03":11,"2026-04":10,"2026-05":12,"2026-06":10}

def red_events(start="2025-06-01", end="2026-06-30"):
    evs = []
    rng = pd.period_range(start[:7], end[:7], freq="M")
    for p in rng:
        y, m = p.year, p.month
        ff = first_friday(y, m)
        evs.append(ff + pd.Timedelta(hours=12 if is_dst(ff) else 13, minutes=30))     # NFP
        cd = CPI_DAYS.get(f"{y}-{m:02d}")
        if cd:
            ts = pd.Timestamp(year=y, month=m, day=cd)
            evs.append(ts + pd.Timedelta(hours=12 if is_dst(ts) else 13, minutes=30)) # CPI
    for d in FOMC:
        ts = pd.Timestamp(d); evs.append(ts + pd.Timedelta(hours=18 if is_dst(ts) else 19))
    for d in ECB:
        ts = pd.Timestamp(d); evs.append(ts + pd.Timedelta(hours=13, minutes=15))
    return sorted(pd.Timestamp(e, tz="UTC") for e in evs)

if __name__ == "__main__":
    evs = red_events()
    print(len(evs), "events"); [print(e) for e in evs[:8]]
