"""Yahoo Finance cache warmer.

Pre-fetches OHLC data for all 28 pairs across 4 timeframes once an hour
so subsequent ``cycle_5h.py`` runs (and the live site) don't have to
wait for a cold Yahoo download.

Side-effect: Yahoo's CDN populates its caches with our typical query
shape, which makes the next live-site request faster too."""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

import yfinance as yf

PAIRS = [
    "EURUSD", "GBPUSD", "USDJPY", "USDCHF", "USDCAD", "AUDUSD", "NZDUSD",
    "EURJPY", "GBPJPY", "EURGBP", "EURCHF", "EURAUD", "EURCAD", "EURNZD",
    "GBPCHF", "GBPAUD", "GBPCAD", "GBPNZD", "AUDJPY", "AUDCHF", "AUDCAD",
    "AUDNZD", "CADJPY", "CADCHF", "CHFJPY", "NZDJPY", "NZDCHF", "NZDCAD",
]

JOBS = [
    ("15m",  "60d"),
    ("1h",   "180d"),
    ("4h",   "365d"),
    ("1d",   "365d"),
]


def warm_one(pair: str, interval: str, period: str) -> tuple[str, int]:
    try:
        df = yf.download(f"{pair}=X", interval=interval, period=period,
                         progress=False, auto_adjust=False, threads=False)
        return f"{pair}@{interval}", len(df)
    except Exception as e:
        return f"{pair}@{interval}", -1


def main() -> int:
    start = time.time()
    total = 0
    failed = 0
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = []
        for pair in PAIRS:
            for interval, period in JOBS:
                futures.append(pool.submit(warm_one, pair, interval, period))
        for fut in as_completed(futures):
            name, n = fut.result()
            if n < 0:
                failed += 1
            else:
                total += n
    elapsed = time.time() - start
    print(f"[cache-warmer] {total} bars across {len(PAIRS)*len(JOBS)} "
          f"queries in {elapsed:.1f}s ({failed} failed)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
