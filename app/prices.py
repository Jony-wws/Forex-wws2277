"""Price fetcher using Yahoo Finance - real data only."""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

import pandas as pd
import yfinance as yf

from .config import yahoo_ticker

log = logging.getLogger("prices")

_CACHE: dict[str, tuple[float, pd.DataFrame]] = {}
_PRICE_CACHE: dict[str, tuple[float, float]] = {}
_PRICE_TTL = 8  # seconds


def fetch_bars(pair: str, interval: str = "1h", period: str = "1mo") -> pd.DataFrame:
    key = f"{pair}_{interval}_{period}"
    cached = _CACHE.get(key)
    ttl = {"1m": 10, "5m": 30, "15m": 60, "1h": 90}.get(interval, 120)
    if cached and time.time() - cached[0] < ttl:
        return cached[1].copy()

    ticker = yahoo_ticker(pair)
    try:
        df = yf.download(
            ticker, interval=interval, period=period,
            progress=False, auto_adjust=False, prepost=False, threads=False,
        )
    except Exception as e:
        log.warning(f"fetch failed {pair} {interval}: {e}")
        return _CACHE.get(key, (0, pd.DataFrame()))[1].copy() if key in _CACHE else pd.DataFrame()

    if df is None or df.empty:
        return _CACHE.get(key, (0, pd.DataFrame()))[1].copy() if key in _CACHE else pd.DataFrame()

    if isinstance(df.columns, pd.MultiIndex):
        df.columns = [c[0] if isinstance(c, tuple) else c for c in df.columns]

    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC")
    else:
        df.index = df.index.tz_convert("UTC")

    df = df[["Open", "High", "Low", "Close", "Volume"]].dropna()
    _CACHE[key] = (time.time(), df.copy())
    return df


def get_current_price(pair: str) -> float | None:
    cached = _PRICE_CACHE.get(pair)
    if cached and time.time() - cached[0] < _PRICE_TTL:
        return cached[1]

    df = fetch_bars(pair, interval="1m", period="1d")
    if df.empty:
        return cached[1] if cached else None
    price = float(df["Close"].iloc[-1])
    _PRICE_CACHE[pair] = (time.time(), price)
    return price


def get_price_change(pair: str) -> dict | None:
    df = fetch_bars(pair, interval="1h", period="2d")
    if df.empty or len(df) < 2:
        return None
    current = float(df["Close"].iloc[-1])
    prev_24h = float(df["Close"].iloc[0])
    change = current - prev_24h
    change_pct = (change / prev_24h) * 100 if prev_24h else 0
    return {
        "current": current,
        "prev_24h": prev_24h,
        "change": change,
        "change_pct": round(change_pct, 3),
    }
