"""Real-data macro layer for the AI brain.

Pulls a few public Yahoo Finance proxies that map cleanly onto FX
fundamentals:

    DXY               (DX-Y.NYB)          USD strength
    US 10y yield      (^TNX)              USD rate expectations
    DE 10y bund       (no clean Yahoo ticker → derived from BNDX)
    JP 10y JGB        (proxied via ^N225 / JPY tnx not available)
    Gold              (GC=F)              safe-haven, USD-inverse
    Oil (Brent)       (BZ=F)              CAD-positive, JPY-negative

These are *real instruments quoted publicly* — no paid API, no
simulators.  The module derives a per-currency signed strength score
in [-3, +3] from the latest 5 sessions of price action plus a carry
score from the cross-currency yield spread implied by yfinance.

If yfinance is unavailable (offline test, network glitch) the module
gracefully degrades to zero scores instead of crashing — the brain
treats that as "no macro signal" rather than blocking the cycle.
"""
from __future__ import annotations

import logging
from typing import Optional

import pandas as pd

log = logging.getLogger("macro")

# Currencies tracked by the system.  Order matches the pair list in
# app/config.py — 8 majors.
CURRENCIES = ["USD", "EUR", "GBP", "JPY", "CHF", "AUD", "CAD", "NZD"]


# Yahoo tickers that the brain actually fetches.  Anything that yfinance
# refuses gets caught and ignored downstream — see ``fetch_macro_snapshot``.
MACRO_TICKERS: dict[str, str] = {
    "DXY": "DX-Y.NYB",     # US Dollar Index spot
    "US10Y": "^TNX",       # CBOE 10-year US Treasury yield (×10)
    "DE10Y": "EWG",        # iShares MSCI Germany ETF — Bund proxy
    "GBP10Y": "EWU",       # iShares MSCI UK
    "JPY10Y": "EWJ",       # iShares MSCI Japan
    "GOLD": "GC=F",
    "BRENT": "BZ=F",
    "VIX": "^VIX",
}


def _safe_pct_change(df: pd.DataFrame, bars: int = 5) -> float:
    if df is None or df.empty or len(df) < bars + 1:
        return 0.0
    last = float(df["Close"].iloc[-1])
    prev = float(df["Close"].iloc[-bars - 1])
    if prev == 0:
        return 0.0
    return (last - prev) / prev * 100.0


_MACRO_CACHE: dict[str, tuple[float, pd.DataFrame]] = {}
_MACRO_CACHE_TTL_SEC = 300  # 5 minutes


def _fetch_index_or_etf(ticker: str) -> pd.DataFrame:
    """Fetch a non-FX yfinance symbol (index, ETF, commodity) directly.

    The repo's ``app.prices.fetch_bars`` is purpose-built for forex tickers
    and unconditionally appends ``=X`` — we can't reuse it here.  This
    helper caches results for 5 min so a single cron run that touches the
    macro layer many times doesn't hammer Yahoo.
    """
    import time as _t

    cached = _MACRO_CACHE.get(ticker)
    if cached and _t.time() - cached[0] < _MACRO_CACHE_TTL_SEC:
        return cached[1].copy()

    try:
        import yfinance as yf
    except Exception:
        return pd.DataFrame()

    try:
        df = yf.download(
            ticker,
            interval="1d",
            period="1mo",
            progress=False,
            auto_adjust=False,
            prepost=False,
            threads=False,
        )
    except Exception as e:
        log.warning(f"macro yfinance failed {ticker}: {e}")
        return pd.DataFrame()
    if df is None or df.empty:
        return pd.DataFrame()
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = [c[0] if isinstance(c, tuple) else c for c in df.columns]
    df = df[["Open", "High", "Low", "Close", "Volume"]].dropna()
    _MACRO_CACHE[ticker] = (_t.time(), df.copy())
    return df


def fetch_macro_snapshot() -> dict:
    """Return a {label: 5-bar % change} snapshot for the macro tickers."""
    out: dict[str, float] = {}
    for label, ticker in MACRO_TICKERS.items():
        try:
            df = _fetch_index_or_etf(ticker)
        except Exception as e:
            log.warning(f"macro fetch failed {label}: {e}")
            df = pd.DataFrame()
        if df is None or df.empty:
            out[label] = 0.0
        else:
            out[label] = round(_safe_pct_change(df, bars=5), 3)
    return out


def currency_strength_from_macro(macro: dict) -> dict:
    """Translate the macro snapshot into per-currency strength scores.

    Each currency gets a signed score in [-3, +3]:

        USD ← +DXY −Gold
        EUR ← +DE10Y −DXY × 0.5
        GBP ← +GBP10Y −DXY × 0.3
        JPY ← +JPY10Y −Risk_on (risk-off lifts JPY)
        CHF ← +Gold −DXY × 0.3 (safe-haven)
        AUD ← +Brent −VIX
        CAD ← +Brent −DXY × 0.3
        NZD ← +Brent −VIX
    """
    if not macro:
        return {c: 0.0 for c in CURRENCIES}

    g = lambda k: macro.get(k, 0.0)

    risk_on = -g("VIX")  # falling VIX = risk-on
    scores = {
        "USD": +1.0 * g("DXY") - 0.5 * g("GOLD"),
        "EUR": +1.0 * g("DE10Y") - 0.5 * g("DXY"),
        "GBP": +1.0 * g("GBP10Y") - 0.3 * g("DXY"),
        "JPY": +1.0 * g("JPY10Y") - 0.5 * risk_on,
        "CHF": +0.6 * g("GOLD") - 0.3 * g("DXY"),
        "AUD": +1.0 * g("BRENT") + 0.4 * risk_on,
        "CAD": +1.0 * g("BRENT") - 0.3 * g("DXY"),
        "NZD": +0.8 * g("BRENT") + 0.4 * risk_on,
    }

    # Normalise to ±3 using a soft clip based on a 0.5%/day "strong" reference.
    out = {}
    for c, raw in scores.items():
        clipped = max(-3.0, min(3.0, raw / 0.5 * 1.0))
        out[c] = round(clipped, 2)
    return out


def pair_macro_score(pair: str, currency_scores: dict) -> dict:
    """Compute a per-pair macro score = base − quote.

    The brain interprets +3 as "macro fundamentals strongly favour the
    pair going up", −3 as the opposite, and 0 as macro-neutral.
    """
    if len(pair) != 6:
        return {"score": 0, "reason": "Неизвестная пара", "base": 0, "quote": 0}
    base, quote = pair[:3], pair[3:]
    bs = currency_scores.get(base, 0.0)
    qs = currency_scores.get(quote, 0.0)
    diff = bs - qs
    score = max(-3, min(3, round(diff)))
    direction = "укрепляется" if score > 0 else "ослабевает" if score < 0 else "нейтрально"
    return {
        "score": score,
        "base": bs,
        "quote": qs,
        "reason": f"Макро: {base} {direction} против {quote}",
    }
