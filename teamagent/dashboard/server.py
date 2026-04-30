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
import time
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .. import config
from ..data import yahoo
from .. import volume_profile as vp_mod
from .. import paper_trader

log = logging.getLogger("dashboard")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "static"

app = FastAPI(title="TeamAgent Dashboard")
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
    return FileResponse(str(STATIC / "index.html"))


@app.get("/api/forecasts")
def api_forecasts():
    """Единый источник: PROGNOZY-28 = мета-голосование (всё в одном).

    Возвращает и rankings (выжимка для таблицы), и forecasts (полный dict),
    чтобы фронт мог прямо из одного запроса взять agents_for_count/against_count.
    """
    snap = _load(config.STATE_DIR / "forecasts.json", {"forecasts": {}, "rankings": []})
    # вся расширенная по-парамная инфа (без больших indicators — их выкачаем лениво через /api/forecast/{pair})
    forecasts_lite = {}
    for pair, f in (snap.get("forecasts") or {}).items():
        forecasts_lite[pair] = {
            "pair": f.get("pair"),
            "side": f.get("side"),
            "probability_pct": f.get("probability_pct"),
            "score": f.get("score"),
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


@app.get("/api/agents")
def api_agents():
    """Список всех агентов с heartbeat."""
    agents_state = _load(config.STATE_DIR / "agents.json", {"agents": []})
    return agents_state


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
        ("orchestrator", "heartbeat_orchestrator.json"),
        ("watchdog", "heartbeat_watchdog.json"),
        ("backtester", "heartbeat_backtester.json"),
        ("state_committer", "heartbeat_state_committer.json"),
        ("strategy_search", "heartbeat_strategy_search.json"),
    ]:
        hb = _load(config.STATE_DIR / fname, None)
        if hb is None:
            out["components"][name] = {"alive": False, "last_seen": None}
            continue
        try:
            ts = datetime.fromisoformat(hb["ts"])
            age = (datetime.now(timezone.utc) - ts).total_seconds()
            out["components"][name] = {
                "alive": age < 120,
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
    return out


def serve(host: str = config.DASHBOARD_HOST, port: int = config.DASHBOARD_PORT) -> None:
    import uvicorn
    log.info(f"dashboard serving on http://{host}:{port}")
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    serve()
