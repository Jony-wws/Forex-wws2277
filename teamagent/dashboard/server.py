"""FastAPI dashboard для TeamAgent.

Эндпоинты:
- GET  /                       — HTML
- GET  /api/forecasts          — все 28 пар (единый источник: ПРОГНОЗЫ + мета в одном)
- GET  /api/forecast/{pair}    — детально по одной паре
- GET  /api/open-trades        — открытые сделки с live PnL и таймером (обновляется каждые 30 сек на фронте)
- GET  /api/closed-trades      — закрытые сделки + PnL
- GET  /api/stats              — total / wins / losses / WR / total PnL
- GET  /api/volume-profile/{pair} — стакан + прогноз 00:00 UTC+5
- GET  /api/agents             — состояние всех 60 агентов (heartbeat)
- GET  /api/health             — общий health-check
- POST /api/agents/{name}/restart — ручной рестарт агента
"""
from __future__ import annotations
import json
import logging
import os
import subprocess
import sys
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from .. import config
from ..data import yahoo
from .. import volume_profile as vp_mod
from .. import paper_trader
from .. import paper_trader_stakan
from .. import live_analyst as live_analyst_mod
from .. import regime as regime_mod
from .. import stakan_view as stakan_view_mod

log = logging.getLogger("dashboard")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "static"


def _flag_enabled(name: str) -> bool:
    value = os.environ.get(name)
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _seed_state_files() -> None:
    """Cold-boot seed for STATE_DIR so dashboard works before scanner's first sweep.

    Strategy:
      1. If we're on a fresh persistent volume (TEAMAGENT_STATE_DIR set and
         empty), copy any shipped state from the repo (/app/teamagent/state)
         so the dashboard renders real data immediately after deploy.
      2. Then fill in the minimal placeholder schemas for any file still
         missing.
    """
    import shutil

    # Repo-shipped state (always under <package_dir>/state regardless of override).
    shipped = Path(__file__).resolve().parent.parent / "state"
    target = config.STATE_DIR

    if target != shipped and target.exists():
        try:
            existing = list(target.glob("*.json"))
            if not existing and shipped.exists():
                shipped_files = list(shipped.glob("*.json"))
                for src in shipped_files:
                    dst = target / src.name
                    try:
                        shutil.copy2(src, dst)
                    except Exception as e:
                        log.warning(f"[seed] copy {src.name} failed: {e}")
                if shipped_files:
                    log.info(
                        f"[seed] bootstrapped {len(shipped_files)} state files "
                        f"from {shipped} → {target}"
                    )
        except Exception as e:
            log.warning(f"[seed] bootstrap probe failed: {e}")

    seeds = {
        "forecasts.json": {"forecasts": {}, "rankings": [], "scanned_at": None},
        "market_radar.json": {"pairs": {}, "scanners": [], "as_of": None},
        "cot.json": {"currencies": {}, "as_of": None},
        "open_trades.json": {"trades": []},
        "stakan_open_trades_enriched.json": {"trades": []},
        "stakan_signals.json": {"signals": [], "as_of": None},
        "daily_signals.json": {"signals": [], "as_of": None},
    }
    for fn, payload in seeds.items():
        fp = target / fn
        if not fp.exists():
            try:
                fp.write_text(json.dumps(payload, ensure_ascii=False))
                log.info(f"[seed] {fn}")
            except Exception as e:
                log.warning(f"[seed] {fn} failed: {e}")


def _spawn_supervisor_processes() -> list[subprocess.Popen]:
    """Spawn supporting processes alongside the FastAPI dashboard.

    Three modes (selected via env vars):

    * DASHBOARD_ONLY=1       → spawn nothing. Dashboard only. Useful for local
      dev where you start scanner/paper_trader manually.
    * FLY_MINIMAL=1          → spawn ONLY core trading loop (forecast_scanner +
      paper_trader_daily). Lightweight, fits in a free-tier Fly machine. The
      60+ subprocess agents are skipped — they only run on the Devin VM.
    * FLY_FULL=1             → spawn full orchestrator + watchdog even on Fly.
    * (default, e.g. on a Devin VM) → spawn full orchestrator + watchdog. The
      orchestrator itself fans out to forecast_scanner, paper_traders, 60
      agents, etc.
    """
    if _flag_enabled("DASHBOARD_ONLY"):
        log.info("DASHBOARD_ONLY=1 — skipping background processes")
        return []
    children: list[subprocess.Popen] = []
    log_dir = config.LOGS_DIR
    log_dir.mkdir(parents=True, exist_ok=True)

    # Auto-detect Fly.io: presence of /data mount + FLY_APP_NAME env var.
    on_fly = os.environ.get("FLY_APP_NAME") is not None or Path("/data").is_dir()
    fly_full = _flag_enabled("FLY_FULL")
    if on_fly and not fly_full:
        # Default Fly machine = 256 MB → cannot fit orchestrator + 60 agents.
        # Dashboard-only mode reads state files committed by the hourly Devin
        # schedule (sched-…); for live scanning use a Fly machine with ≥1 GB.
        log.info("on-fly default-memory mode → dashboard-only (no scanner spawn)")
        return []
    if _flag_enabled("FLY_MINIMAL") and not fly_full:
        modules = (
            "teamagent.forecast_scanner",
            "teamagent.paper_trader_daily",
        )
        log.info(
            f"FLY_MINIMAL=1 — spawning {len(modules)} core processes only"
        )
    else:
        modules = (
            "teamagent.orchestrator",
            "teamagent.watchdog",
        )

    for mod in modules:
        try:
            stem = mod.split(".")[-1]
            out = open(log_dir / f"{stem}.out", "ab", buffering=0)
            err = open(log_dir / f"{stem}.err", "ab", buffering=0)
            p = subprocess.Popen(
                [sys.executable, "-m", mod],
                stdout=out,
                stderr=err,
                env={**os.environ, "PYTHONUNBUFFERED": "1"},
            )
            log.info(f"[spawn] {mod} pid={p.pid}")
            children.append(p)
        except Exception as e:
            log.exception(f"[spawn] {mod} failed: {e}")
    return children


async def _fly_state_refresher():
    """Lightweight forecast-scanner loop for the Fly dashboard-only deployment.

    Default Fly-machine mode skips the heavy orchestrator (см. AGENTS.md
    "Deployment & permanent URL"). Без этого forecasts.json только
    обновляется раз в час Devin-расписанием — пользователь видит "залежалость
    state" в /api/system-audit. Тут раз в 10 минут запускаем scan_all_pairs
    asynchronously (в thread pool, чтобы не блокировать FastAPI).
    """
    on_fly = os.environ.get("FLY_APP_NAME") is not None or Path("/data").is_dir()
    if not on_fly:
        return  # Devin VM has its own scanner; nothing to do here.
    if os.environ.get("FLY_FULL") == "1" or os.environ.get("FLY_MINIMAL") == "1":
        return  # full mode already runs scanner as subprocess.
    if os.environ.get("FLY_DASHBOARD_REFRESH") == "0":
        return  # explicit opt-out.

    import asyncio
    from concurrent.futures import ThreadPoolExecutor
    interval = int(os.environ.get("FLY_REFRESH_SEC", str(10 * 60)))
    pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="fly-refresh")

    async def _tick():
        loop = asyncio.get_running_loop()
        try:
            from .. import forecast_scanner
        except ImportError:
            from teamagent import forecast_scanner
        # First refresh: 30 sec after boot — give the request loop time to
        # serve initial requests before saturating Yahoo.
        await asyncio.sleep(30)
        while True:
            try:
                log.info("[fly-refresh] scan_all_pairs() starting")
                await loop.run_in_executor(pool, forecast_scanner.scan_all_pairs)
                log.info("[fly-refresh] scan_all_pairs() done")
            except Exception as e:
                log.exception(f"[fly-refresh] failed: {e}")
            await asyncio.sleep(interval)

    return asyncio.create_task(_tick())


async def _fly_paper_trader_tick():
    """Lightweight in-process paper-trader tick for Fly dashboard-only deployments.

    The default Fly machine is 256 MB and skips the orchestrator (so paper_trader
    isn't a separate subprocess). Without this tick, /api/open-trades stays empty
    even when forecasts.json has 70%+ signals — exactly what the user reported.

    cycle_once() reads forecasts.json + open_trades.json + closed_trades.json,
    settles expired open trades against Yahoo, and opens new ones for any 70%+
    signals (subject to news / correlation / ensemble filters). Runs every 60 sec
    by default — opt out with FLY_PAPER_TRADER=0.
    """
    on_fly = os.environ.get("FLY_APP_NAME") is not None or Path("/data").is_dir()
    if not on_fly:
        return  # Devin VM has its own paper_trader; nothing to do here.
    if os.environ.get("FLY_FULL") == "1":
        return  # full mode runs paper_trader as subprocess.
    if os.environ.get("FLY_PAPER_TRADER") == "0":
        return  # explicit opt-out.

    import asyncio
    from concurrent.futures import ThreadPoolExecutor
    interval = int(os.environ.get("FLY_PAPER_TICK_SEC", "60"))
    pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="fly-paper")

    async def _tick():
        loop = asyncio.get_running_loop()
        # First tick: 45 sec after boot — give state-refresher and Yahoo cache
        # a head start. cycle_once() is idempotent so running too early is safe,
        # but we want forecasts.json to be fresh first.
        await asyncio.sleep(45)
        while True:
            try:
                from .. import paper_trader
                result = await loop.run_in_executor(pool, paper_trader.cycle_once)
                log.info(
                    f"[fly-paper] tick: opened={result.get('opened')} "
                    f"settled={result.get('settled')} "
                    f"open_now={(result.get('stats') or {}).get('open')} "
                    f"wr={(result.get('stats') or {}).get('win_rate_pct')}%"
                )
            except Exception as e:
                log.exception(f"[fly-paper] tick failed: {e}")
            await asyncio.sleep(interval)

    return asyncio.create_task(_tick())


@asynccontextmanager
async def lifespan(app: FastAPI):
    """FastAPI lifespan: seed state + spawn orchestrator/watchdog on startup,
    terminate them on shutdown. Works both in local dev and on Fly.io.

    On Fly's default 256-MB machine (dashboard-only) we ALSO spin up TWO
    lightweight in-process tasks:
      1. _fly_state_refresher() — re-runs forecast_scanner every 10 min so
         forecasts.json stays fresh between hourly Devin-VM schedules.
      2. _fly_paper_trader_tick() — calls paper_trader.cycle_once() every
         60 sec so trades are actually opened from the live forecasts and
         expired ones are settled. Without this the dashboard shows 70%+
         signals but no new trades — exactly the bug the user reported.
    """
    _seed_state_files()
    children = _spawn_supervisor_processes()
    refresh_task = None
    paper_task = None
    try:
        refresh_task = await _fly_state_refresher()
    except Exception as e:
        log.exception(f"[fly-refresh] startup failed: {e}")
    try:
        paper_task = await _fly_paper_trader_tick()
    except Exception as e:
        log.exception(f"[fly-paper] startup failed: {e}")
    try:
        yield
    finally:
        for task in (refresh_task, paper_task):
            if task is not None:
                try:
                    task.cancel()
                except Exception:
                    pass
        for p in children:
            try:
                p.terminate()
            except Exception:
                pass


app = FastAPI(title="TeamAgent Dashboard", lifespan=lifespan)

# CORS — allow the static CDN mirror (and any future custom domain) to proxy
# every /api/* request through to this live backend. Without this, browsers
# block cross-origin fetches and the static mirror falls back to its frozen
# baked JSON, which lies about market-status/forecasts.
#
# Allowed origins:
#   * https://*.devinapps.com  — the static-build deploy host
#   * https://fxinvestment.fly.dev (and *.fly.dev)  — same-host self-call
#   * http://localhost:* / http://127.0.0.1:*       — local dev
# We use allow_origin_regex to cover the wildcard subdomains.
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=(
        r"^https?://"
        r"(localhost|127\.0\.0\.1)(:\d+)?$"
        r"|^https://([a-z0-9-]+\.)*devinapps\.com$"
        r"|^https://([a-z0-9-]+\.)*fly\.dev$"
    ),
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
    expose_headers=["X-FX-Source"],
    max_age=600,
)

app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")


def _load(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text())
    except Exception:
        return default


@app.get("/")
def root():
    """FX INVESTMENT — cinematic per-pair market intent landing.
    Use /system для полного аудита/heartbeats."""
    return FileResponse(str(STATIC / "intent.html"))


@app.get("/intent")
def intent_page():
    return FileResponse(str(STATIC / "intent.html"))


@app.get("/system")
def system_page():
    """Полный системный дашборд под брендом FX INVESTMENT — аудит, heartbeats, журналы."""
    return FileResponse(str(STATIC / "index.html"))


@app.get("/agents")
def agents_page():
    """Quick deep-link → /system со скроллом к секции агентов."""
    return RedirectResponse(url="/system#agents-section", status_code=302)


@app.get("/trades")
def trades_page():
    """Единая страница 'Сделки' — все open + closed в одном месте.

    Юзер просил: 'один единственный место где будет собрано все истории сделки
    и все открытые сделки только одна мест на отдельном разделе'. Это оно.
    """
    return FileResponse(str(STATIC / "trades.html"))


@app.get("/history")
def history_page():
    """Раньше скроллилось внутрь /system; теперь сразу ведёт на единую /trades."""
    return RedirectResponse(url="/trades", status_code=302)


@app.get("/api/_debug")
def api_debug():
    """Diagnostic endpoint to check container layout / state availability."""
    import shutil
    import teamagent
    pkg = Path(teamagent.__file__).resolve().parent
    candidates = [
        Path("/app/teamagent/state"),
        pkg / "state",
        Path("/app/state"),
        config.STATE_DIR,
    ]
    info = {
        "STATE_DIR": str(config.STATE_DIR),
        "pkg_dir": str(pkg),
        "cwd": str(Path.cwd()),
    }
    for c in candidates:
        if c.exists():
            info[str(c)] = {
                "exists": True,
                "files": [p.name for p in sorted(c.glob("*.json"))[:10]],
                "count": len(list(c.glob("*.json"))),
            }
        else:
            info[str(c)] = {"exists": False}
    return info


_INTENT_BARS_DISK_TTL = 60 * 60  # 1h
_INTENT_BARS_NET_TIMEOUT = 1.8   # sec — never block a worker longer than this


def _intent_bars_disk_path(pair: str, interval: str, n: int) -> Path:
    cache_dir = config.STATE_DIR / "_intent_bars_cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir / f"{pair}_{interval}_{n}.json"


def _intent_bars_load_disk(pair: str, interval: str, n: int):
    fp = _intent_bars_disk_path(pair, interval, n)
    if not fp.exists():
        return None
    try:
        if time.time() - fp.stat().st_mtime > _INTENT_BARS_DISK_TTL * 12:
            # >12 h stale — refuse to serve very old data; let frontend show empty
            return None
        return json.loads(fp.read_text())
    except Exception:
        return None


def _intent_bars_save_disk(pair: str, interval: str, n: int, payload: dict) -> None:
    try:
        fp = _intent_bars_disk_path(pair, interval, n)
        fp.write_text(json.dumps(payload, ensure_ascii=False))
    except Exception:
        pass


from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutTimeout
# Долгоживущий пул — не блокирует выход handler-а если yfinance не успел
# (если использовать `with ThreadPoolExecutor()` то __exit__ ждёт thread.join,
# что сводит наш timeout на нет).
_INTENT_BARS_EXECUTOR = ThreadPoolExecutor(max_workers=4, thread_name_prefix="intentbars")


def _intent_bars_fetch_yahoo(pair: str, interval: str, n: int) -> dict | None:
    """Fetch from Yahoo with a hard wall-clock budget.

    Returns None on timeout / any failure so the caller can fall back to disk
    cache. NEVER raises. Bounded to _INTENT_BARS_NET_TIMEOUT sec.
    """
    def _do_fetch():
        df = yahoo.latest_bars(pair, interval=interval, n=n)
        if df is None or df.empty:
            return None
        out = []
        for ts, row in df.iterrows():
            out.append({
                "time": int(ts.timestamp()),
                "open": float(row["Open"]),
                "high": float(row["High"]),
                "low": float(row["Low"]),
                "close": float(row["Close"]),
            })
        return out

    try:
        fut = _INTENT_BARS_EXECUTOR.submit(_do_fetch)
        bars = fut.result(timeout=_INTENT_BARS_NET_TIMEOUT)
        if not bars:
            return None
        return {"pair": pair, "interval": interval, "bars": bars}
    except FutTimeout:
        log.info(f"intent-bars timeout pair={pair} tf={interval} (>{_INTENT_BARS_NET_TIMEOUT}s)")
        return None
    except Exception as e:
        log.warning(f"intent-bars yahoo failed pair={pair}: {e}")
        return None


@app.get("/api/intent-bars/{pair}")
def api_intent_bars(pair: str, interval: str = "15m", n: int = 96):
    """Лёгкие OHLC-бары для cinematic chart.

    Стратегия: всегда отвечаем за < 2 секунд. Если Yahoo не успевает —
    отдаём disk-кэш в /data/state/_intent_bars_cache (1h TTL → fresh, 12h
    TTL → still ok stale). Гарантирует что worker pool не блокируется на
    медленные yfinance вызовы — и значит /system / /history / любой
    другой запрос всегда отвечает быстро.
    """
    pair = pair.upper()
    if pair not in config.PAIRS:
        return JSONResponse({"error": f"unknown pair {pair}"}, status_code=404)
    if interval not in {"1m", "5m", "15m", "1h", "4h", "1d"}:
        return JSONResponse({"error": "bad interval"}, status_code=400)
    n = max(20, min(int(n), 300))

    cached = _intent_bars_load_disk(pair, interval, n)
    fresh_threshold = time.time() - _INTENT_BARS_DISK_TTL
    fp = _intent_bars_disk_path(pair, interval, n)
    is_fresh = fp.exists() and fp.stat().st_mtime > fresh_threshold

    if cached and is_fresh:
        return cached  # fast path: disk cache <1h old → no network call

    fresh = _intent_bars_fetch_yahoo(pair, interval, n)
    if fresh:
        _intent_bars_save_disk(pair, interval, n, fresh)
        return fresh

    if cached:
        return cached  # network failed/timed out → serve stale disk cache

    return JSONResponse({"pair": pair, "interval": interval, "bars": []})


@app.get("/api/forecasts")
def api_forecasts():
    """Единый источник: PROGNOZY-28 = мета-голосование (всё в одном).

    Возвращает и rankings (выжимка для таблицы), и forecasts (полный dict),
    чтобы фронт мог прямо из одного запроса взять agents_for_count/against_count.
    """
    snap = _load(config.STATE_DIR / "forecasts.json", {"forecasts": {}, "rankings": []})
    # Расширенная по-парамная инфа + сжатый набор ключевых indicators (нужны
    # cinematic Market-Intent карточкам без второго round-trip).
    forecasts_lite = {}
    for pair, f in (snap.get("forecasts") or {}).items():
        ind_full = f.get("indicators") or {}
        ind_compact = {}
        for tf in ("4H", "1H", "15m"):
            ind_tf = ind_full.get(tf) or {}
            if not ind_tf:
                continue
            ind_compact[tf] = {
                k: ind_tf.get(k)
                for k in (
                    "rsi14", "ema20", "ema50", "ema200", "atr14",
                    "bb_pct", "mom5", "cei10", "ofi10", "vwap", "bbp", "close",
                )
                if k in ind_tf
            }
        forecasts_lite[pair] = {
            "pair": f.get("pair"),
            "side": f.get("side"),
            "probability_pct": f.get("probability_pct"),
            "probability": f.get("probability"),
            "score": f.get("score"),
            "max_score": f.get("max_score"),
            "current_price": f.get("current_price"),
            "indicators": ind_compact,
            "agents_for_count": f.get("agents_for_count", len(f.get("agents_for", []))),
            "agents_against_count": f.get("agents_against_count", len(f.get("agents_against", []))),
            "recommended_hours": f.get("recommended_hours"),
            "as_of": f.get("as_of"),
        }
    return JSONResponse({
        "as_of": snap.get("scanned_at"),
        "scanned_at": snap.get("scanned_at"),
        "rankings": snap.get("rankings", []),
        "forecasts": forecasts_lite,
        "total_pairs": len(config.PAIRS),
    })


@app.get("/api/backtest")
def api_backtest():
    """30-дневный backtest WR по каждой паре (baseline-вариант).

    Это baseline-стратегия. Главный gate сейчас — strategy_config (см. ниже).
    """
    return _load(
        config.STATE_DIR / "backtest_30d.json",
        {"as_of": None, "pairs": {}, "summary": {}},
    )


@app.get("/api/strategy-config")
def api_strategy_config():
    """Лучшая стратегия для каждой пары + её WR на 30-дневном бэктесте.

    Это РЕАЛЬНЫЙ gate paper_trader-а:
      - сделка открывается только если best_variant.win_rate_pct ≥ 70
      - И текущий сигнал проходит фильтры этого варианта (сессия, |score|, prob).
    """
    return _load(
        config.STATE_DIR / "strategy_config.json",
        {"as_of": None, "pairs": {}, "summary": {}},
    )


@app.get("/api/playbook")
def api_playbook():
    """Per-(pair × session × regime) playbook. 28×4×4 = 448 ячеек.

    Каждая ячейка имеет:
      - status: STORM_PROOF | QUALIFIED | PROBABLE | FROZEN | INSUFFICIENT
      - wr_pct, wilson_lower_pct, n_trades, side_bias
      - worst_30d_wr_pct + storm_proof flag (резистентность к кризисам)
    """
    return _load(
        config.STATE_DIR / "playbook.json",
        {
            "as_of": None,
            "summary": {
                "total_cells": 0,
                "storm_proof": 0,
                "qualified": 0,
                "probable": 0,
                "frozen": 0,
                "insufficient": 0,
                "note": "playbook not built yet — run `python -m teamagent.playbook`",
            },
            "cells": [],
            "pairs": {},
        },
    )


@app.get("/api/analyst/{pair}")
def api_analyst(pair: str):
    """🧠 Живой AI-аналитик для одной пары — мысли в реальном времени."""
    pair = pair.upper()
    if pair not in config.PAIRS:
        raise HTTPException(404, f"unknown pair: {pair}")
    try:
        return live_analyst_mod.live_analyst(pair)
    except Exception as e:
        log.exception(f"analyst {pair} failed: {e}")
        raise HTTPException(500, f"analyst error: {e}")


@app.get("/api/analyst")
def api_analyst_all():
    """🧠 Живой AI-аналитик ПО ВСЕМ 28 парам сразу — для UI-карусели."""
    try:
        items = live_analyst_mod.live_analyst_all()
    except Exception as e:
        log.exception(f"analyst-all failed: {e}")
        raise HTTPException(500, f"analyst-all error: {e}")
    return JSONResponse({
        "as_of": datetime.now(timezone.utc).isoformat(),
        "count": len(items),
        "items": items,
    })


@app.get("/api/regime/{pair}")
def api_regime(pair: str):
    """Текущий режим пары (Hurst + ATR%-percentile + EMA-stack)."""
    pair = pair.upper()
    if pair not in config.PAIRS:
        raise HTTPException(404, f"unknown pair: {pair}")
    try:
        bars = yahoo.fetch(pair, interval="1h", period="3mo")
    except Exception as e:
        raise HTTPException(503, f"yahoo error: {e}")
    if bars is None or bars.empty:
        return {"pair": pair, "regime": None, "note": "no bars"}
    return {"pair": pair, **regime_mod.regime_summary(bars)}


@app.get("/api/daily-target")
def api_daily_target():
    """Дневной таргет: 5 сделок/день на каждую из 28 пар.

    Считает сделки (paper_trader открытые/закрытые сегодня UTC) per pair.
    Поле missing — на сколько недобираем до 5.
    """
    today = datetime.now(timezone.utc).date()
    closed = _load(config.STATE_DIR / "closed_trades.json", [])
    open_trades = _load(config.STATE_DIR / "open_trades.json", [])
    counts: dict[str, int] = {p: 0 for p in config.PAIRS}
    for t in list(closed) + list(open_trades):
        opened_at = t.get("opened_at") or t.get("ts_open") or ""
        if not opened_at:
            continue
        try:
            d = datetime.fromisoformat(opened_at.replace("Z", "+00:00")).astimezone(timezone.utc).date()
        except Exception:
            continue
        if d == today:
            pair = t.get("pair")
            if pair in counts:
                counts[pair] += 1
    items = []
    target = 5
    for p in config.PAIRS:
        c = counts.get(p, 0)
        items.append({
            "pair": p,
            "count": c,
            "target": target,
            "missing": max(0, target - c),
            "on_target": c >= target,
            "pct": min(100.0, c / target * 100.0) if target else 0.0,
        })
    on_target = sum(1 for x in items if x["on_target"])
    return JSONResponse({
        "as_of": datetime.now(timezone.utc).isoformat(),
        "date_utc": today.isoformat(),
        "target_per_pair": target,
        "on_target_count": on_target,
        "total_pairs": len(config.PAIRS),
        "items": items,
    })


@app.get("/api/forecast/{pair}")
def api_forecast(pair: str):
    pair = pair.upper()
    snap = _load(config.STATE_DIR / "forecasts.json", {"forecasts": {}})
    f = snap.get("forecasts", {}).get(pair)
    if f is None:
        raise HTTPException(404, f"forecast for {pair} not found")
    return f


@app.get("/api/open-trades")
def api_open_trades():
    """Открытые сделки с live-обогащением. На фронте обновляется каждые 30 сек."""
    open_trades = _load(config.STATE_DIR / "open_trades.json", [])
    enriched = []
    for t in open_trades:
        if t.get("status") != "open":
            continue
        enriched.append(paper_trader._enrich_open_trade(t))
    return JSONResponse({
        "as_of": datetime.now(timezone.utc).isoformat(),
        "count": len(enriched),
        "trades": enriched,
    })


@app.get("/api/closed-trades")
def api_closed_trades(limit: int = 100):
    closed = _load(config.STATE_DIR / "closed_trades.json", [])
    closed = sorted(closed, key=lambda t: t.get("close_time", ""), reverse=True)
    return JSONResponse({
        "as_of": datetime.now(timezone.utc).isoformat(),
        "count": len(closed),
        "trades": closed[:limit],
    })


@app.get("/api/stats")
def api_stats():
    return _load(config.STATE_DIR / "paper_stats.json", {
        "total": 0, "wins": 0, "losses": 0,
        "win_rate_pct": 0.0, "total_pnl_usd": 0.0,
    })


@app.get("/api/volume-profile/{pair}")
def api_volume_profile(pair: str):
    pair = pair.upper()
    if pair not in config.PAIRS:
        raise HTTPException(400, f"unknown pair {pair}")
    return vp_mod.build(pair)


# ────────── СТАКАН view (Order Book / Market Depth, added 2026-05-04) ──────────
# Объединяет volume_profile + forecast + 24h bias + 1-5h main forecast + buyers/
# sellers split + per-(pair, session) стратегию в один JSON для нового раздела
# «СТАКАН» на главной странице. Источник правды — те же state/*.json, что
# уже обновляются forecast_scanner-ом раз в 5 мин; на фронте опрашивается
# каждые 10 сек по требованию пользователя ("должно быть точно как
# tradingview, обновлять каждые 10 секунд").

@app.get("/api/stakan-view/{pair}")
def api_stakan_view_pair(pair: str):
    pair = pair.upper()
    if pair not in config.PAIRS:
        raise HTTPException(400, f"unknown pair {pair}")
    return stakan_view_mod.build_view(pair)


@app.get("/api/stakan-view")
def api_stakan_view_all():
    """Компактный snapshot 28 пар для selector-сетки в разделе СТАКАН."""
    return stakan_view_mod.build_all_summary()


# ─── Лёгкий live-price endpoint для 5-секундного refresh (2026-05-04) ─────────
# Пользователь явно попросил «текущая цена обновляется каждые 5 секунд». Чтобы
# не дёргать тяжёлый /api/stakan-view/{pair} (агрегирует volume_profile +
# forecast + macro + COT) каждые 5 сек на каждом клиенте — отдаём ТОЛЬКО цену
# из Yahoo с in-process TTL-кэшем (3 сек). Это эквивалент TradingView ticker.
_LIVE_PRICE_CACHE: dict[str, dict] = {}
_LIVE_PRICE_TTL_SEC = 3.0

@app.get("/api/live-price/{pair}")
def api_live_price(pair: str):
    """{ pair, price, change_1m, change_5m, change_1h, ts, source }.
    TTL-кэш 3 сек: если 5 клиентов опрашивают каждые 5 сек, реально дёргаем Yahoo
    максимум раз в 3 сек.
    """
    pair = pair.upper()
    if pair not in config.PAIRS:
        raise HTTPException(400, f"unknown pair {pair}")
    now_ts = time.time()
    cached = _LIVE_PRICE_CACHE.get(pair)
    if cached and now_ts - cached["_cached_at"] < _LIVE_PRICE_TTL_SEC:
        return cached["data"]
    try:
        df = yahoo.fetch(pair, interval="1m", period="1d")
        if df is None or df.empty:
            data_obj = {"pair": pair, "price": None, "error": "no_data"}
        else:
            close_series = df["Close"]
            last = float(close_series.iloc[-1])
            def _delta_pips(idx_back: int) -> float | None:
                if len(close_series) <= idx_back:
                    return None
                prev = float(close_series.iloc[-1 - idx_back])
                pip_size = 0.01 if "JPY" in pair else 0.0001
                return round((last - prev) / pip_size, 1)
            data_obj = {
                "pair": pair,
                "price": last,
                "change_1m_pips": _delta_pips(1),
                "change_5m_pips": _delta_pips(5),
                "change_1h_pips": _delta_pips(60),
                "ts": datetime.now(timezone.utc).isoformat(),
                "source": "yahoo_1m",
                "bar_time": str(close_series.index[-1]) if len(close_series) else None,
            }
    except Exception as e:
        data_obj = {"pair": pair, "price": None, "error": str(e)}
    _LIVE_PRICE_CACHE[pair] = {"_cached_at": now_ts, "data": data_obj}
    return data_obj


@app.get("/api/news-watch/{pair}")
def api_news_watch(pair: str, hours_ahead: int = 5):
    """Высокоимпактные новости/макроданные в горизонте 1–5 часов для пары.
    Если есть событие в горизонте сделки — UI показывает красное предупреждение.
    Источник: ForexFactory RSS (free, no API key).
    """
    pair = pair.upper()
    if pair not in config.PAIRS:
        raise HTTPException(400, f"unknown pair {pair}")
    try:
        from ..data import news as news_mod
        events = news_mod.upcoming_high_impact(pair, hours_ahead=hours_ahead)
        return {
            "pair": pair,
            "hours_ahead": hours_ahead,
            "as_of": datetime.now(timezone.utc).isoformat(),
            "count": len(events),
            "events": events,
            "warning": (
                f"⚠️ через ≤{hours_ahead}ч выходит {len(events)} high-impact "
                f"событий по {pair} — могут развернуть прогноз"
            ) if events else None,
        }
    except Exception as e:
        return {"pair": pair, "error": str(e), "events": []}


# ────────── Strategy "Стакан" (parallel system, added 2026-05-01) ──────────

@app.get("/api/stakan/open-trades")
def api_stakan_open():
    """Открытые сделки стратегии 'Стакан' с live PnL и таймером."""
    open_trades = _load(config.STATE_DIR / "stakan_open_trades.json", [])
    enriched = []
    for t in open_trades:
        if t.get("status") != "open":
            continue
        enriched.append(paper_trader_stakan._enrich_open_trade(t))
    return JSONResponse({
        "as_of": datetime.now(timezone.utc).isoformat(),
        "count": len(enriched),
        "trades": enriched,
    })


@app.get("/api/stakan/closed-trades")
def api_stakan_closed(limit: int = 100):
    closed = _load(config.STATE_DIR / "stakan_closed_trades.json", [])
    closed = sorted(closed, key=lambda t: t.get("close_time", ""), reverse=True)
    return JSONResponse({
        "as_of": datetime.now(timezone.utc).isoformat(),
        "count": len(closed),
        "trades": closed[:limit],
    })


@app.get("/api/stakan/stats")
def api_stakan_stats():
    return _load(config.STATE_DIR / "stakan_stats.json", {
        "strategy": "stakan",
        "total": 0, "wins": 0, "losses": 0,
        "win_rate_pct": 0.0, "total_pnl_usd": 0.0,
    })


@app.get("/api/stakan/signals")
def api_stakan_signals():
    """Последний скан с разбором по парам: какие уровни найдены, сколько голосов
    набрала каждая пара, почему не открыли (если не открыли)."""
    return _load(config.STATE_DIR / "stakan_signals.json", {
        "as_of": None,
        "min_votes_required": 7,
        "max_votes": 10,
        "signals": [],
        "note": "ещё не считали — жди первого tick paper_trader_stakan",
    })


@app.get("/api/daily/open-trades")
def api_daily_open():
    """Открытые сделки 'Лучший прогноз дня' (paper_trader_daily)."""
    return _load(config.STATE_DIR / "daily_open_trades.json", [])


@app.get("/api/daily/closed-trades")
def api_daily_closed():
    return _load(config.STATE_DIR / "daily_closed_trades.json", [])


@app.get("/api/daily/stats")
def api_daily_stats():
    return _load(config.STATE_DIR / "daily_stats.json", {
        "strategy": "daily",
        "total": 0, "wins": 0, "losses": 0,
        "win_rate_pct": 0.0, "total_pnl_usd": 0.0,
    })


@app.get("/api/daily/signals")
def api_daily_signals():
    """Последний скан 'Лучшего прогноза дня': по каждой паре meta-score
    (компоненты: forecast, radar, stakan, reversal, macro, COT) + результат."""
    return _load(config.STATE_DIR / "daily_signals.json", {
        "as_of": None, "signals": [],
        "note": "ещё не считали — жди первого daily sweep",
    })


@app.get("/api/daily/paused")
def api_daily_paused():
    """Пары на auto-pause (rolling 20-trade WR < 60%)."""
    return _load(config.STATE_DIR / "daily_paused_pairs.json", {})


@app.get("/api/microstructure/{pair}")
def api_microstructure(pair: str):
    """PRO-уровень: «что происходит ВНУТРИ рынка» по конкретной паре.
    Возвращает: cumulative_delta, footprint grid, SMC (order_blocks/FVG/liquidity_sweeps),
    Wyckoff stage, whale activity, Hurst exponent + summary (inner_facts/outer_view).
    Считается онлайн (тяжёлый запрос — ~1-3 сек), не кэшируется в state."""
    try:
        from .. import market_microstructure as _ms  # noqa
    except ImportError:
        from teamagent import market_microstructure as _ms
    pair = pair.upper()
    if pair not in config.PAIRS:
        return {"error": f"unknown pair {pair}", "valid_pairs": config.PAIRS}
    try:
        return _ms.analyze(pair) or {"error": "no data"}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


@app.get("/api/microstructure")
def api_microstructure_all():
    """Краткая сводка microstructure по всем 28 парам (для overview таблицы).
    Считает только Wyckoff stage + cumulative_delta bias + Hurst regime
    (быстрая часть payload, ~1 сек на пару)."""
    try:
        from teamagent import market_microstructure as _ms
    except ImportError:
        return {"error": "microstructure module not available"}
    out = {}
    for pair in config.PAIRS:
        try:
            r = _ms.analyze(pair) or {}
            out[pair] = {
                "wyckoff_stage": (r.get("wyckoff") or {}).get("stage"),
                "wyckoff_confidence": (r.get("wyckoff") or {}).get("confidence"),
                "delta_bias": (r.get("cumulative_delta") or {}).get("bias"),
                "delta_norm_pct": (r.get("cumulative_delta") or {}).get("norm_pct"),
                "hurst_H": (r.get("hurst") or {}).get("H"),
                "hurst_regime": (r.get("hurst") or {}).get("regime"),
                "n_order_blocks": len(r.get("order_blocks") or []),
                "n_fvgs": len(r.get("fair_value_gaps") or []),
                "n_sweeps": len(r.get("liquidity_sweeps") or []),
                "n_whales": len(r.get("whales") or []),
                "inner_facts": (r.get("summary") or {}).get("inner_facts") or [],
                "outer_view": (r.get("summary") or {}).get("outer_view") or [],
            }
        except Exception as e:
            out[pair] = {"error": f"{type(e).__name__}: {e}"}
    return {"as_of": datetime.now(timezone.utc).isoformat(), "pairs": out}


@app.get("/api/market-radar")
def api_market_radar():
    """«Военный радар» рынка: 20+ независимых сканеров × 28 пар.
    Каждая пара получает overall_score [-100..+100] (положительный = BUY-bias),
    direction (BUY/SELL/NEUTRAL), и breakdown по каждому сканеру."""
    return _load(
        config.STATE_DIR / "market_radar.json",
        {"as_of": None, "pairs": {}, "scanners": [],
         "scanner_count": 0,
         "note": "ещё не считали — жди первого tick market_radar"},
    )


@app.get("/api/market-radar/{pair}")
def api_market_radar_pair(pair: str):
    pair = pair.upper()
    full = _load(config.STATE_DIR / "market_radar.json", {"pairs": {}})
    pair_data = (full.get("pairs") or {}).get(pair)
    if not pair_data:
        return JSONResponse({"pair": pair, "error": "no data"}, status_code=404)
    return JSONResponse({
        "pair": pair,
        "as_of": full.get("as_of"),
        **pair_data,
    })


@app.get("/api/market-regime")
def api_market_regime():
    """Глобальный 365-дневный анализ поведения рынка.

    Что внутри:
      - global_hot_hours_utc_top10 — топ-10 часов UTC по среднему |return|
        (по всем 28 парам).
      - per-pair: hot_hours_utc, by_session_dow, by_hour, high_vol_clusters,
        vol_thresholds.
    Источник: state/market_regime_365d.json (обновляется по требованию).
    """
    return _load(
        config.STATE_DIR / "market_regime_365d.json",
        {"as_of": None, "pairs": {}, "global_hot_hours_utc_top10": [],
         "note": "ещё не вычислено — запусти `python -m teamagent.market_regime_analyzer`"},
    )


@app.get("/api/market-regime/{pair}")
def api_market_regime_pair(pair: str):
    pair = pair.upper()
    if pair not in config.PAIRS:
        raise HTTPException(400, f"unknown pair {pair}")
    data = _load(config.STATE_DIR / "market_regime_365d.json", {"pairs": {}})
    p = data.get("pairs", {}).get(pair)
    if not p:
        raise HTTPException(404, f"market regime for {pair} not yet computed")
    return p


@app.get("/api/agents")
def api_agents():
    """Список всех агентов с heartbeat."""
    agents_state = _load(config.STATE_DIR / "agents.json", {"agents": []})
    return agents_state


def _unwrap_agent_state(data: dict, fallback_note: str) -> dict:
    """Агенты пишут tick output в `state["summary"]`. Дашборду удобнее видеть
    summary как корневой объект + as_of сверху. Если файла ещё нет — note."""
    if not data or "summary" not in data:
        return {"note": fallback_note}
    out = dict(data["summary"]) if isinstance(data["summary"], dict) else {"value": data["summary"]}
    if "as_of" in data and "as_of" not in out:
        out["as_of"] = data["as_of"]
    return out


@app.get("/api/wr-floor")
def api_wr_floor():
    """Состояние WR floor monitor (rolling 50 trades vs 70% floor)."""
    raw = _load(config.STATE_DIR / "agent_learner_wr_floor_monitor.json", {})
    return _unwrap_agent_state(
        raw,
        "не считал ещё — жди первого tick (5 мин) от learner_wr_floor_monitor",
    )


@app.get("/api/weekly-loss-review")
def api_weekly_loss():
    """Сводка минусов за последние 7 дней (weekly loss analyzer)."""
    raw = _load(config.STATE_DIR / "agent_learner_weekly_loss_review.json", {})
    return _unwrap_agent_state(
        raw,
        "не считал ещё — жди первого tick (6 ч) от learner_weekly_loss_review",
    )


@app.get("/api/cot")
def api_cot():
    """CFTC COT speculator positioning + per-pair contrarian signals."""
    raw = _load(config.STATE_DIR / "agent_analyzer_cot_positioning.json", {})
    summary = _unwrap_agent_state(
        raw,
        "COT данные ещё не загружены — жди первого tick analyzer_cot_positioning",
    )
    cot_raw = _load(config.STATE_DIR / "cot_positioning.json", {})
    if cot_raw:
        summary["cot_raw"] = {
            "as_of": cot_raw.get("as_of"),
            "currencies": cot_raw.get("currencies", {}),
            "source": cot_raw.get("source"),
        }
    try:
        from .. import cot as cot_mod
        if cot_raw:
            summary["all_pair_signals"] = cot_mod.all_pair_signals().get("signals", {})
    except Exception as e:
        summary["all_pair_signals_error"] = str(e)
    return summary


@app.get("/api/fundamentals")
def api_fundamentals():
    """Per-currency macro snapshot (FRED) + per-pair tilt scores. Updates
    every ~6h via analyzer_fundamental_macro; underlying CSVs cached 24h."""
    raw = _load(config.STATE_DIR / "agent_analyzer_fundamental_macro.json", {})
    summary = _unwrap_agent_state(
        raw,
        "FRED данные ещё не загружены — жди первого tick analyzer_fundamental_macro",
    )
    # Also expose the raw fundamentals.json so frontend can show all 28 pair tilts
    fund_raw = _load(config.STATE_DIR / "fundamentals.json", {})
    if fund_raw:
        summary["fundamentals_raw"] = {
            "as_of": fund_raw.get("as_of"),
            "currencies": fund_raw.get("currencies", {}),
            "source": fund_raw.get("source"),
        }
    # And all 28 pair tilts (compute on demand if cache exists)
    try:
        from .. import fundamentals as fmod
        if fund_raw:
            summary["all_pair_tilts"] = fmod.all_pair_tilts().get("tilts", {})
    except Exception as e:
        summary["all_pair_tilts_error"] = str(e)
    return summary


@app.get("/api/stability")
def api_stability():
    """Главный отчёт стабильности системы (50+ метрик).

    Берёт данные ИСКЛЮЧИТЕЛЬНО из реальных state-файлов + Yahoo:
      - paper_stats / closed_trades / strategy_config / forecasts
      - 365-дневная Yahoo для conformal / VaR / vol метрик

    Никаких симуляторов, никакого random — bootstrap идёт по РЕАЛЬНЫМ
    наблюдениям (resampling), seed зафиксирован.
    """
    try:
        from .. import stability_engine as se
        from .. import resume_ru as ru
    except ImportError:
        from teamagent import stability_engine as se
        from teamagent import resume_ru as ru
    try:
        report = se.system_stability_report()
        assessment = ru.general_assessment()
        mg = se.min_guarantee_per_trade(stake_usd=1.0, payout_pct=0.85)
        return {
            "as_of": report.get("as_of"),
            "report": report,
            "assessment": assessment,
            "min_guarantee": mg,
        }
    except Exception as e:
        log.exception(f"api_stability failed: {e}")
        return JSONResponse(
            {"error": f"{type(e).__name__}: {e}"},
            status_code=500,
        )


@app.get("/api/stability/{pair}")
def api_stability_pair(pair: str):
    """Per-pair детальный отчёт стабильности."""
    try:
        from .. import resume_ru as ru
    except ImportError:
        from teamagent import resume_ru as ru
    pair = pair.upper()
    if pair not in config.PAIRS:
        raise HTTPException(400, f"unknown pair {pair}")
    try:
        return ru.per_pair_summary(pair)
    except Exception as e:
        log.exception(f"api_stability_pair({pair}) failed: {e}")
        return JSONResponse(
            {"error": f"{type(e).__name__}: {e}"},
            status_code=500,
        )


@app.get("/api/min-guarantee")
def api_min_guarantee():
    """Гарантированный (95% доверие) ожидаемый PnL на сделку."""
    try:
        from .. import stability_engine as se
    except ImportError:
        from teamagent import stability_engine as se
    return se.min_guarantee_per_trade(stake_usd=1.0, payout_pct=0.85)


@app.get("/api/conformal/{pair}")
def api_conformal(pair: str, horizon_hours: int = 4, confidence: float = 0.90):
    """Conformal prediction band для цены через N часов."""
    try:
        from .. import stability_engine as se
    except ImportError:
        from teamagent import stability_engine as se
    pair = pair.upper()
    if pair not in config.PAIRS:
        raise HTTPException(400, f"unknown pair {pair}")
    return se.conformal_price_band(pair, horizon_hours, confidence, 90)


@app.get("/api/risk-metrics")
def api_risk_metrics():
    """VaR, CVaR, Sharpe, Sortino, MDD, Calmar, Kelly, Profit Factor по реальным закрытым сделкам."""
    try:
        from .. import stability_engine as se
    except ImportError:
        from teamagent import stability_engine as se
    rep = se.system_stability_report()
    rets = se.closed_trades_returns()
    return {
        "as_of": rep.get("as_of"),
        "var_95": rep.get("var_95"),
        "cvar_95": rep.get("cvar_95"),
        "sharpe_ratio": rep.get("sharpe_ratio"),
        "sortino_ratio": rep.get("sortino_ratio"),
        "max_drawdown_pct": rep.get("max_drawdown_pct"),
        "profit_factor": rep.get("profit_factor"),
        "expectancy_per_trade": rep.get("expectancy_per_trade"),
        "kelly_fraction_half": rep.get("kelly_fraction_half"),
        "skew": rep.get("skew"),
        "kurtosis": rep.get("kurtosis"),
        "n_trades": rets.get("total"),
        "longest_win_streak": rep.get("longest_win_streak"),
        "longest_loss_streak": rep.get("longest_loss_streak"),
        "current_streak": rep.get("current_streak"),
        "current_streak_kind": rep.get("current_streak_kind"),
        "break_even_probability": rep.get("break_even_probability"),
        "slippage_threshold_probability": rep.get("slippage_threshold_probability"),
    }


@app.get("/api/calibration")
def api_calibration():
    """Brier score + log loss + per-bin calibration (predicted prob vs actual WR)."""
    try:
        from .. import stability_engine as se
    except ImportError:
        from teamagent import stability_engine as se
    rep = se.system_stability_report()
    return {
        "as_of": rep.get("as_of"),
        "brier_score": rep.get("brier_score"),
        "log_loss": rep.get("log_loss"),
        "calibration_bins": rep.get("calibration_bins"),
        "n_trades": rep.get("n_closed_trades"),
    }


@app.get("/api/market-status")
def api_market_status():
    """Статус Forex рынка + обратный отсчёт до закрытия/открытия.

    Forex открыт Sun 22:00 — Fri 22:00 UTC.
    Возвращает:
      is_open, status_emoji, status_text (ОТКРЫТ/ЗАКРЫТ)
      session (Asia/London/Overlap/NY/Closed)
      seconds_until_close, seconds_until_open
      next_event_utc — ISO timestamp следующего события
      max_safe_expiry_h — максимальная безопасная экспирация СЕЙЧАС
    """
    try:
        from .. import market_hours as mh
    except ImportError:
        from teamagent import market_hours as mh
    return mh.market_status()


@app.get("/api/stability-forecast")
def api_stability_forecast(hours_ahead: int = 24):
    """Pre-emptive прогноз стабильности на следующие N часов.

    Это НЕ зависит от количества закрытых сделок — расчёт идёт по
    качеству strategy_config × сессиям × market_hours × news.
    Возвращает ожидаемый WR с CI, активные qualified пары, eligible
    forecasts, готовность системы (0..100), диагноз и рекомендации.
    """
    try:
        from .. import stability_forecast as sf
    except ImportError:
        from teamagent import stability_forecast as sf
    hours_ahead = max(1, min(168, int(hours_ahead)))  # 1h..7d
    return sf.forecast_window(hours_ahead=hours_ahead)


@app.get("/api/meta-strategy")
def api_meta_strategy():
    """Master Strategy Agent — последний 5h sweep ансамбля сигналов.

    Каждые 5 часов teamagent.strategy_meta_agent делает sweep 28 пар × 4
    сессий × 120 вариантов на 5-дневном окне Yahoo. Возвращает summary +
    cells (per-(pair, session) ячейки со статусом QUALIFIED / PROBABLE /
    FROZEN, ожидаемой WR, Wilson lower bound, side_bias из ансамбля
    COT/fundamentals/regime/radar)."""
    return _load(
        config.STATE_DIR / "meta_strategy.json",
        {"as_of": None, "summary": {}, "cells": {}, "pairs": {}},
    )


@app.get("/api/meta-strategy/log")
def api_meta_strategy_log(limit: int = 50):
    """Live-лог последних прогонов мета-агента (по одной строке на sweep).

    Объявлена ДО /api/meta-strategy/{pair}, иначе FastAPI ловит "log" как pair.
    """
    log_path = config.STATE_DIR / "meta_strategy_log.jsonl"
    if not log_path.exists():
        return {"as_of": None, "entries": []}
    limit = max(1, min(200, int(limit)))
    try:
        lines = log_path.read_text().splitlines()
    except Exception:
        return {"as_of": None, "entries": []}
    entries = []
    for ln in lines[-limit:]:
        ln = ln.strip()
        if not ln:
            continue
        try:
            entries.append(json.loads(ln))
        except Exception:
            continue
    return {
        "as_of": datetime.now(timezone.utc).isoformat(),
        "log_file": str(log_path),
        "total_entries": len(lines),
        "returned": len(entries),
        "entries": entries,
    }


@app.get("/api/meta-strategy/{pair}")
def api_meta_strategy_pair(pair: str):
    """Per-pair срез: все 4 сессии этой пары + ensemble-сигналы."""
    pair = pair.upper()
    data = _load(config.STATE_DIR / "meta_strategy.json", {})
    pair_data = (data.get("pairs") or {}).get(pair)
    if not pair_data:
        raise HTTPException(404, f"no meta-strategy data for {pair}")
    return pair_data


@app.get("/api/system-audit")
def api_system_audit():
    """Доказательства корректности системы.

    Запускает 15+ проверок самосогласованности (paper_stats vs closed_trades,
    forecasts ↔ config.PAIRS, market_hours ↔ session, expiry формула,
    schema-валидация, code health, freshness, кросс-модульные инварианты)
    и возвращает агрегированный отчёт. Если все 🟢 — данным системы можно
    верить; если хоть одна 🔴 — система противоречит сама себе.
    """
    try:
        from .. import system_audit as sa
    except ImportError:
        from teamagent import system_audit as sa
    try:
        return sa.run_audit()
    except Exception as e:
        log.exception(f"api_system_audit failed: {e}")
        return JSONResponse(
            {"error": f"{type(e).__name__}: {e}", "overall_status": "red"},
            status_code=500,
        )


@app.get("/api/final-signal")
def api_final_signal():
    """ФИНАЛЬНЫЙ ПРОГНОЗ ДЛЯ ПОЛЬЗОВАТЕЛЯ — ТОП-1 валидированный сигнал
    с reasoning + alternates. Backwards-compatible (старая UI-секция).
    """
    try:
        from .. import final_signal as fs
    except ImportError:
        from teamagent import final_signal as fs
    try:
        return JSONResponse(fs.build())
    except Exception as e:
        log.exception(f"api_final_signal failed: {e}")
        return JSONResponse({"error": f"{type(e).__name__}: {e}"}, status_code=500)


@app.get("/api/ai-narrative")
def api_ai_narrative():
    """AI-АГЕНТ: развёрнутый прогноз свободным языком на русском.

    Использует БЕСПЛАТНЫЙ публичный endpoint Pollinations.ai (без API-ключа,
    без учёток, без лимитов на разумных объёмах). Берёт текущее состояние
    системы (final-signals + agent reports) и просит LLM написать одну-две
    короткие связные пары абзацев на русском, которые объясняют:
        — что система предлагает торговать прямо сейчас и почему,
        — какие риски стоит держать в голове,
        — что меняется к ближайшей сессии.

    Если Pollinations недоступен — fallback на детерминированную сводку
    (никаких симуляций, всё из real state).

    Кэшируется в памяти на 5 мин чтобы не спамить free-API.
    """
    import urllib.parse
    import urllib.request
    cache = getattr(api_ai_narrative, "_cache", None)
    now_ts = time.time()
    if cache and now_ts - cache["ts"] < 300:
        return JSONResponse(cache["data"])

    try:
        from .. import final_signal as fs
        from .. import agent_reports as ar
    except ImportError:
        from teamagent import final_signal as fs
        from teamagent import agent_reports as ar

    try:
        full = fs.build_all()
    except Exception as e:
        full = {"error": f"final_signal: {e}"}
    try:
        rep = ar.all_reports() or {}
    except Exception as e:
        rep = {"error": f"agent_reports: {e}"}

    sum_ = (full.get("summary") or {}) if isinstance(full, dict) else {}
    sigs = (full.get("signals") or []) if isinstance(full, dict) else []
    top = sigs[:3]

    fact_lines = []
    fact_lines.append(
        f"Сессия сейчас: {full.get('session_now_ru','?')}. "
        f"GO={sum_.get('go',0)}, GO_CAUTION={sum_.get('go_caution',0)}, "
        f"WAIT={sum_.get('wait',0)} из {sum_.get('total',28)}. "
        f"Стратегии готовы для {sum_.get('qualified_cells_for_session',0)}/28 пар."
    )
    fact_lines.append(
        f"Рынок: {(full.get('global_context') or {}).get('market_detail','?')}"
    )
    for s in top:
        fact_lines.append(
            f"{s.get('pair')} {s.get('side')} prob={s.get('probability_pct',0):.0f}% "
            f"verdict={s.get('verdict')} blocker={s.get('short_blocker','-')}"
        )
    for k in ("technical", "fundamental", "macro", "political", "news"):
        r = (rep.get("reports") or {}).get(k) or {}
        if r.get("verdict_ru"):
            fact_lines.append(f"{k}: {r['verdict_ru']}")

    facts_block = "\n".join(fact_lines)[:3500]

    prompt = (
        "Ты — старший аналитик торговой системы FX INVESTMENT. Тебе дают набор "
        "фактов про текущее состояние рынка форекс (28 пар) и просят написать "
        "короткий связный комментарий на РУССКОМ языке (2 коротких абзаца, "
        "150–250 слов). \n"
        "Стиль: уверенный, спокойный, без воды и без выдумок. Никаких новых "
        "пар или цифр которых нет в фактах. Не говори «по моему мнению», "
        "говори «система видит»/«стратегия рекомендует».\n"
        "Формат:\n"
        "  Абзац 1: Что система предлагает делать прямо сейчас и почему "
        "(перечисли пары из GO/GO_CAUTION с обоснованием).\n"
        "  Абзац 2: Какие риски и что меняется к следующей сессии.\n\n"
        "ФАКТЫ:\n" + facts_block
    )

    narrative = None
    source = "pollinations"
    err = None
    try:
        url = "https://text.pollinations.ai/" + urllib.parse.quote(prompt)
        req = urllib.request.Request(url, headers={"User-Agent": "fx-investment/1.0"})
        with urllib.request.urlopen(req, timeout=18) as resp:
            body = resp.read().decode("utf-8", errors="replace").strip()
            if body and len(body) > 40:
                narrative = body
    except Exception as e:
        err = f"{type(e).__name__}: {e}"

    if not narrative:
        # Honest deterministic fallback — no fake data, no simulator
        source = "fallback_deterministic"
        if sum_.get("go", 0) >= 1:
            lead = (
                f"Прямо сейчас система видит {sum_.get('go',0)} парy/пары в "
                f"состоянии GO и {sum_.get('go_caution',0)} в GO_CAUTION на "
                f"сессии «{full.get('session_now_ru','?')}». Это значит что "
                f"для этих пар все 8 проверок зелёные или почти зелёные."
            )
        else:
            lead = (
                f"Сейчас на сессии «{full.get('session_now_ru','?')}» нет ни "
                f"одной пары в состоянии GO. Все 28 ждут — главные блокеры: "
                f"{(full.get('global_context') or {}).get('market_detail','?')}."
            )
        risks = []
        macro_v = (rep.get("reports") or {}).get("macro", {}).get("verdict_ru", "")
        polit_v = (rep.get("reports") or {}).get("political", {}).get("verdict_ru", "")
        news_v = (rep.get("reports") or {}).get("news", {}).get("verdict_ru", "")
        for v in (macro_v, polit_v, news_v):
            if v and not v.startswith("🟢"):
                risks.append(v)
        risk_text = " ".join(risks[:3]) or "Серьёзных макро/политических рисков сейчас нет."
        narrative = (
            lead + "\n\n"
            "Что важно держать в голове: " + risk_text + " "
            "Стратегии для текущей сессии готовы у "
            f"{sum_.get('qualified_cells_for_session',0)}/28 пар — это значит "
            "что система не торгует «вслепую», для каждой одобренной пары есть "
            "проверенная 30-дневная история. Когда сессия сменится, набор "
            "доступных пар изменится автоматически."
        )

    out = {
        "as_of_utc": datetime.now(timezone.utc).isoformat(),
        "source": source,
        "narrative_ru": narrative,
        "facts_used": fact_lines,
        "error": err,
    }
    api_ai_narrative._cache = {"ts": now_ts, "data": out}
    return JSONResponse(out)


@app.get("/api/final-signals")
def api_final_signals():
    """ФИНАЛЬНЫЙ ПРОГНОЗ — ВСЕ 28 ПАР с индивидуальной валидацией.

    User explicit ask (2026-05-04): «финальный прогноз был всё 27 валюти … нужно
    найти подод для каждого валюти и сессиях отденый подходит».

    Каждая пара получает 8 проверок (probability/market/news/meta_strategy/
    ensemble/macro/political/freshness) и индивидуальный verdict
    GO / GO_CAUTION / WAIT. Сортировка GO → GO_CAUTION → WAIT, внутри по
    probability убывающе.
    """
    try:
        from .. import final_signal as fs
    except ImportError:
        from teamagent import final_signal as fs
    try:
        return JSONResponse(fs.build_all())
    except Exception as e:
        log.exception(f"api_final_signals failed: {e}")
        return JSONResponse({"error": f"{type(e).__name__}: {e}"}, status_code=500)


@app.get("/api/agent-reports")
def api_agent_reports():
    """5 narrative-отчётов в одном вызове, ВСЁ НА РУССКОМ:

    1. ``technical``   — что говорят 28 пар на текущих индикаторах
    2. ``fundamental`` — ставки / доходности / инфляция (FRED)
    3. ``news``        — high-impact события (ForexFactory RSS)
    4. ``macro``       — DXY / US10Y / нефть / золото (Yahoo)
    5. ``political``   — гео-политические триггеры (Reuters/BBC RSS)

    Каждый отчёт честно говорит "источник недоступен" если RSS / API упал —
    никаких выдумок. Все источники открытые, без API-ключей.
    """
    try:
        from .. import agent_reports as ar
    except ImportError:
        from teamagent import agent_reports as ar
    try:
        return JSONResponse(ar.all_reports())
    except Exception as e:
        log.exception(f"api_agent_reports failed: {e}")
        return JSONResponse({"error": f"{type(e).__name__}: {e}"}, status_code=500)


@app.get("/api/coverage-matrix")
def api_coverage_matrix():
    """28 пар × 4 сессии = 112 ячеек. Цвет каждой:
    🟢 QUALIFIED (≥70% WR), 🟡 PROBABLE (60-70%), 🔴 FROZEN (<60%), ⚫ MISSING.

    Источник: ``state/meta_strategy.json`` который пишется
    ``strategy_meta_agent`` (sweep по 28 × 4 × 250 вариантам каждые 5 часов).
    Это даёт пользователю наглядную картину "где ИНДИВИДУАЛЬНЫЙ подход
    к (паре, сессии) уже работает, а где надо ещё дотянуть".
    """
    try:
        from .. import agent_reports as ar
    except ImportError:
        from teamagent import agent_reports as ar
    try:
        return JSONResponse(ar.coverage_matrix())
    except Exception as e:
        log.exception(f"api_coverage_matrix failed: {e}")
        return JSONResponse({"error": f"{type(e).__name__}: {e}"}, status_code=500)


@app.get("/api/system-health")
def api_system_health():
    """Две сводки в одном вызове — то что user-у нужно для понимания
    "что система чувствует прямо сейчас":

    1. ``errors_report`` — самодиагностика: красные проверки из system_audit,
       устаревшие state-файлы, мёртвые heartbeat-ы, неоткрытые сделки при
       подходящих forecasts и т.д. Это то на что система должна РЕАГИРОВАТЬ
       (сама перезапустить агент, переcбилдить static-mirror, etc.).

    2. ``facts_report`` — данные-факты: текущий рынок открыт/закрыт, сколько
       forecasts ≥70%, сколько qualified пар, сколько открытых сделок,
       последние закрытые с PnL. Это то на основе чего система ПРИНИМАЕТ
       решения (открывать сделку или нет, сколько часов expiry и т.п.).

    Все источники — те же файлы, что и существующие endpoints, чтобы фронт
    мог брать ВСЁ из одного запроса вместо 5–7 round-trip-ов и быть
    уверенным, что разные блоки UI рисуются по согласованным данным.
    """
    now = datetime.now(timezone.utc)
    errors: list[dict] = []
    warnings: list[dict] = []
    facts: dict = {}

    # ── 1) market status — single source for everything market-related ──
    try:
        from .. import market_hours as mh
    except ImportError:
        from teamagent import market_hours as mh
    market = mh.market_status(now)
    facts["market"] = {
        "is_open": market["is_open"],
        "session": market["session"],
        "status_text": market["status_text"],
        "seconds_until_open": market["seconds_until_open"],
        "seconds_until_close": market["seconds_until_close"],
        "next_event": market["next_event"],
        "next_event_utc": market["next_event_utc"],
        "max_safe_expiry_h": market["max_safe_expiry_h"],
    }

    # ── 2) state files freshness (errors if stale, warnings on near-stale) ──
    freshness_thresholds_sec = {
        "forecasts.json": 600,            # scanner runs every 5 min
        "paper_stats.json": 1800,         # paper_trader writes every trade
        "closed_trades.json": 86400 * 3,  # closed trades may be sparse
        "strategy_config_locked.json": 86400 * 7,
        "backtest_30d.json": 7200,
        "meta_strategy.json": 86400 * 2,
    }
    state_files: dict[str, dict] = {}
    for fname, max_age in freshness_thresholds_sec.items():
        p = config.STATE_DIR / fname
        if not p.exists():
            entry = {"present": False, "age_sec": None, "stale": True}
            state_files[fname] = entry
            errors.append({
                "code": "STATE_FILE_MISSING",
                "file": fname,
                "message_ru": f"Отсутствует обязательный state-файл {fname}",
                "self_fix_ru": "Запусти `bash scripts/start_all.sh` — агенты пересоздадут файл.",
            })
            continue
        age = max(0.0, (now.timestamp() - p.stat().st_mtime))
        stale = age > max_age
        entry = {"present": True, "age_sec": int(age), "stale": stale}
        state_files[fname] = entry
        if stale:
            warnings.append({
                "code": "STATE_FILE_STALE",
                "file": fname,
                "age_sec": int(age),
                "max_age_sec": max_age,
                "message_ru": f"State-файл {fname} устарел ({int(age/60)} мин назад, лимит {max_age//60} мин).",
                "self_fix_ru": "Перезапусти агента — `bash scripts/start_all.sh`.",
            })
    facts["state_files"] = state_files

    # ── 3) heartbeats — dead agents ──
    hb_components = []
    hb_dead_count = 0
    for name, fname in [
        ("forecast_scanner", "heartbeat_forecast_scanner.json"),
        ("paper_trader", "heartbeat_paper_trader.json"),
        ("orchestrator", "heartbeat_orchestrator.json"),
        ("watchdog", "heartbeat_watchdog.json"),
        ("state_committer", "heartbeat_state_committer.json"),
    ]:
        hb = _load(config.STATE_DIR / fname, None)
        if not hb or "ts" not in hb:
            hb_components.append({"name": name, "alive": False, "last_seen": None})
            hb_dead_count += 1
            errors.append({
                "code": "AGENT_DEAD",
                "agent": name,
                "message_ru": f"Агент {name} не пишет heartbeat — возможно убит.",
                "self_fix_ru": "Watchdog должен авто-перезапустить; если нет — `bash scripts/start_all.sh`.",
            })
            continue
        try:
            ts = datetime.fromisoformat(hb["ts"])
            age = (now - ts).total_seconds()
            alive = age < config.AGENT_DEAD_AFTER_SEC
            hb_components.append({"name": name, "alive": alive, "age_sec": int(age)})
            if not alive:
                hb_dead_count += 1
                warnings.append({
                    "code": "AGENT_STALE_HEARTBEAT",
                    "agent": name,
                    "age_sec": int(age),
                    "message_ru": f"Heartbeat агента {name} {int(age/60)} мин назад (лимит {config.AGENT_DEAD_AFTER_SEC//60} мин).",
                    "self_fix_ru": "Watchdog авто-перезапустит при следующем сканировании.",
                })
        except Exception:
            hb_components.append({"name": name, "alive": False, "last_seen": hb.get("ts")})
    facts["heartbeats"] = {"components": hb_components, "dead_count": hb_dead_count}

    # ── 4) forecasts / paper-trader counts — used by all decision-making ──
    snap = _load(config.STATE_DIR / "forecasts.json", {"forecasts": {}, "rankings": []})
    rankings = snap.get("rankings") or []
    eligible_70 = [r for r in rankings if (r.get("probability_pct") or 0) >= 70]
    open_trades = _load(config.STATE_DIR / "open_trades.json", [])
    closed_trades = _load(config.STATE_DIR / "closed_trades.json", [])
    paper_stats = _load(config.STATE_DIR / "paper_stats.json", {})
    facts["forecasts"] = {
        "total_pairs": len(snap.get("forecasts") or {}),
        "scanned_at": snap.get("scanned_at"),
        "eligible_70_count": len(eligible_70),
        "top_buy": next((r for r in rankings if r.get("side") == "BUY"), None),
        "top_sell": next((r for r in rankings if r.get("side") == "SELL"), None),
    }
    facts["paper_trader"] = {
        "open_count": len(open_trades),
        "closed_count": len(closed_trades),
        "win_rate_pct": paper_stats.get("win_rate_pct"),
        "total_pnl_usd": paper_stats.get("total_pnl_usd"),
        "wins": paper_stats.get("wins"),
        "losses": paper_stats.get("losses"),
    }

    # ── 5) "система видит eligible но рынок закрыт" — диагностический warning ──
    if not market["is_open"] and len(eligible_70) > 0:
        warnings.append({
            "code": "ELIGIBLE_FORECAST_BUT_MARKET_CLOSED",
            "message_ru": (
                f"Сейчас {len(eligible_70)} forecasts ≥70%, но рынок закрыт — "
                f"новые сделки откроются после {market['next_event_utc']} UTC."
            ),
            "self_fix_ru": "Это нормально — paper_trader не открывает сделки на закрытом рынке.",
        })
    elif market["is_open"] and len(eligible_70) > 0 and len(open_trades) == 0:
        warnings.append({
            "code": "ELIGIBLE_FORECAST_NO_OPEN_TRADES",
            "message_ru": (
                f"{len(eligible_70)} eligible forecasts но 0 открытых сделок — "
                "возможно блокирует correlation-filter или news_blackout."
            ),
            "self_fix_ru": "Проверь paper_trader логи — `tail teamagent/logs/paper_trader.log`.",
        })

    # ── 6) consolidated audit summary (calls run_audit but only takes counts) ──
    try:
        try:
            from .. import system_audit as sa
        except ImportError:
            from teamagent import system_audit as sa
        audit = sa.run_audit()
        facts["audit_summary"] = {
            "overall_status": audit.get("overall_status"),
            "summary": audit.get("summary"),
            "verdict_ru": audit.get("verdict_ru"),
        }
        for cat in audit.get("categories") or []:
            for chk in cat.get("checks") or []:
                if chk.get("status") == "red":
                    errors.append({
                        "code": "AUDIT_RED",
                        "check": chk.get("name"),
                        "category": cat.get("key"),
                        "message_ru": chk.get("message_ru") or chk.get("message") or "",
                        "self_fix_ru": "См. `/api/system-audit` для деталей.",
                    })
    except Exception as e:
        warnings.append({
            "code": "AUDIT_FAILED",
            "message_ru": f"system_audit бросил {type(e).__name__}: {e}",
            "self_fix_ru": "Открой /api/system-audit — там подробный traceback.",
        })

    return JSONResponse({
        "as_of_utc": now.isoformat(),
        "errors_report": {
            "count": len(errors),
            "items": errors,
        },
        "warnings_report": {
            "count": len(warnings),
            "items": warnings,
        },
        "facts_report": facts,
        "verdict_ru": (
            "✅ Все системы зелёные." if not errors and not warnings
            else f"⚠️ {len(errors)} ошибка/-ок и {len(warnings)} предупреждение/-й — "
                 "система должна сама среагировать."
            if errors else
            f"🟡 {len(warnings)} предупреждение/-й — диагностика только."
        ),
    })


@app.get("/api/health")
def api_health():
    """Общий health-check всех процессов."""
    out = {
        "as_of": datetime.now(timezone.utc).isoformat(),
        "components": {},
    }
    for name, fname in [
        ("forecast_scanner", "heartbeat_forecast_scanner.json"),
        ("paper_trader", "heartbeat_paper_trader.json"),
        ("paper_trader_stakan", "heartbeat_paper_trader_stakan.json"),
        ("market_radar", "heartbeat_market_radar.json"),
        ("paper_trader_daily", "heartbeat_paper_trader_daily.json"),
        ("orchestrator", "heartbeat_orchestrator.json"),
        ("watchdog", "heartbeat_watchdog.json"),
        ("backtester", "heartbeat_backtester.json"),
        ("state_committer", "heartbeat_state_committer.json"),
        ("strategy_search", "heartbeat_strategy_search.json"),
        ("strategy_meta_agent", "heartbeat_strategy_meta_agent.json"),
    ]:
        hb = _load(config.STATE_DIR / fname, None)
        if hb is None:
            out["components"][name] = {"alive": False, "last_seen": None}
            continue
        try:
            ts = datetime.fromisoformat(hb["ts"])
            age = (datetime.now(timezone.utc) - ts).total_seconds()
            # forecast_scanner heartbeats only at end of each scan (~5min cycle);
            # use AGENT_DEAD_AFTER_SEC (10min) as the UI alive threshold to match
            # what watchdog uses to actually kill stale processes.
            out["components"][name] = {
                "alive": age < config.AGENT_DEAD_AFTER_SEC,
                "age_sec": int(age),
                "last_seen": hb["ts"],
                "pid": hb.get("pid"),
            }
        except Exception:
            out["components"][name] = {"alive": False, "last_seen": hb.get("ts")}

    open_trades = _load(config.STATE_DIR / "open_trades.json", [])
    closed = _load(config.STATE_DIR / "closed_trades.json", [])
    out["paper_trader_summary"] = {
        "open_count": len(open_trades),
        "closed_count": len(closed),
    }
    stakan_open = _load(config.STATE_DIR / "stakan_open_trades.json", [])
    stakan_closed = _load(config.STATE_DIR / "stakan_closed_trades.json", [])
    out["stakan_summary"] = {
        "open_count": len(stakan_open),
        "closed_count": len(stakan_closed),
    }
    return out


def serve(host: str = config.DASHBOARD_HOST, port: int = config.DASHBOARD_PORT) -> None:
    import uvicorn
    log.info(f"dashboard serving on http://{host}:{port}")
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    serve()
