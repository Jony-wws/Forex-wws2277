"""TradingView data feed wrapper with Yahoo Finance fallback.

The user wants the GitHub Actions backtest to use the **same data** that
TradingView traders see, not just Yahoo Finance.  This module tries the
`tvDatafeed` package first (unofficial public TradingView feed via
WebSocket) and falls back to Yahoo Finance if TV is unreachable or the
package is not installed.

Usage from cycle_5h.py:
    from tv_datafeed import fetch_ohlc
    df = fetch_ohlc("EURUSD", "M15", lookback_days=30)
    # df has columns Open/High/Low/Close, DatetimeIndex

The CI workflow can install tvDatafeed via pip; if installation fails the
script keeps working through the Yahoo fallback.
"""
from __future__ import annotations

import os
import time
from datetime import datetime, timedelta
from typing import Optional

import pandas as pd

# Yahoo Finance is always available because the rest of the cycle uses it.
import yfinance as yf

try:                                    # pragma: no cover — optional dep.
    from tvDatafeed import TvDatafeed, Interval  # type: ignore
    _TV_AVAILABLE = True
except ImportError:
    _TV_AVAILABLE = False
    Interval = None                     # type: ignore[assignment]


_TV_INSTANCE: Optional["TvDatafeed"] = None


def _get_tv_client() -> Optional["TvDatafeed"]:
    """Return a cached TvDatafeed client, or None if unavailable."""
    global _TV_INSTANCE
    if not _TV_AVAILABLE:
        return None
    if _TV_INSTANCE is not None:
        return _TV_INSTANCE
    try:
        # Anonymous login works for forex symbols on most TV servers.
        username = os.environ.get("TV_USERNAME") or None
        password = os.environ.get("TV_PASSWORD") or None
        _TV_INSTANCE = TvDatafeed(username=username, password=password)
        return _TV_INSTANCE
    except Exception as e:
        print(f"[tv_datafeed] failed to init TvDatafeed: {e}; falling back to Yahoo")
        return None


_INTERVAL_MAP_TV = {}
if _TV_AVAILABLE:
    _INTERVAL_MAP_TV = {
        "M1":  Interval.in_1_minute,
        "M5":  Interval.in_5_minute,
        "M15": Interval.in_15_minute,
        "M30": Interval.in_30_minute,
        "H1":  Interval.in_1_hour,
        "H4":  Interval.in_4_hour,
        "D1":  Interval.in_daily,
    }

_INTERVAL_MAP_YF = {
    "M1": "1m", "M5": "5m", "M15": "15m", "M30": "30m",
    "H1": "1h", "H4": "4h", "D1": "1d",
}


def _yahoo_fetch(pair: str, interval: str, lookback_days: int) -> pd.DataFrame:
    """Yahoo Finance fallback fetcher — same shape as TV output."""
    yf_interval = _INTERVAL_MAP_YF.get(interval, "15m")
    period = f"{max(1, lookback_days)}d"
    if yf_interval in ("1h", "4h"):
        period = f"{max(30, lookback_days)}d"
    if yf_interval == "1d":
        period = f"{max(60, lookback_days)}d"
    ticker = pair if "=" in pair else f"{pair}=X"
    df = yf.download(ticker, period=period, interval=yf_interval,
                     progress=False, auto_adjust=False)
    if df.empty:
        return df
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df = df[["Open", "High", "Low", "Close", "Volume"]].copy()
    df.index.name = "datetime"
    return df


def fetch_ohlc(pair: str, interval: str = "M15",
               lookback_days: int = 365,
               prefer: str = "tv") -> pd.DataFrame:
    """Fetch OHLC bars for a forex pair.

    Parameters
    ----------
    pair : str   e.g. "EURUSD" (no ``=X`` suffix needed).
    interval : str   one of M1 M5 M15 M30 H1 H4 D1.
    lookback_days : int   how much history to pull.
    prefer : str   "tv" (default) → try TradingView first, fall back to
        Yahoo; "yahoo" → skip TV entirely.
    """
    if prefer == "tv":
        client = _get_tv_client()
        tv_int = _INTERVAL_MAP_TV.get(interval) if _TV_AVAILABLE else None
        if client is not None and tv_int is not None:
            for symbol_form in (pair, pair[:3] + pair[3:]):  # EURUSD, EUR/USD
                for exchange in ("FX_IDC", "OANDA", "FX"):
                    try:
                        # TV bar count: ~96 bars/day on M15.
                        bars_per_day = {
                            "M1": 1440, "M5": 288, "M15": 96, "M30": 48,
                            "H1": 24, "H4": 6, "D1": 1,
                        }.get(interval, 96)
                        n_bars = max(200, lookback_days * bars_per_day)
                        df = client.get_hist(symbol=symbol_form,
                                             exchange=exchange,
                                             interval=tv_int,
                                             n_bars=min(n_bars, 5000))
                        if df is not None and not df.empty:
                            # Normalize columns to match Yahoo output.
                            df = df.rename(columns=str.title)
                            return df[["Open", "High", "Low", "Close", "Volume"]]
                    except Exception as e:
                        print(f"[tv_datafeed] {symbol_form}@{exchange} {interval}: {e}")
                        continue
    # Fallback.
    return _yahoo_fetch(pair, interval, lookback_days)


if __name__ == "__main__":  # pragma: no cover — manual smoke test
    df = fetch_ohlc("EURUSD", "M15", lookback_days=5)
    print(f"fetched {len(df)} rows; head:")
    print(df.head())
    print(f"tail:")
    print(df.tail())
