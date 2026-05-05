"""CFTC COT release calendar 365d.

Strategy:
- The CFTC publishes the "Traders in Financial Futures" report every Friday
  at 19:30 UTC, with positioning data as-of the previous Tuesday (close).
- We use the existing `teamagent.cot` module to fetch 52 weeks of weekly
  observations, then construct the release timestamp = following Friday 19:30 UTC.
- Each event carries the z-score of leveraged-money net position vs 52w
  history → a strong "extreme positioning → contrarian" signal.

For each currency we emit:
- type: "cot_extreme_long" / "cot_extreme_short" / "cot_release_neutral"
- value: z-score of net leveraged position
- impact: "Red" if |z| ≥ 1.5 else "Yellow"
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import requests

log = logging.getLogger("events.cot")

# CFTC public Socrata API — Traders in Financial Futures (no API key)
# Code list:
# 099741 — EURO FX, 096742 — BRITISH POUND, 097741 — JAPANESE YEN,
# 092741 — SWISS FRANC, 232741 — AUSTRALIAN DOLLAR, 090741 — CANADIAN DOLLAR,
# 112741 — NEW ZEALAND DOLLAR
COT_CONTRACTS = {
    "EUR": "099741",
    "GBP": "096742",
    "JPY": "097741",
    "CHF": "092741",
    "AUD": "232741",
    "CAD": "090741",
    "NZD": "112741",
}

# COT public API
_COT_URL = "https://publicreporting.cftc.gov/resource/gpe5-46if.json"
_TIMEOUT = 30
_SESSION = requests.Session()
_SESSION.headers.update({"User-Agent": "Mozilla/5.0 TeamAgent/1.0"})


def _fetch_cot_weekly(code: str, weeks: int = 60) -> list[dict]:
    """Fetch weekly COT report rows for given contract code (newest first)."""
    params = {
        "$where": f"cftc_contract_market_code='{code}'",
        "$order": "report_date_as_yyyy_mm_dd DESC",
        "$limit": str(weeks),
    }
    try:
        r = _SESSION.get(_COT_URL, params=params, timeout=_TIMEOUT)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        log.warning(f"cot fetch failed {code}: {e}")
        return []


def _to_float(x: str | None) -> float:
    try:
        return float(x or 0)
    except Exception:
        return 0.0


def _release_dt_from_report(report_date_str: str) -> datetime:
    """report_date_as_yyyy_mm_dd is the Tuesday close. Release is following Friday 19:30 UTC."""
    rd = datetime.fromisoformat(report_date_str).replace(tzinfo=timezone.utc)
    # Tuesday is weekday 1; Friday is 4 → +3 days
    rel = rd + timedelta(days=3)
    return rel.replace(hour=19, minute=30)


def all_events(start: datetime, end: datetime) -> list[dict]:
    """Build COT release events for all 7 contract currencies in [start, end]."""
    out: list[dict] = []
    for ccy, code in COT_CONTRACTS.items():
        rows = _fetch_cot_weekly(code, weeks=60)
        if not rows:
            continue
        # Compute z-score of net_lev across the 52w window
        nets: list[float] = []
        parsed: list[dict] = []
        for row in rows:
            try:
                rd = row["report_date_as_yyyy_mm_dd"]
            except KeyError:
                continue
            lev_long = _to_float(row.get("lev_money_positions_long") or row.get("lev_money_positions_long_all"))
            lev_short = _to_float(row.get("lev_money_positions_short") or row.get("lev_money_positions_short_all"))
            oi = _to_float(row.get("open_interest_all"))
            if oi <= 0:
                continue
            net = (lev_long - lev_short) / oi * 100.0
            nets.append(net)
            parsed.append({
                "report_date": rd,
                "net_lev_pct_oi": net,
                "lev_long": lev_long,
                "lev_short": lev_short,
                "oi": oi,
            })
        if len(nets) < 4:
            continue
        # Use rolling 52w window — for simplicity compute mean/std of the full set
        # (newer points dominate a bit but this is fine for attribution scope)
        m = sum(nets) / len(nets)
        var = sum((x - m) ** 2 for x in nets) / len(nets)
        sd = var ** 0.5 if var > 0 else 1.0
        for p in parsed:
            rel = _release_dt_from_report(p["report_date"])
            if not (start <= rel <= end):
                continue
            z = (p["net_lev_pct_oi"] - m) / sd
            if z >= 1.5:
                ev_type = "cot_extreme_long"
                impact = "Red"
            elif z <= -1.5:
                ev_type = "cot_extreme_short"
                impact = "Red"
            else:
                ev_type = "cot_release_neutral"
                impact = "Yellow"
            out.append({
                "ts": rel.isoformat(),
                "currency": ccy,
                "type": ev_type,
                "title": f"COT {ccy} z={z:+.2f}",
                "value": p["net_lev_pct_oi"],
                "z_score": z,
                "impact": impact,
            })
    out.sort(key=lambda e: e["ts"])
    return out
