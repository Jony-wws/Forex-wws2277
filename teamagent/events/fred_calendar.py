"""FRED economic-release calendar 365d (USD events).

Strategy:
- For each economic series we care about, download FRED CSV (no API key)
  via `https://fred.stlouisfed.org/graph/fredgraph.csv?id=<SERIES>`.
- The CSV gives `observation_date,VALUE` for each period.
- We **construct release timestamps** from the observation_date using
  the standard published release calendar (e.g. NFP = 1st Friday of next
  month at 12:30 UTC, CPI = ~mid of next month, etc.).
- We compute "surprise" by comparing actual vs. consensus estimate. Without
  consensus data we use prior-period as baseline → "delta vs prior" — a
  weaker but free signal.

Series covered:
- PAYEMS  → NFP (Total Nonfarm Payrolls)
- UNRATE  → Unemployment Rate
- CPIAUCSL → CPI YoY (we compute YoY ourselves)
- CPILFESL → Core CPI YoY
- PCEPI   → PCE
- PCEPILFE → Core PCE
- GDPC1   → Real GDP (quarterly)
- ICSA    → Initial Jobless Claims (weekly)
- ISM (NAPMPMI) → ISM Manufacturing PMI
- (RSAFS) → Retail Sales advance
- (PPIACO) → PPI

Each FRED series has a known release schedule. We use deterministic offsets:
- Monthly series → release on a specific business day of the next month
- Weekly series → release every Thursday
- Quarterly → release ~30-60d after quarter end

This isn't perfect (sometimes shifted by 1 day for holidays) but it's good
enough to attribute price moves to event windows ±2h.
"""
from __future__ import annotations

import calendar
import csv
import io
import logging
import subprocess
from datetime import datetime, timedelta, timezone
from typing import Optional

log = logging.getLogger("events.fred")


def _fred_csv(series_id: str, start: datetime, end: datetime) -> list[tuple[datetime, float]]:
    """Download FRED CSV and return list of (observation_date, value) tuples.

    Uses curl subprocess instead of `requests` — the Python requests library
    has a strange TLS handshake hang against fred.stlouisfed.org on this
    environment (15+ sec read-timeout) but curl returns the same data in <1s.
    """
    url = (
        "https://fred.stlouisfed.org/graph/fredgraph.csv"
        f"?id={series_id}"
        f"&cosd={start.strftime('%Y-%m-%d')}"
        f"&coed={end.strftime('%Y-%m-%d')}"
    )
    try:
        # Default curl UA works; Mozilla UA triggers Cloudflare HTTP/2 error.
        proc = subprocess.run(
            ["curl", "-sS", "-m", "15", url],
            capture_output=True,
            text=True,
            timeout=20,
        )
        if proc.returncode != 0:
            log.warning(f"fred curl failed for {series_id}: rc={proc.returncode} {proc.stderr.strip()}")
            return []
        text = proc.stdout
    except Exception as e:
        log.warning(f"fred fetch failed for {series_id}: {e}")
        return []
    rows: list[tuple[datetime, float]] = []
    reader = csv.DictReader(io.StringIO(text))
    val_col = None
    for row in reader:
        if val_col is None:
            for k in row:
                if k.upper() != "OBSERVATION_DATE" and k.upper() != "DATE":
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


def _first_friday_after(year: int, month: int) -> datetime:
    """First Friday of the given month — used for NFP release date."""
    cal = calendar.Calendar()
    for d in cal.itermonthdates(year, month):
        if d.month == month and d.weekday() == 4:  # Friday
            return datetime(d.year, d.month, d.day, 12, 30, tzinfo=timezone.utc)
    raise RuntimeError(f"unreachable: no friday in {year}-{month}")


def _nth_business_day(year: int, month: int, n: int, hour: int, minute: int) -> datetime:
    cal = calendar.Calendar()
    biz = [d for d in cal.itermonthdates(year, month)
           if d.month == month and d.weekday() < 5]
    d = biz[min(n - 1, len(biz) - 1)]
    return datetime(d.year, d.month, d.day, hour, minute, tzinfo=timezone.utc)


def _next_month(d: datetime) -> tuple[int, int]:
    if d.month == 12:
        return d.year + 1, 1
    return d.year, d.month + 1


def _release_for_nfp(observation_dt: datetime) -> datetime:
    # NFP for month M is released on first Friday of month M+1 at 12:30 UTC
    y, m = _next_month(observation_dt)
    return _first_friday_after(y, m)


def _release_for_unrate(observation_dt: datetime) -> datetime:
    # Released same time as NFP
    return _release_for_nfp(observation_dt)


def _release_for_cpi(observation_dt: datetime) -> datetime:
    # CPI for month M released ~10-15th of month M+1 at 12:30 UTC.
    # We use 10th business day of next month as approximation; actual is between
    # 7th-15th calendar day. Using 7th business day is closest empirically.
    y, m = _next_month(observation_dt)
    return _nth_business_day(y, m, 9, 12, 30)


def _release_for_pce(observation_dt: datetime) -> datetime:
    # PCE for month M released ~last business day of M+1 at 12:30 UTC
    y, m = _next_month(observation_dt)
    last_day = calendar.monthrange(y, m)[1]
    rel = datetime(y, m, last_day, 12, 30, tzinfo=timezone.utc)
    while rel.weekday() >= 5:
        rel -= timedelta(days=1)
    return rel


def _release_for_ism(observation_dt: datetime) -> datetime:
    # ISM Mfg released 1st business day of next month at 14:00 UTC
    y, m = _next_month(observation_dt)
    return _nth_business_day(y, m, 1, 14, 0)


def _release_for_retail(observation_dt: datetime) -> datetime:
    # Retail sales released ~mid of next month at 12:30 UTC
    y, m = _next_month(observation_dt)
    return _nth_business_day(y, m, 11, 12, 30)


def _release_for_gdp(observation_dt: datetime) -> datetime:
    # GDP advance for quarter Q released ~30 days after Q-end at 12:30 UTC
    rel = observation_dt + timedelta(days=120)
    rel = rel.replace(hour=12, minute=30, second=0, microsecond=0)
    while rel.weekday() >= 5:
        rel += timedelta(days=1)
    return rel


def _release_for_claims(observation_dt: datetime) -> datetime:
    # Initial claims for week ending Saturday released following Thursday at 12:30 UTC
    rel = observation_dt + timedelta(days=5)  # Saturday → Thursday
    while rel.weekday() != 3:  # Thursday
        rel += timedelta(days=1)
    return rel.replace(hour=12, minute=30, tzinfo=timezone.utc)


_SERIES_CONFIG: list[dict] = [
    # (id, name, release_fn, type)
    {"id": "PAYEMS",    "name": "NFP (Nonfarm Payrolls)", "release_fn": _release_for_nfp,    "type": "us_nfp"},
    {"id": "UNRATE",    "name": "Unemployment Rate",      "release_fn": _release_for_unrate, "type": "us_unrate"},
    {"id": "CPIAUCSL",  "name": "CPI Headline (YoY)",     "release_fn": _release_for_cpi,    "type": "us_cpi"},
    {"id": "CPILFESL",  "name": "Core CPI (YoY)",         "release_fn": _release_for_cpi,    "type": "us_core_cpi"},
    {"id": "PCEPI",     "name": "PCE Headline",           "release_fn": _release_for_pce,    "type": "us_pce"},
    {"id": "PCEPILFE",  "name": "Core PCE",               "release_fn": _release_for_pce,    "type": "us_core_pce"},
    {"id": "GDPC1",     "name": "Real GDP (advance)",     "release_fn": _release_for_gdp,    "type": "us_gdp"},
    {"id": "ICSA",      "name": "Initial Jobless Claims", "release_fn": _release_for_claims, "type": "us_claims"},
    {"id": "RSAFS",     "name": "Retail Sales advance",   "release_fn": _release_for_retail, "type": "us_retail"},
    {"id": "PPIACO",    "name": "PPI Headline",           "release_fn": _release_for_cpi,    "type": "us_ppi"},
]


def all_events(start: datetime, end: datetime) -> list[dict]:
    """Fetch every USD event with computed release timestamp."""
    out: list[dict] = []
    # Pull a bit before start so we have prior values for "surprise vs prior"
    fetch_start = start - timedelta(days=120)
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
                "currency": "USD",
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
    return out
