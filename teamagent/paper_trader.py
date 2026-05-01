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

from . import config, strategies
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
STRATEGY_CONFIG_FILE = config.STATE_DIR / "strategy_config.json"
HEARTBEAT_FILE = config.STATE_DIR / "heartbeat_paper_trader.json"

# Gate: сделка открывается только если:
#   forecast.probability >= MIN_PROBABILITY И
#   strategy_config[pair].best_variant.win_rate_pct >= MIN_WR_PCT (на этой паре лучшая стратегия даёт ≥7О% WR на 30д) И
#   forecast сейчас удовлетворяет фильтры этого best_variant (сессия, |score|, prob).
# Это и есть «реальный 70% WR», а не теоретическая probability прямо из scanner-а.
MIN_WR_PCT = 70.0
MIN_TRADES = 5
# fallback на backtest_30d.json если strategy_config ещё не рассчитан
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
    """Fallback: проверить backtest_30d.json (baseline-вариант) если strategy_config отсутствует."""
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


def _strategy_qualified(pair: str, ts: datetime, score: float, prob_pct: float,
                         baseline_side: str, rsi_1h: float | None,
                         strategy_cfg: dict) -> tuple[bool, dict]:
    """Основной гейт: проверяет что для пары есть лучшая стратегия с ≥70% WR
    в ТЕКУЩЕЙ сессии (Asia/London/Overlap/NY) на 30д бэктесте, И текущий сигнал
    соответствует фильтрам той стратегии.

    Логика выбора стратегии:
    1. Определяем текущую сессию по `ts.hour` (`strategies.detect_session`).
    2. Если `strategy_cfg.pairs[pair].by_session[session].qualifies_70pct=True` —
       берём её best_variant.
    3. Иначе — фолбэк на общий best_variant (`strategy_cfg.pairs[pair]`),
       но ТОЛЬКО если он сам qualifies_70pct.
    4. Иначе — пропускаем (пара/сессия заморожена).

    Возвращает в info также recommended `side` (с учётом contrarian/fade_extreme_rsi),
    `session`, `variant`, `win_rate_pct`.
    """
    pairs = (strategy_cfg or {}).get("pairs") or {}
    p = pairs.get(pair)
    if not p:
        return False, {"reason": "strategy_search ещё не запускался для этой пары"}

    # шаг 1 — текущая сессия
    sess_name = strategies.detect_session(ts.hour)
    if sess_name is None:
        return False, {
            "reason": f"вне канонических сессий (час {ts.hour} UTC = 22-23 off-hours)",
            "win_rate_pct": p.get("win_rate_pct"),
        }

    # шаг 2 — per-session best
    by_session = p.get("by_session") or {}
    session_cfg = by_session.get(sess_name) or {}
    chosen_source = "by_session"
    chosen = None
    if session_cfg.get("qualifies_70pct") and (session_cfg.get("trades") or 0) >= MIN_TRADES:
        chosen = session_cfg
    elif p.get("qualifies_70pct") and (p.get("trades") or 0) >= MIN_TRADES:
        # шаг 3 — фолбэк на общий best_variant (без session фильтра)
        chosen = p
        chosen_source = "global_best"
    else:
        # шаг 4 — заморожено
        return False, {
            "reason": (
                f"в сессии {sess_name} лучшая стратегия даёт "
                f"{session_cfg.get('win_rate_pct')}% WR (trades={session_cfg.get('trades')}); "
                f"общий best — {p.get('win_rate_pct')}% (trades={p.get('trades')})"
            ),
            "session": sess_name,
            "win_rate_pct_session": session_cfg.get("win_rate_pct"),
            "trades_session": session_cfg.get("trades"),
            "win_rate_pct_global": p.get("win_rate_pct"),
            "trades_global": p.get("trades"),
            "variant_session": session_cfg.get("best_variant"),
            "variant_global": p.get("best_variant"),
        }

    variants = strategies.variants_by_id()
    variant = variants.get(chosen.get("best_variant"))
    if variant is None:
        return False, {"reason": f"unknown variant id {chosen.get('best_variant')}"}

    # Эффективный score (с учётом contrarian / fade_extreme_rsi)
    effective_score = score
    if variant.fade_extreme_rsi and rsi_1h is not None and (rsi_1h <= 25 or rsi_1h >= 75):
        effective_score = -effective_score
    if variant.contrarian:
        effective_score = -effective_score

    # 1) variant.session_utc — если у варианта есть СВОЙ session filter,
    #    он применяется поверх (на практике редко конфликтует с per-session search,
    #    поскольку per-session search фильтрует часы извне).
    if variant.session_utc is not None:
        h = ts.hour
        s, e = variant.session_utc
        if s <= e:
            in_window = s <= h < e
        else:
            in_window = h >= s or h < e
        if not in_window:
            return False, {
                "reason": f"current hour {h} UTC не попадает в session_utc варианта {variant.session_utc}",
                "variant": variant.id,
                "win_rate_pct": chosen.get("win_rate_pct"),
                "session": sess_name,
            }
    # 2) min_abs_score (на effective_score)
    if abs(effective_score) < variant.min_abs_score:
        return False, {
            "reason": f"|effective_score|={abs(effective_score)} < {variant.min_abs_score}",
            "variant": variant.id, "win_rate_pct": chosen.get("win_rate_pct"),
            "session": sess_name,
        }
    # 3) min_probability (probability — функция от |score|, симметрична)
    if prob_pct / 100.0 < variant.min_probability:
        return False, {
            "reason": f"prob={prob_pct}% < {variant.min_probability * 100}%",
            "variant": variant.id, "win_rate_pct": chosen.get("win_rate_pct"),
            "session": sess_name,
        }

    # сторона: variant определяет направление (с учётом флипа)
    side = "BUY" if effective_score > 0 else "SELL"
    return True, {
        "variant": variant.id,
        "variant_label": variant.label,
        "win_rate_pct": chosen.get("win_rate_pct"),
        "trades": chosen.get("trades"),
        "side": side,
        "effective_score": effective_score,
        "session": sess_name,
        "chosen_source": chosen_source,  # "by_session" или "global_best"
    }


def _open_new_trades(open_trades: list[dict], snapshot: dict, backtest: dict, strategy_cfg: dict) -> int:
    """Открыть виртуальные сделки по прогнозам, проходящим все гейты:
      forecast.probability_pct >= 70
      Лучшая стратегия пары имеет WR ≥ 70% на 30д (strategy_search)
      Текущий сигнал удовлетворяет фильтры той стратегии.
    """
    rankings = snapshot.get("rankings", [])
    forecasts = snapshot.get("forecasts", {})
    has_strategy_cfg = bool((strategy_cfg or {}).get("pairs"))
    opened = 0
    now_ts = _now()
    for r in rankings:
        if r["probability_pct"] < config.MIN_PROBABILITY * 100.0:
            continue
        pair = r["pair"]
        if _is_open_for_pair(open_trades, pair):
            continue

        f = forecasts.get(pair)
        if not f:
            continue

        # Главный гейт — strategy_search результат
        rsi_1h = ((f.get("indicators") or {}).get("1H") or {}).get("rsi14")
        if has_strategy_cfg:
            ok, why = _strategy_qualified(
                pair, now_ts,
                score=r.get("score", 0),
                prob_pct=r["probability_pct"],
                baseline_side=f.get("side", "BUY"),
                rsi_1h=rsi_1h,
                strategy_cfg=strategy_cfg,
            )
        else:
            # fallback: старый backtest_30d gate
            ok, why = _backtest_qualified(pair, backtest)
        if not ok:
            log.info(f"SKIP {pair} prob={r['probability_pct']}% — gate: {why}")
            continue

        cp = yahoo.latest_price(pair)
        if cp is None:
            continue
        # стратегия может развернуть сторону (contrarian / fade_extreme_rsi)
        trade_side = (why or {}).get("side") or f.get("side")

        # выбранный вариант может прийти из by_session или global; берём из `why`
        chosen_variant_id = (why or {}).get("variant")
        chosen_session = (why or {}).get("session")
        chosen_source = (why or {}).get("chosen_source")
        chosen_wr = (why or {}).get("win_rate_pct")
        chosen_trades = (why or {}).get("trades")

        # expiry: из выбранного variant если зафиксирована, иначе из forecast.recommended_hours
        recommended = f.get("recommended_hours", config.DEFAULT_EXPIRY_HOURS)
        if chosen_variant_id:
            variants = strategies.variants_by_id()
            v = variants.get(chosen_variant_id)
            if v is not None and v.fixed_expiry_h is not None:
                recommended = v.fixed_expiry_h

        expiry_time = now_ts + timedelta(hours=recommended)
        per_pair_cfg = (strategy_cfg.get("pairs") or {}).get(pair, {}) if has_strategy_cfg else {}
        trade = {
            "id": str(uuid.uuid4())[:12],
            "pair": pair,
            "side": trade_side,
            "open_price": cp,
            "open_time": now_ts.isoformat(),
            "expiry_time": expiry_time.isoformat(),
            "expiry_hours": recommended,
            "stake_usd": config.STAKE_USD,
            "payout_pct": config.PAYOUT_PCT,
            "probability_pct_at_open": f["probability_pct"],
            "score_at_open": f["score"],
            "session_at_open": chosen_session or f.get("session"),
            "agents_for_count": f.get("agents_for_count", 0),
            "agents_against_count": f.get("agents_against_count", 0),
            "strategy_variant_at_open": chosen_variant_id or per_pair_cfg.get("best_variant"),
            "strategy_wr_pct_at_open": chosen_wr if chosen_wr is not None else per_pair_cfg.get("win_rate_pct"),
            "strategy_trades_at_open": chosen_trades,
            "strategy_source_at_open": chosen_source,  # "by_session" или "global_best"
            "backtest_30d_wr_pct_at_open": (backtest.get("pairs") or {}).get(pair, {}).get("win_rate_pct"),
            "backtest_30d_trades_at_open": (backtest.get("pairs") or {}).get(pair, {}).get("trades"),
            "status": "open",
        }
        open_trades.append(trade)
        opened += 1
        log.info(f"OPEN {pair} {trade_side} @ {cp} prob={f['probability_pct']}% "
                 f"expiry={recommended}h variant={chosen_variant_id} "
                 f"session={chosen_session} src={chosen_source} WR30d={chosen_wr}%")
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
    strategy_cfg = _load(STRATEGY_CONFIG_FILE, {"pairs": {}})
    open_trades = _load(OPEN_FILE, [])
    closed = _load(CLOSED_FILE, [])

    settled = _settle_expired(open_trades, closed)
    opened = _open_new_trades(open_trades, snapshot, backtest, strategy_cfg)

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
