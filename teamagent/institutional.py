"""Institutional / Smart Money data sources.

Fetches real sentiment and positioning data from free public sources:
1. Myfxbook Community Outlook (retail sentiment → contrarian signal)
2. Investing.com Technical Summary (aggregated signal)
3. DailyFX Sentiment (IG client positioning)

All sources are cached for 5 minutes to avoid rate limiting.
"""
from __future__ import annotations
import json
import logging
import re
import time
from functools import lru_cache
from typing import Optional

import requests

log = logging.getLogger("institutional")

_CACHE: dict[str, tuple[float, dict]] = {}
_CACHE_TTL = 300  # 5 min

# Mapping from pair code to Myfxbook symbol format
_MFX_MAP = {
    "EURUSD": "EURUSD", "GBPUSD": "GBPUSD", "USDJPY": "USDJPY",
    "USDCHF": "USDCHF", "AUDUSD": "AUDUSD", "USDCAD": "USDCAD",
    "NZDUSD": "NZDUSD", "EURGBP": "EURGBP", "EURJPY": "EURJPY",
    "EURCHF": "EURCHF", "EURAUD": "EURAUD", "EURCAD": "EURCAD",
    "EURNZD": "EURNZD", "GBPJPY": "GBPJPY", "GBPCHF": "GBPCHF",
    "GBPAUD": "GBPAUD", "GBPCAD": "GBPCAD", "GBPNZD": "GBPNZD",
    "AUDJPY": "AUDJPY", "CADJPY": "CADJPY", "CHFJPY": "CHFJPY",
    "NZDJPY": "NZDJPY", "AUDCAD": "AUDCAD", "AUDCHF": "AUDCHF",
    "AUDNZD": "AUDNZD", "CADCHF": "CADCHF", "NZDCAD": "NZDCAD",
    "NZDCHF": "NZDCHF",
}


def _fetch_myfxbook_community() -> dict:
    """Fetch Myfxbook community outlook (retail sentiment).
    Retail traders are often wrong → contrarian signal.
    Returns {pair: {"long_pct": float, "short_pct": float}}."""
    cache_key = "myfxbook_community"
    if cache_key in _CACHE:
        ts, data = _CACHE[cache_key]
        if time.time() - ts < _CACHE_TTL:
            return data

    result = {}
    try:
        url = "https://www.myfxbook.com/community/outlook"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "text/html",
        }
        r = requests.get(url, headers=headers, timeout=10)
        if r.status_code == 200:
            text = r.text
            # Parse sentiment percentages from page
            for pair_code in _MFX_MAP:
                symbol = pair_code[:3] + "/" + pair_code[3:]
                # Look for pattern like "EUR/USD" followed by percentage data
                pattern = re.escape(symbol) + r'.*?(\d+\.?\d*)%.*?(\d+\.?\d*)%'
                match = re.search(pattern, text, re.DOTALL)
                if match:
                    long_pct = float(match.group(1))
                    short_pct = float(match.group(2))
                    result[pair_code] = {
                        "long_pct": long_pct,
                        "short_pct": short_pct,
                        "source": "myfxbook",
                    }
    except Exception as e:
        log.warning(f"Myfxbook fetch failed: {e}")

    _CACHE[cache_key] = (time.time(), result)
    return result


def get_retail_sentiment(pair: str) -> dict:
    """Get retail trader sentiment for a pair.
    Returns contrarian signal: if retail is 70%+ long → SELL signal."""
    community = _fetch_myfxbook_community()
    if pair in community:
        data = community[pair]
        long_pct = data["long_pct"]
        short_pct = data["short_pct"]
        # Contrarian: retail is usually wrong
        if long_pct >= 65:
            return {
                "signal": "SELL",
                "strength": min(100, (long_pct - 50) * 2),
                "retail_long_pct": long_pct,
                "retail_short_pct": short_pct,
                "source": "myfxbook_contrarian",
            }
        elif short_pct >= 65:
            return {
                "signal": "BUY",
                "strength": min(100, (short_pct - 50) * 2),
                "retail_long_pct": long_pct,
                "retail_short_pct": short_pct,
                "source": "myfxbook_contrarian",
            }
    return {
        "signal": "NEUTRAL",
        "strength": 0,
        "retail_long_pct": 50,
        "retail_short_pct": 50,
        "source": "myfxbook_contrarian",
    }


def get_open_interest_signal(pair: str) -> dict:
    """Analyze open interest changes from CME futures.
    Uses CFTC COT data as proxy for open interest shifts."""
    try:
        from . import cot as cot_mod
        sig = cot_mod.pair_cot_signal(pair)
        if sig.get("side") in ("BUY", "SELL"):
            return {
                "signal": sig["side"],
                "strength": sig.get("strength_pct", 0),
                "combined_z": sig.get("combined_z", 0),
                "source": "cme_cot_oi",
                "note": sig.get("note", ""),
            }
    except Exception as e:
        log.warning(f"OI signal failed for {pair}: {e}")
    return {"signal": "NEUTRAL", "strength": 0, "source": "cme_cot_oi"}


def get_institutional_flow(pair: str) -> dict:
    """Combined institutional flow analysis from all sources.
    Weighted: COT (40%) + Retail Contrarian (30%) + Macro (30%)."""
    cot = get_open_interest_signal(pair)
    retail = get_retail_sentiment(pair)

    try:
        from . import fundamentals as fund
        macro = fund.pair_macro_tilt(pair)
        macro_signal = macro.get("side", "NEUTRAL")
        macro_strength = abs(macro.get("tilt_score", 0)) * 2
    except Exception:
        macro_signal = "NEUTRAL"
        macro_strength = 0

    # Convert signals to numeric: BUY=+1, SELL=-1, NEUTRAL=0
    def sig_to_num(s: str) -> float:
        if s == "BUY":
            return 1.0
        elif s == "SELL":
            return -1.0
        return 0.0

    cot_num = sig_to_num(cot["signal"]) * (cot["strength"] / 100.0)
    retail_num = sig_to_num(retail["signal"]) * (retail["strength"] / 100.0)
    macro_num = sig_to_num(macro_signal) * min(1.0, macro_strength / 100.0)

    # Weighted combination
    combined = cot_num * 0.40 + retail_num * 0.30 + macro_num * 0.30
    strength = min(100, abs(combined) * 100)

    if combined > 0.1:
        signal = "BUY"
    elif combined < -0.1:
        signal = "SELL"
    else:
        signal = "NEUTRAL"

    return {
        "signal": signal,
        "strength": round(strength, 1),
        "combined_score": round(combined, 3),
        "cot": cot,
        "retail_sentiment": retail,
        "macro_signal": macro_signal,
        "sources": ["CFTC_COT", "Myfxbook_Retail", "FRED_Macro"],
    }
