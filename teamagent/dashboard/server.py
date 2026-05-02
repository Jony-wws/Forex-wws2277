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
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from .. import config
from ..data import yahoo
from .. import volume_profile as vp_mod
from .. import paper_trader
from .. import paper_trader_stakan

log = logging.getLogger("dashboard")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "static"


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
    * (default, e.g. on a Devin VM) → spawn full orchestrator + watchdog. The
      orchestrator itself fans out to forecast_scanner, paper_traders, 60
      agents, etc.
    """
    if os.environ.get("DASHBOARD_ONLY") == "1":
        log.info("DASHBOARD_ONLY=1 — skipping background processes")
        return []
    children: list[subprocess.Popen] = []
    log_dir = config.LOGS_DIR
    log_dir.mkdir(parents=True, exist_ok=True)

    # Auto-detect Fly.io: presence of /data mount + FLY_APP_NAME env var.
    on_fly = os.environ.get("FLY_APP_NAME") is not None or Path("/data").is_dir()
    if on_fly and os.environ.get("FLY_FULL") != "1":
        # Default Fly machine = 256 MB → cannot fit orchestrator + 60 agents.
        # Dashboard-only mode reads state files committed by the hourly Devin
        # schedule (sched-…); for live scanning use a Fly machine with ≥1 GB.
        log.info("on-fly default-memory mode → dashboard-only (no scanner spawn)")
        return []
    if os.environ.get("FLY_MINIMAL") == "1":
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


@asynccontextmanager
async def lifespan(app: FastAPI):
    """FastAPI lifespan: seed state + spawn orchestrator/watchdog on startup,
    terminate them on shutdown. Works both in local dev and on Fly.io.
    """
    _seed_state_files()
    children = _spawn_supervisor_processes()
    try:
        yield
    finally:
        for p in children:
            try:
                p.terminate()
            except Exception:
                pass


app = FastAPI(title="TeamAgent Dashboard", lifespan=lifespan)
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


@app.get("/history")
def history_page():
    """Quick deep-link → /system со скроллом к закрытым сделкам / paper-stats."""
    return RedirectResponse(url="/system#closed-trades-section", status_code=302)


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


@app.get("/api/intent-bars/{pair}")
def api_intent_bars(pair: str, interval: str = "15m", n: int = 96):
    """Облегчённые OHLC-бары для cinematic chart на /intent — Yahoo, кэшируется."""
    pair = pair.upper()
    if pair not in config.PAIRS:
        return JSONResponse({"error": f"unknown pair {pair}"}, status_code=404)
    if interval not in {"1m", "5m", "15m", "1h", "4h", "1d"}:
        return JSONResponse({"error": "bad interval"}, status_code=400)
    n = max(20, min(int(n), 300))
    try:
        df = yahoo.latest_bars(pair, interval=interval, n=n)
    except Exception as e:
        return JSONResponse({"error": f"{type(e).__name__}: {e}"}, status_code=500)
    if df is None or df.empty:
        return JSONResponse({"pair": pair, "bars": []})
    bars = []
    for ts, row in df.iterrows():
        bars.append({
            "time": int(ts.timestamp()),
            "open": float(row["Open"]),
            "high": float(row["High"]),
            "low": float(row["Low"]),
            "close": float(row["Close"]),
        })
    return {"pair": pair, "interval": interval, "bars": bars}


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
