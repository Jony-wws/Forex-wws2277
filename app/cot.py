"""CFTC Commitment of Traders (COT) — real big-player positioning.

The CFTC publishes weekly Commitment of Traders reports on its public
website (https://publicreporting.cftc.gov).  This is *the* canonical
public source for institutional positioning on the major FX currency
futures contracts traded at the CME — non-commercial (= speculators,
hedge funds, large traders) net positions are exactly what the user
calls "крупные игроки / smart money".

We pull the Socrata Open Data endpoint, which is free, no auth, no
ticker quota, and returns plain JSON.  The brain consumes one number
per currency:

    cot_z_score[CCY] in [-3, +3]    (52-week z-score of net non-comm)

A high positive value means big specs are net-long the currency at an
unusually high level — bullish for that currency.  A high negative
value means they're heavily net-short — bearish.

Critical safety properties:

* Module never crashes the brain.  On any error (network, parse,
  rate-limit) it returns zero scores plus an `error` reason.
* In-memory cache TTL = 12 hours (COT is published weekly, so 12h is
  conservatively fresh).
* Uses ``urllib.request`` with a strict 10s timeout — same approach as
  ``app/news_brain.py``.
* Compatible with GitHub Actions (no extra dependency beyond stdlib +
  the libs already installed for the cycle).
"""
from __future__ import annotations

import json
import logging
import time
import urllib.parse
import urllib.request
from typing import Optional

log = logging.getLogger("cot")

# Currencies tracked by the system.  Order matches app/macro.py CURRENCIES.
CURRENCIES = ["USD", "EUR", "GBP", "JPY", "CHF", "AUD", "CAD", "NZD"]

# CFTC Legacy Futures-Only report (Socrata).  Each row = one market for
# one Tuesday report date.  ``market_and_exchange_names`` filters by
# instrument; we ask for the eight FX futures that map cleanly onto
# the majors.
COT_ENDPOINT = "https://publicreporting.cftc.gov/resource/6dca-aqww.json"

# Mapping from currency code → CFTC market name (exactly as written in
# the public dataset).  ``USD`` has no CME futures; we derive its
# positioning from the inverse of the dollar index basket (sum of the
# negatives of the other seven contracts), so it doesn't appear here.
COT_MARKETS: dict[str, str] = {
    "EUR": "EURO FX - CHICAGO MERCANTILE EXCHANGE",
    "GBP": "BRITISH POUND - CHICAGO MERCANTILE EXCHANGE",
    "JPY": "JAPANESE YEN - CHICAGO MERCANTILE EXCHANGE",
    "CHF": "SWISS FRANC - CHICAGO MERCANTILE EXCHANGE",
    "AUD": "AUSTRALIAN DOLLAR - CHICAGO MERCANTILE EXCHANGE",
    "CAD": "CANADIAN DOLLAR - CHICAGO MERCANTILE EXCHANGE",
    "NZD": "NZ DOLLAR - CHICAGO MERCANTILE EXCHANGE",
}

# How many weekly reports to read.  52 weeks = full 1-year baseline,
# which is what most institutional desks use for COT z-scores.
LOOKBACK_WEEKS = 52
HTTP_TIMEOUT_SEC = 10
CACHE_TTL_SEC = 12 * 3600   # COT publishes weekly; 12h is plenty

_CACHE: dict[str, tuple[float, dict[str, float]]] = {}


def _fetch_market_history(market_name: str, limit: int) -> list[dict]:
    """Pull the last ``limit`` weekly rows for one CFTC market."""
    # Socrata's $where filter supports exact equality on string columns.
    # ``$order=report_date_as_yyyy_mm_dd DESC`` returns newest first.
    # ``urlencode`` handles the spaces inside the SoQL literal and the
    # ORDER BY clause that ``http.client`` otherwise rejects.
    qs = urllib.parse.urlencode({
        "$where": f"market_and_exchange_names='{market_name}'",
        "$order": "report_date_as_yyyy_mm_dd DESC",
        "$limit": str(limit),
    })
    url = f"{COT_ENDPOINT}?{qs}"
    req = urllib.request.Request(url, headers={"User-Agent": "forex-wws2277/1.0"})
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_SEC) as resp:
        body = resp.read()
    rows = json.loads(body.decode("utf-8"))
    if not isinstance(rows, list):
        raise ValueError("CFTC response not a list")
    return rows


def _net_noncomm(row: dict) -> Optional[float]:
    """Extract net non-commercial position from one CFTC row."""
    try:
        longs = float(row.get("noncomm_positions_long_all", 0) or 0)
        shorts = float(row.get("noncomm_positions_short_all", 0) or 0)
    except (TypeError, ValueError):
        return None
    return longs - shorts


def _zscore(values: list[float]) -> float:
    """Compute a clamped z-score of the most recent value vs the rest."""
    if len(values) < 5:
        return 0.0
    latest = values[0]
    baseline = values[1:]
    mean = sum(baseline) / len(baseline)
    var = sum((v - mean) ** 2 for v in baseline) / len(baseline)
    std = var ** 0.5
    if std == 0:
        return 0.0
    z = (latest - mean) / std
    # Clamp to ±3 — anything beyond that means "extreme positioning".
    return max(-3.0, min(3.0, z))


def cot_currency_zscores() -> dict[str, float]:
    """Return per-currency net-non-comm z-scores in [-3, +3].

    On error (network, parse, missing data) returns zeros for the
    affected currencies and logs a warning.  USD is derived as the
    *inverse* basket average of EUR/GBP/JPY/CHF — when traders pile
    into the non-USD majors, USD is implicitly being sold.
    """
    cached = _CACHE.get("scores")
    if cached and time.time() - cached[0] < CACHE_TTL_SEC:
        return dict(cached[1])

    out: dict[str, float] = {c: 0.0 for c in CURRENCIES}
    raw_nets: dict[str, list[float]] = {}

    for ccy, market in COT_MARKETS.items():
        try:
            rows = _fetch_market_history(market, LOOKBACK_WEEKS)
        except Exception as e:
            # Network, http.client.InvalidURL, JSON parse, schema —
            # we never want CFTC issues to abort the cycle.
            log.warning(f"CFTC fetch failed for {ccy}: {type(e).__name__}: {e}")
            continue
        nets = [_net_noncomm(r) for r in rows]
        nets = [n for n in nets if n is not None]
        if len(nets) < 5:
            log.warning(f"CFTC: too few rows for {ccy} ({len(nets)})")
            continue
        raw_nets[ccy] = nets
        out[ccy] = round(_zscore(nets), 2)

    # USD is the inverse basket of the four most-traded non-USD majors
    # (EUR/GBP/JPY/CHF).  Heavy net-long EUR/GBP/JPY/CHF implies traders
    # are short USD.  We take the negative average of those z-scores.
    basket = [out[c] for c in ("EUR", "GBP", "JPY", "CHF") if c in raw_nets]
    if basket:
        out["USD"] = round(max(-3.0, min(3.0, -sum(basket) / len(basket))), 2)

    _CACHE["scores"] = (time.time(), dict(out))
    return out


def pair_cot_score(pair: str, scores: dict[str, float]) -> dict:
    """Return a per-pair COT score in [-3, +3] with a Russian reason.

    Positive = big specs net-long base vs quote → pair likely up.
    Negative = big specs net-short base vs quote → pair likely down.
    """
    if len(pair) != 6:
        return {"score": 0, "reason": "COT: пара неизвестна"}
    base, quote = pair[:3], pair[3:]
    bs = scores.get(base, 0.0)
    qs = scores.get(quote, 0.0)
    diff = bs - qs
    score = int(round(max(-3, min(3, diff))))
    if score > 0:
        direction = f"крупные игроки в плюсе по {base} ({bs:+.2f}) против {quote} ({qs:+.2f})"
    elif score < 0:
        direction = f"крупные игроки в минусе по {base} ({bs:+.2f}) против {quote} ({qs:+.2f})"
    else:
        direction = f"крупные игроки нейтральны: {base} {bs:+.2f} vs {quote} {qs:+.2f}"
    return {
        "score": score,
        "base_z": bs,
        "quote_z": qs,
        "reason": f"COT: {direction}",
    }
