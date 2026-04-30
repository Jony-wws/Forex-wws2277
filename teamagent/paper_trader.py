"""Paper-Trader 24/7 — бинарные опционы на основе forecast_scanner.

При каждом прогнозе ≥70% и отсутствии открытой сделки по этой паре —
открываем виртуальную сделку:
  stake = $50, payout = 85% (WIN +$42.50, LOSS -$50)
  expiry = recommended_hours от forecast_scanner (1..4ч)

Раз в минуту:
  - читает state/forecasts.json
  - открывает новые сделки (если ≥70% и нет уже открытой)
  - пересчитывает live PnL по всем открытым (по реальной цене Yahoo)
  - закрывает истёкшие по реальной цене Yahoo (settlement_price)
  - пишет state/open_trades.json и state/closed_trades.json

Для каждой открытой сделки UI получает:
  pair, side, open_price, open_time, expiry_time, current_price, current_pnl,
  current_pnl_pct, time_remaining_sec, projected_payout_if_closed_now.
"""
from __future__ import annotations
import json
import logging
import signal
import sys
import time
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path

from . import config
from .data import yahoo

log = logging.getLogger("paper")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.FileHandler(config.LOGS_DIR / "paper_trader.log"),
        logging.StreamHandler(sys.stdout),
    ],
)

FORECASTS_FILE = config.STATE_DIR / "forecasts.json"
OPEN_FILE = config.STATE_DIR / "open_trades.json"
CLOSED_FILE = config.STATE_DIR / "closed_trades.json"
STATS_FILE = config.STATE_DIR / "paper_stats.json"
BACKTEST_FILE = config.STATE_DIR / "backtest_30d.json"
HEARTBEAT_FILE = config.STATE_DIR / "heartbeat_paper_trader.json"

# Backtest gate: сделка открывается только если:
#   forecast.probability >= MIN_PROBABILITY И
#   backtest_30d[pair].win_rate_pct >= BACKTEST_MIN_WR_PCT И
#   backtest_30d[pair].trades >= BACKTEST_MIN_TRADES.
# Это и есть «реальный 70% WR», а не теоретическая probability.
BACKTEST_MIN_WR_PCT = 70.0
BACKTEST_MIN_TRADES = 5


def _load(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text())
    except Exception:
        log.warning(f"corrupt {path.name}, resetting")
        return default


def _save(path: Path, obj) -> None:
    path.write_text(json.dumps(obj, indent=2))


def _heartbeat() -> None:
    HEARTBEAT_FILE.write_text(json.dumps({
        "name": "paper_trader",
        "ts": datetime.now(timezone.utc).isoformat(),
        "pid": __import__("os").getpid(),
    }))


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _is_open_for_pair(open_trades: list[dict], pair: str) -> bool:
    return any(t["pair"] == pair and t["status"] == "open" for t in open_trades)


def _live_pnl(t: dict, current_price: float) -> dict:
    """Текущий PnL открытой сделки по live-цене.

    Бинарный опцион "если в твою сторону на момент сейчас, projected = WIN, иначе LOSS".
    Это «прогноз исхода», а не реальный PnL — UI должен это понимать.
    """
    side = t["side"]
    open_price = t["open_price"]
    if side == "BUY":
        in_money = current_price > open_price
        diff_pct = (current_price - open_price) / open_price * 100.0
    else:
        in_money = current_price < open_price
        diff_pct = (open_price - current_price) / open_price * 100.0

    projected_payout = config.STAKE_USD * config.PAYOUT_PCT if in_money else -config.STAKE_USD
    return {
        "current_price": float(current_price),
        "diff_pct": round(diff_pct, 5),
        "in_money_now": in_money,
        "projected_payout": round(projected_payout, 2),
    }


def _enrich_open_trade(t: dict) -> dict:
    """Добавляет live-данные для UI: current_price, current_pnl, time_remaining."""
    now = _now()
    expiry = datetime.fromisoformat(t["expiry_time"])
    open_time = datetime.fromisoformat(t["open_time"])
    cp = yahoo.latest_price(t["pair"])
    live = _live_pnl(t, cp) if cp is not None else {
        "current_price": None,
        "diff_pct": None,
        "in_money_now": None,
        "projected_payout": None,
    }
    return {
        **t,
        "live": {
            **live,
            "time_remaining_sec": max(0, int((expiry - now).total_seconds())),
            "elapsed_sec": int((now - open_time).total_seconds()),
            "as_of": now.isoformat(),
        },
    }


def _backtest_qualified(pair: str, backtest: dict) -> tuple[bool, dict]:
    """Проверить что исторический WR за 30 дней проходит порог."""
    pairs = (backtest or {}).get("pairs") or {}
    p = pairs.get(pair)
    if not p:
        return False, {"reason": "backtest ещё не запускался"}
    wr = p.get("win_rate_pct")
    trades = p.get("trades") or 0
    if wr is None:
        return False, {"reason": p.get("note") or "нет истории", "trades": trades}
    if trades < BACKTEST_MIN_TRADES:
        return False, {"reason": f"trades<{BACKTEST_MIN_TRADES}", "trades": trades, "win_rate_pct": wr}
    if wr < BACKTEST_MIN_WR_PCT:
        return False, {"reason": f"WR<{BACKTEST_MIN_WR_PCT}%", "trades": trades, "win_rate_pct": wr}
    return True, {"trades": trades, "win_rate_pct": wr}


def _open_new_trades(open_trades: list[dict], snapshot: dict, backtest: dict) -> int:
    """Открыть виртуальные сделки по всем прогнозам, которые проходят:
      forecast.probability_pct >= 70 И backtest WR за 30д ≥ 70% (на 5+ сделках).
    """
    rankings = snapshot.get("rankings", [])
    forecasts = snapshot.get("forecasts", {})
    opened = 0
    for r in rankings:
        if r["probability_pct"] < config.MIN_PROBABILITY * 100.0:
            continue
        pair = r["pair"]
        if _is_open_for_pair(open_trades, pair):
            continue

        ok, why = _backtest_qualified(pair, backtest)
        if not ok:
            log.info(
                f"SKIP {pair} prob={r['probability_pct']}% — backtest gate: {why}"
            )
            continue

        f = forecasts.get(pair)
        if not f:
            continue
        cp = yahoo.latest_price(pair)
        if cp is None:
            continue

        recommended = f.get("recommended_hours", config.DEFAULT_EXPIRY_HOURS)
        expiry_time = _now() + timedelta(hours=recommended)
        trade = {
            "id": str(uuid.uuid4())[:12],
            "pair": pair,
            "side": f["side"],
            "open_price": cp,
            "open_time": _now().isoformat(),
            "expiry_time": expiry_time.isoformat(),
            "expiry_hours": recommended,
            "stake_usd": config.STAKE_USD,
            "payout_pct": config.PAYOUT_PCT,
            "probability_pct_at_open": f["probability_pct"],
            "score_at_open": f["score"],
            "session_at_open": f.get("session"),
            "agents_for_count": f.get("agents_for_count", 0),
            "agents_against_count": f.get("agents_against_count", 0),
            "backtest_30d_wr_pct_at_open": (backtest.get("pairs") or {}).get(pair, {}).get("win_rate_pct"),
            "backtest_30d_trades_at_open": (backtest.get("pairs") or {}).get(pair, {}).get("trades"),
            "status": "open",
        }
        open_trades.append(trade)
        opened += 1
        log.info(f"OPEN {pair} {f['side']} @ {cp} prob={f['probability_pct']}% expiry={recommended}h")
    return opened


def _settle_expired(open_trades: list[dict], closed_trades: list[dict]) -> int:
    """Закрыть истёкшие сделки по реальной цене Yahoo."""
    settled = 0
    still_open = []
    for t in open_trades:
        if t["status"] != "open":
            continue
        expiry = datetime.fromisoformat(t["expiry_time"])
        if _now() < expiry:
            still_open.append(t)
            continue

        close_price = yahoo.settlement_price(t["pair"], expiry)
        if close_price is None:
            # данные ещё не готовы — попробуем в следующий цикл
            still_open.append(t)
            continue

        if t["side"] == "BUY":
            win = close_price > t["open_price"]
        else:
            win = close_price < t["open_price"]
        pnl = (config.STAKE_USD * config.PAYOUT_PCT) if win else (-config.STAKE_USD)

        t.update({
            "status": "closed",
            "close_price": close_price,
            "close_time": _now().isoformat(),
            "result": "WIN" if win else "LOSS",
            "pnl_usd": round(pnl, 2),
        })
        closed_trades.append(t)
        settled += 1
        log.info(
            f"CLOSE {t['pair']} {t['side']} open={t['open_price']} "
            f"close={close_price} → {'WIN' if win else 'LOSS'} pnl={pnl:+.2f}"
        )

    open_trades[:] = still_open
    return settled


def _refresh_stats(closed: list[dict]) -> dict:
    n = len(closed)
    wins = sum(1 for t in closed if t.get("result") == "WIN")
    losses = n - wins
    pnl = sum(float(t.get("pnl_usd", 0.0)) for t in closed)
    wr = (wins / n * 100.0) if n > 0 else 0.0
    stats = {
        "as_of": _now().isoformat(),
        "total": n,
        "wins": wins,
        "losses": losses,
        "win_rate_pct": round(wr, 2),
        "total_pnl_usd": round(pnl, 2),
    }
    _save(STATS_FILE, stats)
    return stats


def cycle_once() -> dict:
    snapshot = _load(FORECASTS_FILE, {"forecasts": {}, "rankings": []})
    backtest = _load(BACKTEST_FILE, {"pairs": {}})
    open_trades = _load(OPEN_FILE, [])
    closed = _load(CLOSED_FILE, [])

    settled = _settle_expired(open_trades, closed)
    opened = _open_new_trades(open_trades, snapshot, backtest)

    # обогащаем для UI и сохраняем
    enriched = [_enrich_open_trade(t) for t in open_trades]
    _save(OPEN_FILE, open_trades)
    _save(config.STATE_DIR / "open_trades_enriched.json", {
        "as_of": _now().isoformat(),
        "trades": enriched,
    })
    _save(CLOSED_FILE, closed)
    stats = _refresh_stats(closed)

    return {"opened": opened, "settled": settled, "stats": stats}


def run_loop() -> None:
    log.info("paper_trader start")
    stop = {"flag": False}

    def _sig(_a, _b):
        stop["flag"] = True

    signal.signal(signal.SIGTERM, _sig)
    signal.signal(signal.SIGINT, _sig)

    while not stop["flag"]:
        _heartbeat()
        try:
            r = cycle_once()
            log.info(f"cycle: opened={r['opened']} settled={r['settled']} stats={r['stats']}")
        except Exception as e:
            log.exception(f"cycle failed: {e}")
        _heartbeat()
        for _ in range(config.PAPER_TRADER_INTERVAL_SEC):
            if stop["flag"]:
                break
            time.sleep(1)
    log.info("paper_trader exit")


if __name__ == "__main__":
    run_loop()
