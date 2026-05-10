"""Forex Signals - FastAPI server with real-time data and order book."""
from __future__ import annotations

import json
import logging
import time
import threading
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

from .config import PAIRS, PAIR_NAMES_RU, TZ_UTC5, MIN_CONFIDENCE, SCAN_INTERVAL_SEC
from .prices import fetch_bars, get_current_price, get_price_change
from .analyzer import analyze_pair
from .orderbook import get_orderbook
from . import cycle as cycle_mod

log = logging.getLogger("server")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
# Built React SPA (vite build → static/dashboard/). The directory is
# created lazily at container build time by the CI frontend build step
# (see .github/workflows/deploy_fly.yml and Dockerfile). When the folder
# is absent — e.g. a local Python-only checkout without Node — we skip
# mounting /v2 entirely so the classic / and /tg dashboards still work.
DASHBOARD_DIR = STATIC_DIR / "dashboard"
STATE_DIR = Path(__file__).resolve().parent.parent / "state"
ORDERBOOKS_SUMMARY_FILE = STATE_DIR / "orderbooks_summary.json"
ORDERBOOKS_SUMMARY_TTL = 60  # seconds

# Allow-list of candlestick intervals exposed via /api/bars. Each entry
# maps an interval key to the Yahoo Finance `period` string we pass to
# fetch_bars() — picked so every timeframe returns ~150–500 bars, which
# is a sweet spot for lightweight-charts on a phone screen.
BAR_INTERVALS: dict[str, str] = {
    "15m": "5d",
    "1h": "1mo",
    "4h": "3mo",
    "1d": "1y",
}

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
        has_signal = (
            analysis["side"] is not None
            and analysis["confidence"] >= MIN_CONFIDENCE
        )
        entry["signal"] = analysis["side"] if has_signal else None
        # `side` is the raw analyser direction (independent of the 80% gate),
        # used by the strict 5h cycle to always pick top-5 even on weak markets.
        entry["side"] = analysis["side"]
        entry["confidence"] = analysis["confidence"]
        entry["strength"] = analysis["strength"] if has_signal else "Нет сигнала"
        entry["score"] = analysis["score"]
        entry["max_score"] = analysis.get("max_score", 0)
        entry["multi_tf_aligned"] = bool(analysis.get("multi_tf_aligned"))
        entry["adx_h1"] = analysis.get("adx_h1", 0.0)
        entry["adx_h4"] = analysis.get("adx_h4", 0.0)
        entry["trend_persistence_5h"] = analysis.get("trend_persistence_5h", 0.0)
        entry["trend_persistence_bars"] = analysis.get("trend_persistence_bars", 0)
        entry["is_strong_trend"] = bool(analysis.get("is_strong_trend"))
        entry["details"] = analysis["details"]
        entry["indicators"] = analysis["indicators"]
        entry["forecast_5h"] = analysis["forecast_5h"]
        entry["forecast_24h"] = analysis["forecast_24h"]
    else:
        entry["signal"] = None
        entry["side"] = None
        entry["confidence"] = 0
        entry["strength"] = "Нет данных"
        entry["score"] = 0
        entry["max_score"] = 0
        entry["multi_tf_aligned"] = False
        entry["adx_h1"] = 0.0
        entry["adx_h4"] = 0.0
        entry["trend_persistence_5h"] = 0.0
        entry["trend_persistence_bars"] = 0
        entry["is_strong_trend"] = False
        entry["details"] = []
        entry["indicators"] = {}
        entry["forecast_5h"] = None
        entry["forecast_24h"] = None

    return entry


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
            except Exception as e:
                log.error(f"Error scanning {pair}: {e}")

        # Update order books on the FIRST scan so the tab is never empty,
        # then refresh every 3rd scan to keep load light.
        if scan_num == 1 or scan_num % 3 == 1:
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


def _render_index(for_telegram: bool = False) -> str:
    """Render `static/index.html` with embedded initial data.

    When ``for_telegram`` is True we additionally inject:
      * the Telegram Web App SDK (``telegram-web-app.js``);
      * a small ``<style>`` block that maps the existing dark theme to
        Telegram theme variables so the page blends with the user's
        Telegram colour scheme without changing the standalone look.

    The page degrades gracefully in a normal browser: if
    ``window.Telegram.WebApp`` is undefined the SDK is a no-op and the
    CSS variables fall back to the existing dark palette.
    """
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    with _lock:
        data_json = json.dumps(_signals, default=str, ensure_ascii=False)
        ob_json = json.dumps(dict(_orderbooks), default=str, ensure_ascii=False)
    cycle_json = json.dumps(
        cycle_mod.snapshot(), default=str, ensure_ascii=False
    )
    inject = (
        f'<script>window.__INITIAL_DATA__ = {data_json};'
        f'window.__INITIAL_CYCLE__ = {cycle_json};'
        f'window.__INITIAL_OB__ = {ob_json};</script>'
    )
    if for_telegram:
        tg_block = (
            '<script src="https://telegram.org/js/telegram-web-app.js?56"></script>'
            '<style>'
            ':root{'
            '--bg: var(--tg-theme-bg-color, #0a0e17);'
            '--card-bg: var(--tg-theme-secondary-bg-color, #141b2d);'
            '--header-bg: var(--tg-theme-secondary-bg-color, #0d1321);'
            '--text: var(--tg-theme-text-color, #e2e8f0);'
            '--text-dim: var(--tg-theme-hint-color, #8892a4);'
            '--accent: var(--tg-theme-link-color, #4fc3f7);'
            '}'
            'html,body{overscroll-behavior-y:contain;}'
            '</style>'
            '<script>'
            'try{var tg=window.Telegram&&window.Telegram.WebApp;'
            'if(tg){tg.ready();tg.expand();'
            'document.documentElement.classList.add("tg-mini-app");}'
            '}catch(e){}'
            '</script>'
        )
        inject = tg_block + inject
    return html.replace('</head>', inject + '</head>')


@app.get("/", response_class=HTMLResponse)
async def index():
    return _render_index(for_telegram=False)


@app.get("/tg", response_class=HTMLResponse)
async def index_telegram():
    """Telegram Mini App entry point.

    Same dashboard as ``/`` but with the Telegram Web App SDK loaded and
    theme variables wired up.  Open this URL via a BotFather menu button
    or a ``web_app`` inline button to launch the dashboard inside
    Telegram without leaving the chat.
    """
    return _render_index(for_telegram=True)


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


# ---------------------------------------------------------------------------
# /api/bars/{pair} — candlestick data for the v2 React dashboard.
#
# We intentionally return the minimal OHLCV shape that
# lightweight-charts expects (unix-seconds `time`, numeric OHLC +
# volume), so the frontend can feed the array straight into
# `series.setData(...)` without any per-bar transform.
# ---------------------------------------------------------------------------

@app.get("/api/bars/{pair}")
@limiter.limit("60/minute")
async def get_bars(
    request: Request,
    pair: str,
    interval: str = Query("1h", pattern="^(15m|1h|4h|1d)$"),
):
    pair = pair.upper()
    if pair not in PAIRS:
        raise HTTPException(status_code=404, detail=f"Unknown pair {pair}")

    period = BAR_INTERVALS.get(interval)
    if period is None:
        raise HTTPException(status_code=400, detail="Unsupported interval")

    df = fetch_bars(pair, interval=interval, period=period)
    if df is None or df.empty:
        return {"pair": pair, "interval": interval, "bars": []}

    bars: list[dict] = []
    for ts, row in df.iterrows():
        # Guard against stray NaN rows — yfinance occasionally emits
        # partial bars that pandas keeps in the frame with NaN close.
        try:
            close = float(row["Close"])
        except (TypeError, ValueError):
            continue
        if close != close:  # NaN check
            continue
        bars.append({
            "time": int(ts.timestamp()),
            "open": float(row["Open"]),
            "high": float(row["High"]),
            "low": float(row["Low"]),
            "close": close,
            "volume": float(row.get("Volume", 0) or 0),
        })

    return {"pair": pair, "interval": interval, "bars": bars}


# ---------------------------------------------------------------------------
# /v2 — modern React SPA (Vite + TailwindCSS). Built artefacts live under
# static/dashboard/ and are produced by CI before the Fly.io deploy.
# When the build folder is missing (local dev without a frontend build)
# we return a helpful 404 explaining how to build it rather than an
# opaque StaticFiles error.
# ---------------------------------------------------------------------------

_DASHBOARD_INDEX = DASHBOARD_DIR / "index.html"

if _DASHBOARD_INDEX.is_file():
    # Mount hashed Vite assets (JS/CSS bundles) under /v2/assets.
    assets_dir = DASHBOARD_DIR / "assets"
    if assets_dir.is_dir():
        app.mount(
            "/v2/assets",
            StaticFiles(directory=str(assets_dir)),
            name="v2_assets",
        )

    @app.get("/v2", include_in_schema=False)
    @app.get("/v2/", include_in_schema=False)
    async def v2_root():
        return FileResponse(_DASHBOARD_INDEX)

    # SPA fallback: any unmatched /v2/<client-side-route> path (e.g.
    # /v2/pair/EURUSD, /v2/cycle) serves index.html so react-router can
    # handle the route on the client.  Note FastAPI will still match
    # /v2/assets/... first because that mount is declared above, and
    # /api/* routes are declared earlier, so we do not shadow them.
    @app.get("/v2/{path:path}", include_in_schema=False)
    async def v2_spa(path: str):
        # Serve static files that exist in the build dir verbatim
        # (favicon.ico, sitemap, etc.). Anything else falls through to
        # index.html for client-side routing.
        candidate = DASHBOARD_DIR / path
        if candidate.is_file() and candidate.resolve().is_relative_to(
            DASHBOARD_DIR.resolve()
        ):
            return FileResponse(candidate)
        return FileResponse(_DASHBOARD_INDEX)
else:
    log.warning(
        "Dashboard build not found at %s — /v2 routes disabled. "
        "Run `npm ci && npm run build` in /web to generate it.",
        DASHBOARD_DIR,
    )

    @app.get("/v2", include_in_schema=False)
    @app.get("/v2/", include_in_schema=False)
    @app.get("/v2/{path:path}", include_in_schema=False)
    async def v2_missing(path: str = ""):
        return HTMLResponse(
            "<!doctype html><html><body style='font-family:sans-serif;"
            "background:#0a0e17;color:#e2e8f0;padding:2rem'>"
            "<h1>Dashboard not built</h1>"
            "<p>The v2 React dashboard hasn't been built yet.</p>"
            "<pre>cd web && npm ci && npm run build</pre>"
            "<p>Then restart the server. "
            "Meanwhile the classic dashboard is available at "
            "<a href='/' style='color:#4fc3f7'>/</a>.</p>"
            "</body></html>",
            status_code=503,
        )
