"""Non-USD economic events from FRED (OECD MEI series).

Series cover EU/UK/JP/AU/CA/NZ CPI + unemployment. Same FRED CSV mechanic
as `fred_calendar.py`, but with country-specific release-day approximations.

Coverage is essential because the 365-day analysis was USD-heavy (~25% of
events), missing the major macro drivers for EUR/GBP/JPY/AUD/CAD/NZD pairs.
With this module we add ~150 non-USD events across 365 days.

Release-day approximations (all UTC):
- EU CPI flash: ~last working day of month at 09:00 (Eurostat flash)
- UK CPI: ~3rd Wednesday of next month at 06:00 (ONS standard)
- JP CPI: ~last Friday of next month at 23:30 (Statistics Bureau)
- AU CPI Q-rly: ~25th of month after Q-end at 00:30 (ABS)
- CA CPI: ~3rd Tuesday of next month at 12:30 (StatCan)
- NZ CPI Q-rly: ~17th of month after Q-end at 21:45 (Stats NZ)
- Unemployment monthly: similar pattern but offset by 1 week

These are approximate — actual release can shift ±1-2 days for holidays.
For ±2h attribution windows this introduces some misses but adds far more
real matches than zero non-USD coverage.
"""
from __future__ import annotations

import calendar
import csv
import io
import logging
import subprocess
from datetime import datetime, timedelta, timezone
from typing import Optional

log = logging.getLogger("events.fred_global")


def _fred_csv(series_id: str, start: datetime, end: datetime) -> list[tuple[datetime, float]]:
    """Same curl-based fetch as fred_calendar (avoids requests TLS hang)."""
    url = (
        "https://fred.stlouisfed.org/graph/fredgraph.csv"
        f"?id={series_id}"
        f"&cosd={start.strftime('%Y-%m-%d')}"
        f"&coed={end.strftime('%Y-%m-%d')}"
    )
    try:
        proc = subprocess.run(
            ["curl", "-sS", "-m", "15", url],
            capture_output=True, text=True, timeout=20,
        )
        if proc.returncode != 0:
            log.warning(f"fred_global curl failed for {series_id}: rc={proc.returncode}")
            return []
        text = proc.stdout
    except Exception as e:
        log.warning(f"fred_global fetch failed for {series_id}: {e}")
        return []
    rows: list[tuple[datetime, float]] = []
    reader = csv.DictReader(io.StringIO(text))
    val_col = None
    for row in reader:
        if val_col is None:
            for k in row:
                if k.upper() not in ("OBSERVATION_DATE", "DATE"):
                    val_col = k
                    break
        if val_col is None:
            continue
        date_str = row.get("observation_date") or row.get("DATE") or row.get("date")
        if not date_str:
            continue
        try:
            obs_dt = datetime.fromisoformat(date_str).replace(tzinfo=timezone.utc)
        except Exception:
            continue
        v = row.get(val_col, "").strip()
        if not v or v == ".":
            continue
        try:
            rows.append((obs_dt, float(v)))
        except ValueError:
            continue
    return rows


def _next_month(d: datetime) -> tuple[int, int]:
    return (d.year + 1, 1) if d.month == 12 else (d.year, d.month + 1)


def _nth_weekday_of_month(year: int, month: int, weekday: int, n: int) -> datetime:
    """N-th occurrence of weekday in given month (weekday: Mon=0, Sun=6)."""
    cal = calendar.Calendar()
    matches = [d for d in cal.itermonthdates(year, month) if d.month == month and d.weekday() == weekday]
    if n - 1 < len(matches):
        d = matches[n - 1]
    else:
        d = matches[-1]
    return datetime(d.year, d.month, d.day, tzinfo=timezone.utc)


def _last_weekday_of_month(year: int, month: int, weekday: int) -> datetime:
    cal = calendar.Calendar()
    matches = [d for d in cal.itermonthdates(year, month) if d.month == month and d.weekday() == weekday]
    d = matches[-1]
    return datetime(d.year, d.month, d.day, tzinfo=timezone.utc)


def _last_business_day(year: int, month: int) -> datetime:
    last_day = calendar.monthrange(year, month)[1]
    rel = datetime(year, month, last_day, tzinfo=timezone.utc)
    while rel.weekday() >= 5:
        rel -= timedelta(days=1)
    return rel


def _eu_cpi_release(observation_dt: datetime) -> datetime:
    """EU flash CPI ~last business day of next month at 09:00 UTC."""
    y, m = _next_month(observation_dt)
    base = _last_business_day(y, m)
    return base.replace(hour=9, minute=0)


def _uk_cpi_release(observation_dt: datetime) -> datetime:
    """UK CPI ~3rd Wednesday of next month at 06:00 UTC."""
    y, m = _next_month(observation_dt)
    base = _nth_weekday_of_month(y, m, 2, 3)  # Wed=2
    return base.replace(hour=6, minute=0)


def _jp_cpi_release(observation_dt: datetime) -> datetime:
    """JP CPI ~last Friday of next month at 23:30 UTC (Tokyo Fri 8:30)."""
    y, m = _next_month(observation_dt)
    base = _last_weekday_of_month(y, m, 4)  # Fri=4
    return base.replace(hour=23, minute=30)


def _au_cpi_release(observation_dt: datetime) -> datetime:
    """AU CPI quarterly: ~last Wednesday of month after Q-end at 00:30 UTC."""
    y, m = _next_month(observation_dt)
    base = _last_weekday_of_month(y, m, 2)  # Wed
    return base.replace(hour=0, minute=30)


def _ca_cpi_release(observation_dt: datetime) -> datetime:
    """CA CPI ~3rd Tuesday of next month at 12:30 UTC."""
    y, m = _next_month(observation_dt)
    base = _nth_weekday_of_month(y, m, 1, 3)  # Tue
    return base.replace(hour=12, minute=30)


def _nz_cpi_release(observation_dt: datetime) -> datetime:
    """NZ CPI quarterly: ~3rd Wednesday of month after Q-end at 21:45 UTC."""
    y, m = _next_month(observation_dt)
    base = _nth_weekday_of_month(y, m, 2, 3)
    return base.replace(hour=21, minute=45)


def _eu_unemp_release(observation_dt: datetime) -> datetime:
    """EU unemployment ~last business day of next month at 09:00 UTC."""
    y, m = _next_month(observation_dt)
    return _last_business_day(y, m).replace(hour=9, minute=0)


def _uk_unemp_release(observation_dt: datetime) -> datetime:
    """UK unemployment ~3rd Tuesday of next month at 06:00 UTC."""
    y, m = _next_month(observation_dt)
    base = _nth_weekday_of_month(y, m, 1, 3)
    return base.replace(hour=6, minute=0)


def _jp_unemp_release(observation_dt: datetime) -> datetime:
    """JP unemployment ~last Tuesday of next month at 23:30 UTC."""
    y, m = _next_month(observation_dt)
    base = _last_weekday_of_month(y, m, 1)
    return base.replace(hour=23, minute=30)


def _au_unemp_release(observation_dt: datetime) -> datetime:
    """AU employment monthly ~3rd Thursday of next month at 00:30 UTC."""
    y, m = _next_month(observation_dt)
    base = _nth_weekday_of_month(y, m, 3, 3)  # Thu
    return base.replace(hour=0, minute=30)


def _ca_unemp_release(observation_dt: datetime) -> datetime:
    """CA Labour Force Survey: 1st Friday of next month at 12:30 UTC."""
    y, m = _next_month(observation_dt)
    base = _nth_weekday_of_month(y, m, 4, 1)  # Fri
    return base.replace(hour=12, minute=30)


def _nz_unemp_release(observation_dt: datetime) -> datetime:
    """NZ Household Labour Force Q-rly: ~1st Wednesday of month after Q+1 at 21:45 UTC."""
    y, m = _next_month(observation_dt)
    base = _nth_weekday_of_month(y, m, 2, 1)
    return base.replace(hour=21, minute=45)


_SERIES_CONFIG: list[dict] = [
    # CPI series
    {"id": "CP0000EZ19M086NEST", "currency": "EUR", "type": "eu_cpi",   "name": "Eurozone HICP", "release_fn": _eu_cpi_release},
    {"id": "GBRCPIALLMINMEI",    "currency": "GBP", "type": "uk_cpi",   "name": "UK CPI",         "release_fn": _uk_cpi_release},
    {"id": "JPNCPIALLMINMEI",    "currency": "JPY", "type": "jp_cpi",   "name": "Japan CPI",      "release_fn": _jp_cpi_release},
    {"id": "AUSCPIALLQINMEI",    "currency": "AUD", "type": "au_cpi",   "name": "Australia CPI",  "release_fn": _au_cpi_release},
    {"id": "CANCPIALLMINMEI",    "currency": "CAD", "type": "ca_cpi",   "name": "Canada CPI",     "release_fn": _ca_cpi_release},
    {"id": "NZLCPIALLQINMEI",    "currency": "NZD", "type": "nz_cpi",   "name": "NZ CPI",         "release_fn": _nz_cpi_release},
    # Unemployment / employment series
    {"id": "LRHUTTTTEZM156S",    "currency": "EUR", "type": "eu_unemp", "name": "Eurozone Unemployment", "release_fn": _eu_unemp_release},
    {"id": "LMUNRRTTGBM156S",    "currency": "GBP", "type": "uk_unemp", "name": "UK Unemployment",       "release_fn": _uk_unemp_release},
    {"id": "LRHUTTTTJPM156S",    "currency": "JPY", "type": "jp_unemp", "name": "Japan Unemployment",    "release_fn": _jp_unemp_release},
    {"id": "LRHUTTTTAUM156S",    "currency": "AUD", "type": "au_unemp", "name": "Australia Unemployment","release_fn": _au_unemp_release},
    {"id": "LRHUTTTTCAM156S",    "currency": "CAD", "type": "ca_unemp", "name": "Canada Unemployment",   "release_fn": _ca_unemp_release},
    {"id": "LRHUTTTTNZM156S",    "currency": "NZD", "type": "nz_unemp", "name": "NZ Unemployment",       "release_fn": _nz_unemp_release},
]


def all_events(start: datetime, end: datetime) -> list[dict]:
    out: list[dict] = []
    fetch_start = start - timedelta(days=180)
    for cfg in _SERIES_CONFIG:
        rows = _fred_csv(cfg["id"], fetch_start, end)
        if not rows:
            continue
        rows.sort(key=lambda r: r[0])
        prev_val: Optional[float] = None
        for obs_dt, val in rows:
            try:
                rel_dt = cfg["release_fn"](obs_dt)
            except Exception:
                continue
            if not (start <= rel_dt <= end):
                prev_val = val
                continue
            delta = (val - prev_val) if prev_val is not None else None
            out.append({
                "ts": rel_dt.isoformat(),
                "currency": cfg["currency"],
                "type": cfg["type"],
                "title": cfg["name"],
                "value": val,
                "prev": prev_val,
                "delta_vs_prev": delta,
                "impact": "Red",
                "series_id": cfg["id"],
                "observation_date": obs_dt.date().isoformat(),
            })
            prev_val = val
    out.sort(key=lambda e: e["ts"])
    log.info(f"fred_global: {len(out)} events across {len({e['type'] for e in out})} series-types")
    return out
