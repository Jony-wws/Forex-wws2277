"""Fundamental data fetcher: 10Y bond yields, policy rates, CPI per currency
from FRED (https://fred.stlouisfed.org) CSV download — no API key required.

Why these signals:
- **10Y yield differential** → main driver of FX over weeks-months. Higher real
  yield = currency stronger. Used by every macro hedge fund.
- **Policy rate differential** → fast-moving, drives short-term carry trades.
- **CPI YoY** → indicates whether central bank is forced to hike (strengthens
  currency) or can cut (weakens). 1-12 week horizon.

These are FREE government data feeds; we cache CSV daily so we don't hammer
FRED. Per-currency macro snapshot lives in `state/fundamentals.json`.

Used by analyzer agents (see agents/analyzers/builtin.py:
FundamentalRateDifferential, FundamentalYieldDifferential, FundamentalCPISurprise)
to nudge the forecast probability of each pair.

Honest scope: this is structural macro tilt (not real-time news / sentiment /
order flow). To add real-time news sentiment we'd need an LLM API key.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import urllib.error
import urllib.request

from . import config

log = logging.getLogger("fundamentals")

# Per-currency FRED series IDs.
# - 10y_yield: long-term government bond yield (decimal %, e.g. 4.5 = 4.5%).
# - policy_rate: central bank short rate or comparable 3m money rate.
# - cpi: CPI level (we compute YoY% from the series).
#
# Some series are monthly (M), some daily (D), some quarterly (Q). We always
# parse and take the latest non-empty observation.
FRED_SERIES: dict[str, dict[str, str]] = {
    "USD": {
        "10y_yield":   "DGS10",                    # daily
        "policy_rate": "DFF",                      # daily, Fed Funds Effective
        "cpi":         "CPIAUCSL",                 # monthly
    },
    "EUR": {
        "10y_yield":   "IRLTLT01EZM156N",          # monthly (Eurozone)
        "policy_rate": "IR3TIB01EZM156N",          # monthly 3m rate
        "cpi":         "CP0000EZ19M086NEST",       # monthly HICP
    },
    "GBP": {
        "10y_yield":   "IRLTLT01GBM156N",          # monthly UK
        "policy_rate": "IR3TIB01GBM156N",          # monthly 3m
        "cpi":         "GBRCPIALLMINMEI",          # monthly
    },
    "JPY": {
        "10y_yield":   "IRLTLT01JPM156N",
        "policy_rate": "IR3TIB01JPM156N",
        "cpi":         "JPNCPIALLMINMEI",
    },
    "CHF": {
        "10y_yield":   "IRLTLT01CHM156N",
        "policy_rate": "IR3TIB01CHM156N",
        "cpi":         "CHECPIALLMINMEI",
    },
    "AUD": {
        "10y_yield":   "IRLTLT01AUM156N",
        "policy_rate": "IR3TIB01AUM156N",
        "cpi":         "AUSCPIALLQINMEI",          # quarterly
    },
    "CAD": {
        "10y_yield":   "IRLTLT01CAM156N",
        "policy_rate": "IR3TIB01CAM156N",
        "cpi":         "CANCPIALLMINMEI",
    },
    "NZD": {
        "10y_yield":   "IRLTLT01NZM156N",
        "policy_rate": "IR3TIB01NZM156N",
        "cpi":         "NZLCPIALLQINMEI",
    },
}

CACHE_FILE = config.STATE_DIR / "fundamentals.json"
CACHE_TTL_SEC = 24 * 60 * 60   # 1 day — these are slow-moving series
FRED_BASE = "https://fred.stlouisfed.org/graph/fredgraph.csv?id="

_HTTP_TIMEOUT = 20


def _http_get(url: str) -> Optional[str]:
    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "TeamAgent/1.0 (+https://github.com/Jony-wws)"},
        )
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as r:
            data = r.read().decode("utf-8", errors="replace")
            return data
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        log.warning(f"fundamentals: GET failed {url}: {e}")
        return None


def _parse_fred_csv(text: str) -> list[tuple[str, float]]:
    """Returns list of (date_iso, value) sorted ascending."""
    out: list[tuple[str, float]] = []
    if not text:
        return out
    rdr = csv.reader(io.StringIO(text))
    rows = list(rdr)
    if not rows:
        return out
    header = rows[0]
    # FRED uses observation_date in newer dumps and DATE in older
    if len(header) < 2:
        return out
    for row in rows[1:]:
        if len(row) < 2:
            continue
        date_s, val_s = row[0], row[1]
        if not date_s or val_s in ("", ".", "NA", "null"):
            continue
        try:
            v = float(val_s)
        except ValueError:
            continue
        out.append((date_s, v))
    out.sort(key=lambda x: x[0])
    return out


def _cpi_yoy_pct(series: list[tuple[str, float]]) -> Optional[float]:
    """Year-over-year CPI inflation in percent.
    Compare latest value to the value ~12 months ago (closest match)."""
    if len(series) < 2:
        return None
    latest_date, latest_val = series[-1]
    # find the row with date >= 12 months before latest, but as close as possible
    try:
        ld = datetime.fromisoformat(latest_date)
    except ValueError:
        return None
    target = ld.replace(year=ld.year - 1) if ld.month != 2 or ld.day != 29 else ld.replace(
        year=ld.year - 1, day=28
    )
    target_iso = target.date().isoformat()
    # binary-search-ish: find last entry with date <= target
    candidate = None
    for date_s, val in series:
        if date_s <= target_iso:
            candidate = (date_s, val)
        else:
            break
    if not candidate or candidate[1] == 0:
        return None
    return round((latest_val - candidate[1]) / candidate[1] * 100.0, 2)


def fetch_one_series(currency: str, kind: str) -> Optional[dict]:
    """Returns {'value': latest value, 'date': YYYY-MM-DD, 'yoy_pct': for CPI}."""
    series_id = FRED_SERIES.get(currency, {}).get(kind)
    if not series_id:
        return None
    url = f"{FRED_BASE}{series_id}"
    text = _http_get(url)
    if not text:
        return None
    rows = _parse_fred_csv(text)
    if not rows:
        return None
    latest_date, latest_val = rows[-1]
    out: dict = {
        "series_id": series_id,
        "date": latest_date,
        "value": latest_val,
    }
    if kind == "cpi":
        yoy = _cpi_yoy_pct(rows)
        if yoy is not None:
            out["yoy_pct"] = yoy
    return out


def fetch_all() -> dict:
    """Fetch all FRED series for all 8 currencies. Slow (~30-50 sec for ~24
    HTTP calls). Caches to fundamentals.json."""
    out: dict = {
        "as_of": datetime.now(timezone.utc).isoformat(),
        "currencies": {},
        "source": "FRED (https://fred.stlouisfed.org) — public CSV, no API key",
    }
    for ccy, ids in FRED_SERIES.items():
        out["currencies"][ccy] = {}
        for kind in ids:
            row = fetch_one_series(ccy, kind)
            if row is not None:
                out["currencies"][ccy][kind] = row
    return out


def get_cached(force_refresh: bool = False) -> dict:
    """Returns cached fundamentals (refreshes if older than CACHE_TTL_SEC)."""
    if not force_refresh and CACHE_FILE.exists():
        try:
            data = json.loads(CACHE_FILE.read_text())
            ts = datetime.fromisoformat(data["as_of"])
            age = (datetime.now(timezone.utc) - ts).total_seconds()
            if age < CACHE_TTL_SEC:
                return data
        except (json.JSONDecodeError, ValueError, KeyError):
            pass
    log.info("fundamentals: refreshing FRED data (24h cache expired)")
    data = fetch_all()
    CACHE_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    return data


# ─── Per-pair derived signals ────────────────────────────────────────────────

# Maps each pair to (base, quote) currency codes.
PAIR_TO_CCY: dict[str, tuple[str, str]] = {
    # USD pairs
    "EURUSD": ("EUR", "USD"), "GBPUSD": ("GBP", "USD"),
    "USDJPY": ("USD", "JPY"), "USDCHF": ("USD", "CHF"),
    "AUDUSD": ("AUD", "USD"), "USDCAD": ("USD", "CAD"),
    "NZDUSD": ("NZD", "USD"),
    # EUR crosses
    "EURGBP": ("EUR", "GBP"), "EURJPY": ("EUR", "JPY"),
    "EURCHF": ("EUR", "CHF"), "EURAUD": ("EUR", "AUD"),
    "EURCAD": ("EUR", "CAD"), "EURNZD": ("EUR", "NZD"),
    # GBP crosses
    "GBPJPY": ("GBP", "JPY"), "GBPCHF": ("GBP", "CHF"),
    "GBPAUD": ("GBP", "AUD"), "GBPCAD": ("GBP", "CAD"),
    "GBPNZD": ("GBP", "NZD"),
    # JPY crosses
    "AUDJPY": ("AUD", "JPY"), "NZDJPY": ("NZD", "JPY"),
    "CADJPY": ("CAD", "JPY"), "CHFJPY": ("CHF", "JPY"),
    # AUD/NZD/CAD/CHF crosses
    "AUDNZD": ("AUD", "NZD"), "AUDCAD": ("AUD", "CAD"),
    "AUDCHF": ("AUD", "CHF"), "NZDCAD": ("NZD", "CAD"),
    "NZDCHF": ("NZD", "CHF"), "CADCHF": ("CAD", "CHF"),
}


def pair_macro_tilt(pair: str, fundamentals: Optional[dict] = None) -> dict:
    """For a given pair, computes macro 'tilt': BUY if base currency is
    fundamentally stronger, SELL if quote is stronger.

    Components:
    - rate_diff_pct (base.policy - quote.policy)
    - yield_diff_pct (base.10y - quote.10y)
    - cpi_diff_pct (base.cpi_yoy - quote.cpi_yoy) — positive = base inflating
      faster = more pressure on its central bank to hike.

    Returns:
      {
        "pair": ...,
        "base": ..., "quote": ...,
        "rate_diff_pct": float | None,
        "yield_diff_pct": float | None,
        "cpi_diff_pct": float | None,
        "tilt_score": -100..100  (positive = BUY, negative = SELL),
        "side": "BUY" | "SELL" | "NEUTRAL",
        "confidence_pct": 0..100,
      }
    """
    fundamentals = fundamentals or get_cached()
    pair = pair.upper()
    if pair not in PAIR_TO_CCY:
        return {"pair": pair, "side": "NEUTRAL", "tilt_score": 0,
                "confidence_pct": 0, "note": "pair not mapped"}
    base, quote = PAIR_TO_CCY[pair]
    cdata = fundamentals.get("currencies", {})
    b = cdata.get(base, {})
    q = cdata.get(quote, {})

    def diff(kind: str, prefer_yoy: bool = False) -> Optional[float]:
        bv = b.get(kind)
        qv = q.get(kind)
        if not bv or not qv:
            return None
        if prefer_yoy:
            bvv = bv.get("yoy_pct"); qvv = qv.get("yoy_pct")
        else:
            bvv = bv.get("value");   qvv = qv.get("value")
        if bvv is None or qvv is None:
            return None
        return round(float(bvv) - float(qvv), 3)

    rate_diff = diff("policy_rate")
    yield_diff = diff("10y_yield")
    cpi_diff = diff("cpi", prefer_yoy=True)

    # Aggregate tilt: weighted sum, normalized so each component contributes
    # bounded ±~33 to the final score before clamp.
    parts: list[float] = []
    if rate_diff is not None:
        # rate diff > 0 = base CCY pays more → strong (BUY)
        parts.append(_clamp(rate_diff * 10, -33, 33))
    if yield_diff is not None:
        # 10y yield diff > 0 = same direction
        parts.append(_clamp(yield_diff * 8, -33, 33))
    if cpi_diff is not None:
        # higher CPI on base = pressure to hike → BUY (in modern hawkish era).
        # weaker effect than rate/yield.
        parts.append(_clamp(cpi_diff * 4, -25, 25))

    if not parts:
        return {"pair": pair, "base": base, "quote": quote,
                "side": "NEUTRAL", "tilt_score": 0, "confidence_pct": 0,
                "rate_diff_pct": rate_diff, "yield_diff_pct": yield_diff,
                "cpi_diff_pct": cpi_diff,
                "note": "no fundamental data available"}

    score = round(sum(parts), 2)
    side = "BUY" if score > 5 else ("SELL" if score < -5 else "NEUTRAL")
    # Confidence: how aligned the parts are (all positive or all negative
    # = high confidence; mixed signs = low). And how strong the magnitude is.
    if len(parts) >= 2:
        same_sign = sum(1 for p in parts if (p > 0) == (parts[0] > 0))
        agreement = same_sign / len(parts)
    else:
        agreement = 1.0
    magnitude = min(abs(score) / 50.0, 1.0)
    confidence = round(agreement * magnitude * 100, 1)

    return {
        "pair": pair,
        "base": base, "quote": quote,
        "rate_diff_pct": rate_diff,
        "yield_diff_pct": yield_diff,
        "cpi_diff_pct": cpi_diff,
        "tilt_score": score,
        "side": side,
        "confidence_pct": confidence,
        "components": {
            "rate_part":  parts[0] if rate_diff is not None else None,
            "yield_part": parts[1] if rate_diff is not None and yield_diff is not None
                          else (parts[0] if yield_diff is not None else None),
            "cpi_part":   parts[-1] if cpi_diff is not None else None,
        },
    }


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def all_pair_tilts() -> dict:
    """Compute fundamental tilt for all 28 pairs."""
    fund = get_cached()
    return {
        "as_of": datetime.now(timezone.utc).isoformat(),
        "fundamentals_as_of": fund.get("as_of"),
        "tilts": {pair: pair_macro_tilt(pair, fund) for pair in config.PAIRS},
    }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    log.info("fundamentals: fetching FRED data for 8 currencies...")
    t0 = time.time()
    data = get_cached(force_refresh=True)
    log.info(f"fundamentals: fetched in {time.time()-t0:.1f}s, cached at {CACHE_FILE}")
    # print snapshot
    for ccy, vals in data["currencies"].items():
        rate = vals.get("policy_rate", {}).get("value")
        y10 = vals.get("10y_yield", {}).get("value")
        cpi_yoy = vals.get("cpi", {}).get("yoy_pct")
        log.info(f"  {ccy}: rate={rate}, 10y={y10}, cpi_yoy={cpi_yoy}")
    log.info("\nPer-pair fundamental tilt:")
    tilts = all_pair_tilts()
    for pair, t in tilts["tilts"].items():
        log.info(f"  {pair}: side={t['side']} score={t['tilt_score']} "
                 f"conf={t['confidence_pct']}% rate_diff={t['rate_diff_pct']} "
                 f"yield_diff={t['yield_diff_pct']} cpi_diff={t['cpi_diff_pct']}")
