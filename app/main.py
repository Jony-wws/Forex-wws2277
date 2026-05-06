"""Forex Signals - FastAPI server with real-time data and order book."""
from __future__ import annotations

import json
import logging
import time
import threading
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

from .config import (
    PAIRS,
    PAIR_NAMES_RU,
    TZ_UTC5,
    MIN_CONFIDENCE,
    SCAN_INTERVAL_SEC,
    STRICT_TREND_QUALITY,
    STRICT_REQUIRE_MULTI_TF,
    MIN_FORECASTS,
)
from .prices import get_current_price, get_price_change
from .analyzer import analyze_pair
from .orderbook import get_orderbook
from . import cycle as cycle_mod

log = logging.getLogger("server")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
STATE_DIR = Path(__file__).resolve().parent.parent / "state"
ORDERBOOKS_SUMMARY_FILE = STATE_DIR / "orderbooks_summary.json"
ORDERBOOKS_SUMMARY_TTL = 60  # seconds

_signals: dict = {"pairs": {}, "updated_at": None, "scan_count": 0}
_orderbooks: dict = {}
_lock = threading.Lock()
_orderbooks_summary_cache: dict = {"ts": 0.0, "data": None}


def _build_entry(pair: str) -> dict | None:
    """Build data entry for one pair."""
    price = get_current_price(pair)
    if price is None:
        return None

    analysis = analyze_pair(pair)
    price_info = get_price_change(pair)

    is_jpy = "JPY" in pair
    pip_mult = 100 if is_jpy else 10000

    entry: dict = {
        "pair": pair,
        "name_ru": PAIR_NAMES_RU.get(pair, pair),
        "price": price,
        "price_display": f"{price:.3f}" if is_jpy else f"{price:.5f}",
    }

    if price_info:
        entry["change_24h_pips"] = round(price_info["change"] * pip_mult, 1)
        entry["change_24h_pct"] = price_info["change_pct"]
    else:
        entry["change_24h_pips"] = 0
        entry["change_24h_pct"] = 0

    if analysis:
        # `side` is the raw analyser direction; `signal` is set by the
        # strict-gate post-pass in `_apply_strict_gate` after the full
        # scan completes (we need to compare pairs across the whole
        # universe to enforce the min-3 fallback).
        entry["signal"] = None
        entry["signal_kind"] = None
        entry["side"] = analysis["side"]
        entry["confidence"] = analysis["confidence"]
        entry["strength"] = analysis["strength"]
        entry["score"] = analysis["score"]
        entry["max_score"] = analysis.get("max_score", 0)
        entry["multi_tf_aligned"] = bool(analysis.get("multi_tf_aligned"))
        entry["trend_quality"] = analysis.get("trend_quality", 0.0)
        entry["trend_quality_breakdown"] = analysis.get("trend_quality_breakdown", {})
        entry["expected_move_pips_5h"] = analysis.get("expected_move_pips_5h", 0.0)
        entry["details"] = analysis["details"]
        entry["indicators"] = analysis["indicators"]
        # Provisional forecasts from the analyser (kept only for pairs the
        # post-pass promotes to a real signal).
        entry["forecast_5h"] = analysis["forecast_5h"]
        entry["forecast_24h"] = analysis["forecast_24h"]
    else:
        entry["signal"] = None
        entry["signal_kind"] = None
        entry["side"] = None
        entry["confidence"] = 0
        entry["strength"] = "Нет данных"
        entry["score"] = 0
        entry["max_score"] = 0
        entry["multi_tf_aligned"] = False
        entry["trend_quality"] = 0.0
        entry["trend_quality_breakdown"] = {}
        entry["expected_move_pips_5h"] = 0.0
        entry["details"] = []
        entry["indicators"] = {}
        entry["forecast_5h"] = None
        entry["forecast_24h"] = None

    return entry


def _apply_strict_gate(pairs_dict: dict) -> None:
    """Enforce the strict-trend-quality gate across all 28 pairs.

    1. A pair gets a BUY/SELL `signal` only when `side` is set,
       `confidence >= MIN_CONFIDENCE`, and `trend_quality >= STRICT_TREND_QUALITY`.
    2. If fewer than `MIN_FORECASTS` pairs pass that gate, promote the
       top-N pairs by `trend_quality` (desc) so the dashboard always shows
       at least `MIN_FORECASTS` directional forecasts. Promoted entries
       are flagged with `signal_kind = "fallback"` so the UI can mark them
       as «лучший из доступных» rather than full strict signals.
    3. For every pair that does NOT end up with a `signal`, clear its
       provisional 5h/24h forecasts so the UI shows «нет сигнала» cleanly.
    """
    candidates = []
    for pair, entry in pairs_dict.items():
        side = entry.get("side")
        tq = entry.get("trend_quality", 0.0)
        conf = entry.get("confidence", 0)
        mtf = bool(entry.get("multi_tf_aligned"))
        entry["signal"] = None
        entry["signal_kind"] = None
        if side is not None:
            candidates.append((pair, entry, side, tq, conf, mtf))

    def _is_strict(c):
        _pair, _entry, _side, tq, conf, mtf = c
        if tq < STRICT_TREND_QUALITY:
            return False
        if conf < MIN_CONFIDENCE:
            return False
        if STRICT_REQUIRE_MULTI_TF and not mtf:
            return False
        return True

    strict = [c for c in candidates if _is_strict(c)]
    strict.sort(key=lambda c: c[3], reverse=True)
    strict_pairs = {c[0] for c in strict}

    promoted = list(strict)
    if len(promoted) < MIN_FORECASTS:
        rest = [c for c in candidates if c[0] not in strict_pairs]
        rest.sort(key=lambda c: c[3], reverse=True)
        promoted.extend(rest[: MIN_FORECASTS - len(promoted)])

    promoted_pairs: set[str] = set()
    for pair, entry, side, tq, _conf, _mtf in promoted:
        entry["signal"] = side
        entry["signal_kind"] = "strict" if pair in strict_pairs else "fallback"
        promoted_pairs.add(pair)

    for pair, entry in pairs_dict.items():
        if pair not in promoted_pairs:
            entry["forecast_5h"] = None
            entry["forecast_24h"] = None
            if entry.get("strength"):
                entry["strength"] = "Нет сигнала"


def _scanner_loop():
    """Background scanner - publishes each pair as it's ready."""
    log.info("Scanner started")
    scan_num = 0
    while True:
        start = time.time()
        scan_num += 1

        for pair in PAIRS:
            try:
                entry = _build_entry(pair)
                if entry:
                    now_utc5 = datetime.now(TZ_UTC5)
                    with _lock:
                        _signals["pairs"][pair] = entry
                        _signals["updated_at"] = now_utc5.strftime("%Y-%m-%d %H:%M:%S")
                        _signals["scan_count"] = scan_num
                        # Re-run the strict gate after every pair so the API
                        # stays internally consistent even while the scan is
                        # in progress (avoids races where some pairs hold
                        # stale `signal_kind` from the previous scan).
                        _apply_strict_gate(_signals["pairs"])
            except Exception as e:
                log.error(f"Error scanning {pair}: {e}")

        try:
            with _lock:
                strict_n = sum(
                    1 for e in _signals["pairs"].values()
                    if e.get("signal_kind") == "strict"
                )
                fallback_n = sum(
                    1 for e in _signals["pairs"].values()
                    if e.get("signal_kind") == "fallback"
                )
            log.info(
                f"Scan #{scan_num}: strict={strict_n} fallback={fallback_n} "
                f"total_signals={strict_n + fallback_n}"
            )
        except Exception as e:
            log.error(f"Error applying strict gate: {e}")

        # Update order books (skip first 3 scans, then every 3rd scan)
        if scan_num >= 4 and scan_num % 3 == 1:
            for pair in PAIRS:
                try:
                    ob = get_orderbook(pair)
                    with _lock:
                        _orderbooks[pair] = ob
                except Exception as e:
                    log.error(f"Error orderbook {pair}: {e}")

        # Tick the strict 5h cycle (rotates at 00/05/10/15/20 UTC,
        # evaluates expired 5h/24h forecasts, persists state).
        try:
            with _lock:
                pairs_snapshot = dict(_signals["pairs"])
            cycle_mod.tick(pairs_snapshot)
        except Exception as e:
            log.error(f"Error ticking cycle: {e}")

        elapsed = time.time() - start
        log.info(f"Scan #{scan_num}: {len(_signals['pairs'])} pairs in {elapsed:.1f}s")

        sleep_time = max(1, SCAN_INTERVAL_SEC - elapsed)
        time.sleep(sleep_time)


app = FastAPI(title="Forex Signals", version="2.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
)

# Per-IP rate limiting. Heavy, per-pair endpoints get a stricter limit.
# `/api/signals` and `/api/cycle` are lightweight (cached, in-memory) so we
# allow more headroom there.
limiter = Limiter(key_func=get_remote_address)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)


@app.on_event("startup")
async def startup():
    cycle_mod.init()
    t = threading.Thread(target=_scanner_loop, daemon=True)
    t.start()


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/", response_class=HTMLResponse)
async def index():
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    # Inject current data directly into HTML so mobile doesn't need API call
    with _lock:
        data_json = json.dumps(_signals, default=str, ensure_ascii=False)
    cycle_json = json.dumps(
        cycle_mod.snapshot(), default=str, ensure_ascii=False
    )
    inject = (
        f'<script>window.__INITIAL_DATA__ = {data_json};'
        f'window.__INITIAL_CYCLE__ = {cycle_json};</script>'
    )
    html = html.replace('</head>', inject + '</head>')
    return html


@app.get("/api/signals")
@limiter.limit("120/minute")
async def get_signals(request: Request):
    with _lock:
        return JSONResponse(content=json.loads(json.dumps(_signals, default=str)))


@app.get("/api/cycle")
@limiter.limit("120/minute")
async def get_cycle(request: Request):
    """Strict 5h-cycle snapshot: top-5 forecasts, countdown and winrate."""
    return JSONResponse(
        content=json.loads(json.dumps(cycle_mod.snapshot(), default=str))
    )


@app.get("/api/orderbook/{pair}")
@limiter.limit("60/minute")
async def get_orderbook_api(request: Request, pair: str):
    pair = pair.upper()
    with _lock:
        ob = _orderbooks.get(pair)
    if not ob:
        try:
            ob = get_orderbook(pair)
        except Exception:
            return JSONResponse(content={"error": "Нет данных"}, status_code=404)
    return JSONResponse(content=json.loads(json.dumps(ob, default=str)))


@app.get("/api/orderbooks")
@limiter.limit("60/minute")
async def get_all_orderbooks(request: Request):
    """All-pair orderbook summary.

    Cached for ``ORDERBOOKS_SUMMARY_TTL`` seconds in-memory and on disk
    (``state/orderbooks_summary.json``) so repeat hits are served
    instantly without rebuilding the snapshot.
    """
    now = time.time()
    if (
        _orderbooks_summary_cache["data"] is not None
        and now - _orderbooks_summary_cache["ts"] < ORDERBOOKS_SUMMARY_TTL
    ):
        return JSONResponse(content=_orderbooks_summary_cache["data"])

    with _lock:
        data = json.loads(json.dumps(dict(_orderbooks), default=str))

    _orderbooks_summary_cache["ts"] = now
    _orderbooks_summary_cache["data"] = data
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        ORDERBOOKS_SUMMARY_FILE.write_text(
            json.dumps(data, ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception as e:
        log.warning(f"Could not persist orderbooks summary: {e}")

    return JSONResponse(content=data)


@app.get("/api/health")
@limiter.limit("120/minute")
async def health(request: Request):
    with _lock:
        sc = _signals.get("scan_count", 0)
        ua = _signals.get("updated_at")
    return {
        "status": "ok",
        "scan_count": sc,
        "updated_at": ua,
        "time_utc5": datetime.now(TZ_UTC5).strftime("%Y-%m-%d %H:%M:%S"),
    }
