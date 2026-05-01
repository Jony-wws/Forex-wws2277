"""CFTC Commitments of Traders (COT) — speculator positioning per FX future.

This is the closest free proxy for "institutional order flow" — every Friday
the CFTC publishes large traders' net long/short positions in EUR, GBP, JPY,
CHF, AUD, CAD, NZD futures (CME).

Why it's useful for FX:
- When leveraged-money funds are stretched LONG (e.g. 95th percentile), the
  trade is "crowded" — historically the next 1-2 weeks tend to mean-revert.
- When they're stretched SHORT, the squeeze risk is high.
- This is the SAME data hedge funds & banks watch every Saturday morning.

Source: https://publicreporting.cftc.gov/resource/gpe5-46if.json
(Traders in Financial Futures, "futures only" report, no API key required).

Per-currency: pulls last 52 weekly observations, computes:
- net_lev_money = lev_money_long - lev_money_short
- net_pct_oi = net_lev_money / open_interest * 100
- z_score over 52w = (net_pct_oi - mean) / std
- extreme positioning thresholds: |z| > 1.5 → contrarian signal

Per-pair signal (e.g. EURUSD):
- If EUR specs net-long extremely → expect EURUSD reversal SHORT (sell)
- If EUR specs net-short extremely → expect EURUSD reversal LONG (buy)
- For crosses (e.g. EURGBP): combine both currencies' z-scores.
"""

from __future__ import annotations

import json
import logging
import math
import statistics
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import urllib.error
import urllib.request

from . import config

log = logging.getLogger("cot")

CACHE_FILE = config.STATE_DIR / "cot_positioning.json"
CACHE_TTL_SEC = 24 * 60 * 60   # COT updates Fridays — daily refresh is plenty.

# Public CFTC Socrata API. No key required for ≤1k rows/day per IP.
COT_BASE = "https://publicreporting.cftc.gov/resource/gpe5-46if.json"

# CFTC contract names (use SODA $where to filter exactly).
COT_CONTRACTS: dict[str, str] = {
    "EUR": "EURO FX",
    "GBP": "BRITISH POUND",
    "JPY": "JAPANESE YEN",
    "CHF": "SWISS FRANC",
    "AUD": "AUSTRALIAN DOLLAR",
    "CAD": "CANADIAN DOLLAR",
    "NZD": "NZ DOLLAR",
}

_HTTP_TIMEOUT = 25


def _http_json(url: str) -> Optional[list]:
    try:
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "TeamAgent/1.0 (+https://github.com/Jony-wws)",
                "Accept": "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as r:
            return json.loads(r.read().decode("utf-8", errors="replace"))
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as e:
        log.warning(f"cot: GET failed {url}: {e}")
        return None


def fetch_currency_history(currency: str, weeks: int = 52) -> list[dict]:
    """Fetch last N weekly COT reports for a given currency. Returns most-
    recent-first. Each row contains all report fields; we trim to what we use.
    """
    contract = COT_CONTRACTS.get(currency)
    if not contract:
        return []
    # SODA query: contract_market_name = '<name>', order desc, limit
    # Note: contract_market_name is preferred over the long
    # market_and_exchange_names because it's the canonical short form.
    where = f"contract_market_name='{contract}'"
    url = (f"{COT_BASE}?$where={urllib.request.quote(where)}"
           f"&$order={urllib.request.quote('report_date_as_yyyy_mm_dd DESC')}"
           f"&$limit={weeks}")
    rows = _http_json(url)
    if not rows:
        return []
    out = []
    for r in rows:
        try:
            row = {
                "date":         r.get("report_date_as_yyyy_mm_dd"),
                "open_interest": _to_int(r.get("open_interest_all")),
                "lev_money_long":   _to_int(r.get("lev_money_positions_long")),
                "lev_money_short":  _to_int(r.get("lev_money_positions_short")),
                "asset_mgr_long":   _to_int(r.get("asset_mgr_positions_long")),
                "asset_mgr_short":  _to_int(r.get("asset_mgr_positions_short")),
                "dealer_long":      _to_int(r.get("dealer_positions_long_all")),
                "dealer_short":     _to_int(r.get("dealer_positions_short_all")),
            }
            out.append(row)
        except (TypeError, ValueError):
            continue
    return out


def _to_int(v) -> Optional[int]:
    if v is None or v == "":
        return None
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def compute_currency_metrics(rows: list[dict]) -> dict:
    """For ≥4 weeks of data, compute net_lev_money, net_pct_oi history, and
    latest z-score vs 52-week distribution.

    Returns:
      {
        "latest_date": ...,
        "open_interest": ...,
        "net_lev_money": int,                # latest week's net (long-short)
        "net_pct_oi":   float (%),
        "z_score":      float (~ -3..+3 typical range),
        "n_weeks":      int,
        "mean_pct_oi":  float,
        "std_pct_oi":   float,
        "extreme":      str  ("crowded long" / "crowded short" / "neutral"),
      }
    """
    if not rows:
        return {"error": "no data"}
    pct_history: list[float] = []
    for r in rows:
        oi = r.get("open_interest")
        ll = r.get("lev_money_long"); ss = r.get("lev_money_short")
        if not oi or ll is None or ss is None:
            continue
        net = ll - ss
        pct_history.append((r["date"], net / oi * 100.0))
    if len(pct_history) < 4:
        return {"error": "insufficient history"}
    # latest is index 0 (we ordered DESC)
    latest_date, latest_pct = pct_history[0]
    series = [p for _, p in pct_history]
    mean = statistics.mean(series)
    std = statistics.pstdev(series) if len(series) >= 2 else 0
    z = (latest_pct - mean) / std if std > 0 else 0
    if z > 1.5:
        extreme = "crowded long (contrarian SHORT signal)"
    elif z < -1.5:
        extreme = "crowded short (contrarian LONG signal)"
    elif z > 0.7:
        extreme = "tilted long"
    elif z < -0.7:
        extreme = "tilted short"
    else:
        extreme = "neutral"
    latest = rows[0]
    return {
        "latest_date":   latest_date,
        "open_interest": latest.get("open_interest"),
        "lev_money_long":  latest.get("lev_money_long"),
        "lev_money_short": latest.get("lev_money_short"),
        "net_lev_money":   (latest.get("lev_money_long") or 0)
                           - (latest.get("lev_money_short") or 0),
        "net_pct_oi":  round(latest_pct, 2),
        "z_score":     round(z, 2),
        "mean_pct_oi": round(mean, 2),
        "std_pct_oi":  round(std, 2),
        "n_weeks":     len(series),
        "extreme":     extreme,
    }


def fetch_all() -> dict:
    """Fetch latest 52 weeks of COT data for all 7 currencies."""
    out: dict = {
        "as_of": datetime.now(timezone.utc).isoformat(),
        "source": ("CFTC Commitments of Traders, Traders in Financial Futures "
                   "(public Socrata API, no key required)"),
        "currencies": {},
    }
    for ccy in COT_CONTRACTS:
        rows = fetch_currency_history(ccy)
        out["currencies"][ccy] = compute_currency_metrics(rows)
        # rate-limit ourselves a tiny bit
        time.sleep(0.3)
    return out


def get_cached(force_refresh: bool = False) -> dict:
    if not force_refresh and CACHE_FILE.exists():
        try:
            data = json.loads(CACHE_FILE.read_text())
            ts = datetime.fromisoformat(data["as_of"])
            age = (datetime.now(timezone.utc) - ts).total_seconds()
            if age < CACHE_TTL_SEC:
                return data
        except (json.JSONDecodeError, ValueError, KeyError):
            pass
    log.info("cot: refreshing CFTC weekly positioning (24h cache expired)")
    data = fetch_all()
    CACHE_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    return data


# ─── Per-pair contrarian signal ────────────────────────────────────────────

# We piggy-back on fundamentals.PAIR_TO_CCY for base/quote split.
def pair_cot_signal(pair: str, cot: Optional[dict] = None) -> dict:
    """Per-pair COT-derived signal.

    Logic:
    - Pull base.z and quote.z (USD has no COT future on its own; for USD
      pairs we treat USD positioning as inverse of the foreign currency).
    - Combined z = base_z - quote_z (if both available).
    - Extreme combined z (>|2|) → contrarian SHORT/LONG vote.

    Returns:
      {
        "pair": ..., "base": ..., "quote": ...,
        "base_z": float|None, "quote_z": float|None,
        "combined_z": float|None,
        "side": "BUY" | "SELL" | "NEUTRAL",
        "strength_pct": 0..100,
        "note": ...
      }
    """
    from .fundamentals import PAIR_TO_CCY
    cot = cot or get_cached()
    pair = pair.upper()
    if pair not in PAIR_TO_CCY:
        return {"pair": pair, "side": "NEUTRAL", "note": "pair not mapped"}
    base, quote = PAIR_TO_CCY[pair]
    metrics = cot.get("currencies", {})

    def get_z(ccy: str) -> Optional[float]:
        m = metrics.get(ccy) or {}
        z = m.get("z_score")
        return float(z) if isinstance(z, (int, float)) else None

    base_z = get_z(base) if base != "USD" else None
    quote_z = get_z(quote) if quote != "USD" else None

    # USD has no own COT future in this report — for USD pairs we use the
    # foreign currency z and treat USD as inverse.
    if base == "USD" and quote_z is not None:
        # USDxxx: if foreign currency specs are crowded long → expect xxx
        # weakness → USDxxx UP → BUY.
        combined_z = -quote_z
    elif quote == "USD" and base_z is not None:
        # xxxUSD: if base currency specs are crowded long → reversal
        # → xxx weakness → xxxUSD DOWN → SELL.
        combined_z = base_z   # we want the contrarian flip below
    elif base_z is not None and quote_z is not None:
        # crosses: combine
        combined_z = base_z - quote_z
    else:
        return {"pair": pair, "base": base, "quote": quote,
                "base_z": base_z, "quote_z": quote_z,
                "side": "NEUTRAL", "strength_pct": 0,
                "note": "insufficient COT history"}

    # CONTRARIAN: extreme positive combined_z → expect mean-reversion → SELL.
    side = "NEUTRAL"
    if combined_z > 1.5:
        side = "SELL"
    elif combined_z < -1.5:
        side = "BUY"

    # confidence scales with |z| above 1.5, capped at 3.0
    if abs(combined_z) <= 1.5:
        strength = 0.0
    else:
        strength = min(100.0, (abs(combined_z) - 1.5) / 1.5 * 100.0)

    return {
        "pair": pair, "base": base, "quote": quote,
        "base_z": base_z, "quote_z": quote_z,
        "combined_z": round(combined_z, 2),
        "side": side,
        "strength_pct": round(strength, 1),
        "note": ("contrarian: specs crowded → expect reversal"
                 if side != "NEUTRAL" else "no extreme positioning"),
    }


def all_pair_signals() -> dict:
    cot = get_cached()
    return {
        "as_of": datetime.now(timezone.utc).isoformat(),
        "cot_as_of": cot.get("as_of"),
        "signals": {p: pair_cot_signal(p, cot) for p in config.PAIRS},
    }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    log.info("cot: fetching CFTC weekly positioning for 7 currencies...")
    t0 = time.time()
    data = get_cached(force_refresh=True)
    log.info(f"cot: fetched in {time.time()-t0:.1f}s, cached at {CACHE_FILE}")
    for ccy, m in data["currencies"].items():
        log.info(f"  {ccy}: net_pct_oi={m.get('net_pct_oi')}% z={m.get('z_score')} "
                 f"({m.get('extreme')})")
    log.info("\nPer-pair COT-derived contrarian signal:")
    sig = all_pair_signals()
    for p, s in sig["signals"].items():
        log.info(f"  {p}: side={s['side']} z={s.get('combined_z')} "
                 f"strength={s.get('strength_pct')}% — {s.get('note')}")
