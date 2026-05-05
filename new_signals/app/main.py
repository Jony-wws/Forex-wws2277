"""Forex Signals - FastAPI server with real-time data."""
from __future__ import annotations

import asyncio
import json
import logging
import time
import threading
from datetime import datetime, timezone, timedelta
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .config import PAIRS, PAIR_NAMES_RU, TZ_UTC5, MIN_CONFIDENCE, SCAN_INTERVAL_SEC
from .prices import get_current_price, get_price_change
from .analyzer import analyze_pair

log = logging.getLogger("server")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

# Global state - updated by background scanner
_signals: dict = {"pairs": {}, "updated_at": None, "scan_count": 0}
_lock = threading.Lock()


def _scanner_loop():
    """Background thread that scans all pairs every SCAN_INTERVAL_SEC."""
    log.info("Scanner started")
    while True:
        start = time.time()
        results = {}
        for pair in PAIRS:
            try:
                price = get_current_price(pair)
                analysis = analyze_pair(pair)
                price_info = get_price_change(pair)

                if price is None:
                    continue

                # Determine pip multiplier for display
                is_jpy = "JPY" in pair
                pip_mult = 100 if is_jpy else 10000

                entry: dict = {
                    "pair": pair,
                    "name_ru": PAIR_NAMES_RU.get(pair, pair),
                    "price": price,
                    "price_display": f"{price:.3f}" if is_jpy else f"{price:.5f}",
                }

                if price_info:
                    entry["change_24h"] = price_info["change"]
                    entry["change_24h_pct"] = price_info["change_pct"]
                    entry["change_24h_pips"] = round(price_info["change"] * pip_mult, 1)
                else:
                    entry["change_24h"] = 0
                    entry["change_24h_pct"] = 0
                    entry["change_24h_pips"] = 0

                if analysis and analysis["side"] and analysis["confidence"] >= MIN_CONFIDENCE:
                    entry["signal"] = analysis["side"]
                    entry["confidence"] = analysis["confidence"]
                    entry["strength"] = analysis["strength"]
                    entry["score"] = analysis["score"]
                    entry["details"] = analysis["details"]
                    entry["indicators"] = analysis["indicators"]

                    # 5h forecast
                    entry["forecast_5h"] = {
                        "direction": analysis["side"],
                        "strength": analysis["strength"],
                        "confidence": analysis["confidence"],
                    }
                    # 24h forecast based on trend strength
                    abs_score = abs(analysis["score"])
                    if abs_score >= 10:
                        forecast_24h_dir = analysis["side"]
                        forecast_24h_strength = "Сильное движение"
                    elif abs_score >= 6:
                        forecast_24h_dir = analysis["side"]
                        forecast_24h_strength = "Умеренное движение"
                    else:
                        forecast_24h_dir = analysis["side"]
                        forecast_24h_strength = "Слабое движение"

                    entry["forecast_24h"] = {
                        "direction": forecast_24h_dir,
                        "strength": forecast_24h_strength,
                    }
                else:
                    entry["signal"] = None
                    entry["confidence"] = analysis["confidence"] if analysis else 0
                    entry["strength"] = "Нет сигнала"
                    entry["details"] = analysis["details"] if analysis else []
                    entry["indicators"] = analysis["indicators"] if analysis else {}
                    entry["forecast_5h"] = None
                    entry["forecast_24h"] = None

                results[pair] = entry

            except Exception as e:
                log.error(f"Error scanning {pair}: {e}")

        now_utc5 = datetime.now(TZ_UTC5)
        with _lock:
            _signals["pairs"] = results
            _signals["updated_at"] = now_utc5.strftime("%Y-%m-%d %H:%M:%S")
            _signals["scan_count"] = _signals.get("scan_count", 0) + 1

        elapsed = time.time() - start
        log.info(
            f"Scan #{_signals['scan_count']} complete: "
            f"{len(results)} pairs in {elapsed:.1f}s"
        )

        sleep_time = max(1, SCAN_INTERVAL_SEC - elapsed)
        time.sleep(sleep_time)


app = FastAPI(title="Forex Signals", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
async def startup():
    thread = threading.Thread(target=_scanner_loop, daemon=True)
    thread.start()
    log.info("Background scanner thread started")


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/", response_class=HTMLResponse)
async def index():
    return (STATIC_DIR / "index.html").read_text(encoding="utf-8")


@app.get("/api/signals")
async def get_signals():
    with _lock:
        data = json.loads(json.dumps(_signals, default=str))
    return JSONResponse(content=data)


@app.get("/api/health")
async def health():
    with _lock:
        scan_count = _signals.get("scan_count", 0)
        updated_at = _signals.get("updated_at")
    return {
        "status": "ok",
        "scan_count": scan_count,
        "updated_at": updated_at,
        "time_utc5": datetime.now(TZ_UTC5).strftime("%Y-%m-%d %H:%M:%S"),
    }
