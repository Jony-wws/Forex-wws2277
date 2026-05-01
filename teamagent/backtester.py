"""backtester — реальный 365-дневный бэктест для каждой пары.

Цель: paper-trader использует исторический WR на 365 днях per pair как
второй «trust score». Гейт сделки = `forecast.probability_pct >= 70`
(free 70% gate с 2026-05-01); этот WR — диагностика «как реально вела себя
пара за год», не блокатор.

Алгоритм для одной пары:
  1. Скачиваем 1H историю за 2 года (yahoo period="2y", interval="1h") —
     чтобы было ~30 дней буфера для индикаторов перед началом окна.
  2. Берём последние 365 дней (≈8760 баров).
  3. Идём по часу: каждый час делаем «прогноз» точно той же логикой
     forecast_scanner.evaluate_pair(), но на срезе данных до этого часа.
  4. Если probability ≥ MIN_PROBABILITY и сделка по этой паре ещё не открыта —
     виртуально «открываем»: запоминаем entry_price + side + expiry =
     entry_time + recommended_hours.
  5. На время expiry сравниваем close-цену:
       BUY → WIN if close > entry, иначе LOSS
       SELL → WIN if close < entry, иначе LOSS
  6. Считаем wins / losses / win_rate, средний PnL ($50 stake, 85% payout).

Запускается раз в час отдельным процессом (state_committer-style).
Результат: state/backtest_30d.json (имя legacy — сохраняем для совместимости).
"""
from __future__ import annotations
import json
import logging
import math
import os
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pandas as pd

from . import config, indicators
from .data import yahoo, news

log = logging.getLogger("backtester")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.FileHandler(config.LOGS_DIR / "backtester.log"),
        logging.StreamHandler(sys.stdout),
    ],
)

OUTPUT_FILE = config.STATE_DIR / "backtest_30d.json"
HEARTBEAT_FILE = config.STATE_DIR / "heartbeat_backtester.json"

REFRESH_INTERVAL_SEC = 60 * 60   # раз в час
MIN_BARS_FOR_HISTORY = 200       # ниже — пропускаем пару
LOOKBACK_DAYS = 365              # 1 год реальной истории Yahoo 1H
STAKE_USD = 50.0
PAYOUT_PCT = 0.85


def _heartbeat(tick: int = 0) -> None:
    HEARTBEAT_FILE.write_text(json.dumps({
        "name": "backtester",
        "category": "system",
        "ts": datetime.now(timezone.utc).isoformat(),
        "pid": os.getpid(),
        "tick_count": tick,
    }))


def _sigmoid(x: float) -> float:
    return 1.0 / (1.0 + math.exp(-x))


def _score_to_prob(score: int) -> float:
    p = _sigmoid((score / 44) * 4.0)
    return max(0.50, min(config.MAX_PROBABILITY, p))


def _evaluate_slice(slice_4h: pd.DataFrame, slice_1h: pd.DataFrame, slice_15m: pd.DataFrame) -> tuple[int, int]:
    """Упрощённая версия forecast_scanner.evaluate_pair, которая возвращает
    (score, recommended_hours). Без VolumeProfile/news (исторический ForexFactory
    мы не качаем — это сильно дороже и редко влияет на 1H бэктест).
    """
    if slice_4h.empty or slice_1h.empty or slice_15m.empty:
        return 0, 1
    if len(slice_4h) < 30 or len(slice_1h) < 30 or len(slice_15m) < 30:
        return 0, 1

    ind_4h = indicators.all_indicators(slice_4h)
    ind_1h = indicators.all_indicators(slice_1h)
    ind_15m = indicators.all_indicators(slice_15m)
    if not ind_4h or not ind_1h or not ind_15m:
        return 0, 1

    score = 0

    # BLOCK A — структура старшего TF
    if ind_4h["close"] > ind_4h["ema50"] > ind_4h["ema200"]:
        score += 3
    elif ind_4h["close"] < ind_4h["ema50"] < ind_4h["ema200"]:
        score -= 3
    elif ind_4h["close"] > ind_4h["ema50"]:
        score += 1
    elif ind_4h["close"] < ind_4h["ema50"]:
        score -= 1

    if ind_1h["close"] > ind_1h["ema20"] > ind_1h["ema50"]:
        score += 2
    elif ind_1h["close"] < ind_1h["ema20"] < ind_1h["ema50"]:
        score -= 2

    if ind_15m["close"] > ind_15m["ema20"]:
        score += 1
    else:
        score -= 1

    # BLOCK B — RSI
    rsi = ind_1h["rsi14"]
    if 50 < rsi < 70:
        score += 2
    elif 30 < rsi < 50:
        score -= 2
    elif rsi >= 70:
        score -= 2
    elif rsi <= 30:
        score += 2

    # BLOCK C — Bollinger %B
    bb = ind_1h["bb_pct"]
    if bb > 0.95:
        score -= 1
    elif bb < 0.05:
        score += 1
    elif 0.5 < bb < 0.85:
        score += 1
    elif 0.15 < bb < 0.5:
        score -= 1

    # BLOCK D — Momentum
    if ind_1h["mom5"] > 0.1:
        score += 2
    elif ind_1h["mom5"] < -0.1:
        score -= 2

    # BLOCK E — CEI/OFI
    if ind_1h["cei10"] > 60 and ind_1h["ofi10"] > 0.3:
        score += 2
    elif ind_1h["cei10"] > 60 and ind_1h["ofi10"] < -0.3:
        score -= 2

    # BLOCK F — VWAP
    if ind_1h["close"] > ind_1h["vwap"] * 1.001:
        score += 1
    elif ind_1h["close"] < ind_1h["vwap"] * 0.999:
        score -= 1

    # BLOCK G — BBP regime
    if ind_1h["bbp"] > 0:
        score += 1
    elif ind_1h["bbp"] < 0:
        score -= 1

    # BLOCK H — multi-TF consensus
    bull_count = (
        int(ind_4h["close"] > ind_4h["ema50"])
        + int(ind_1h["close"] > ind_1h["ema20"])
        + int(ind_15m["close"] > ind_15m["ema20"])
    )
    if bull_count == 3:
        score += 3
    elif bull_count == 0:
        score -= 3

    # recommended_hours
    abs_norm = min(1.0, abs(score) / 20.0)
    rec_hours = int(round(config.MIN_EXPIRY_HOURS + abs_norm * (config.MAX_EXPIRY_HOURS - config.MIN_EXPIRY_HOURS)))
    rec_hours = max(config.MIN_EXPIRY_HOURS, min(config.MAX_EXPIRY_HOURS, rec_hours))

    return score, rec_hours


def backtest_pair(pair: str) -> dict:
    """Один прогон бэктеста по одной паре. Возвращает dict со статистикой."""
    # 2y нужно для LOOKBACK_DAYS=365 + ~30 дней буфера на индикаторы
    bars = yahoo.fetch(pair, interval="1h", period="2y")
    if bars is None or bars.empty or len(bars) < MIN_BARS_FOR_HISTORY:
        return {
            "pair": pair,
            "trades": 0,
            "wins": 0,
            "losses": 0,
            "win_rate_pct": None,
            "total_pnl_usd": 0.0,
            "avg_score": None,
            "note": f"insufficient history ({len(bars) if bars is not None else 0} bars)",
        }

    # отрезаем последние LOOKBACK_DAYS дней по дате (а не по числу баров — на случай выходных и пропусков)
    cutoff = bars.index[-1] - timedelta(days=LOOKBACK_DAYS)
    backtest_window_start_idx = bars.index.searchsorted(cutoff)
    if backtest_window_start_idx >= len(bars) - 5:
        return {
            "pair": pair,
            "trades": 0,
            "wins": 0,
            "losses": 0,
            "win_rate_pct": None,
            "total_pnl_usd": 0.0,
            "avg_score": None,
            "note": "not enough recent bars",
        }

    # для 4H приближённо ресемплируем из 1H
    bars_4h = bars.resample("4h").agg({
        "Open": "first", "High": "max", "Low": "min", "Close": "last", "Volume": "sum",
    }).dropna()
    # для 15m yahoo держит только 60 дней — используем 1H для 15m тоже (грубо, но не страшно для бэктеста на 365д)
    bars_15m = bars

    open_trades: list[dict] = []
    closed: list[dict] = []
    scores: list[int] = []

    for idx in range(backtest_window_start_idx, len(bars)):
        ts = bars.index[idx]

        # сначала закрываем те, у которых наступило время
        still_open = []
        for t in open_trades:
            if ts >= t["expiry"]:
                close_price = float(bars.iloc[idx]["Close"])
                if t["side"] == "BUY":
                    win = close_price > t["entry_price"]
                else:
                    win = close_price < t["entry_price"]
                pnl = STAKE_USD * PAYOUT_PCT if win else -STAKE_USD
                closed.append({
                    **t,
                    "close_time": ts.isoformat(),
                    "close_price": close_price,
                    "result": "WIN" if win else "LOSS",
                    "pnl_usd": pnl,
                })
            else:
                still_open.append(t)
        open_trades = still_open

        # пытаемся открыть новую (по одной активной сделке на пару — как в реале)
        if any(True for _ in open_trades):
            continue

        # срезы для аналитики (только история ДО этого момента)
        slice_1h = bars.iloc[max(0, idx - 100):idx]
        # 4H берём по реальной дате
        slice_4h = bars_4h.loc[bars_4h.index <= ts].tail(240)
        slice_15m = bars_15m.iloc[max(0, idx - 100):idx]

        score, rec_hours = _evaluate_slice(slice_4h, slice_1h, slice_15m)
        if score == 0:
            continue
        prob = _score_to_prob(abs(score))
        if prob < config.MIN_PROBABILITY:
            continue

        scores.append(score)
        side = "BUY" if score > 0 else "SELL"
        entry_price = float(bars.iloc[idx]["Close"])
        expiry = ts + timedelta(hours=rec_hours)
        open_trades.append({
            "pair": pair,
            "side": side,
            "score": score,
            "probability_pct": round(prob * 100.0, 1),
            "open_time": ts.isoformat(),
            "entry_price": entry_price,
            "expiry": expiry,
            "recommended_hours": rec_hours,
        })

    # сделки, которые остались не закрытыми (хвост окна) — закрываем по последней цене
    last_idx = len(bars) - 1
    last_ts = bars.index[last_idx]
    last_price = float(bars.iloc[last_idx]["Close"])
    for t in open_trades:
        if t["side"] == "BUY":
            win = last_price > t["entry_price"]
        else:
            win = last_price < t["entry_price"]
        pnl = STAKE_USD * PAYOUT_PCT if win else -STAKE_USD
        closed.append({
            **t,
            "close_time": last_ts.isoformat(),
            "close_price": last_price,
            "result": "WIN" if win else "LOSS",
            "pnl_usd": pnl,
            "note": "closed at end-of-window",
        })

    wins = sum(1 for t in closed if t["result"] == "WIN")
    losses = sum(1 for t in closed if t["result"] == "LOSS")
    total = wins + losses
    wr = round((wins / total) * 100.0, 1) if total else None
    pnl_total = round(sum(t["pnl_usd"] for t in closed), 2)
    avg_score = round(sum(scores) / len(scores), 2) if scores else None

    return {
        "pair": pair,
        "trades": total,
        "wins": wins,
        "losses": losses,
        "win_rate_pct": wr,
        "total_pnl_usd": pnl_total,
        "avg_score": avg_score,
    }


def run_full_backtest() -> dict:
    log.info("backtester: начинаю прогон по %d парам", len(config.PAIRS))
    results = {}
    started = datetime.now(timezone.utc)
    for i, pair in enumerate(config.PAIRS, 1):
        try:
            r = backtest_pair(pair)
            results[pair] = r
            log.info(
                "[%2d/%d] %s trades=%s wins=%s WR=%s%% pnl=%s",
                i, len(config.PAIRS), pair,
                r.get("trades"), r.get("wins"), r.get("win_rate_pct"), r.get("total_pnl_usd"),
            )
        except Exception as e:
            log.exception("backtest_pair failed pair=%s: %s", pair, e)
            results[pair] = {"pair": pair, "trades": 0, "win_rate_pct": None, "error": str(e)}
        _heartbeat(i)

    # суммарка
    total_trades = sum((r.get("trades") or 0) for r in results.values())
    total_wins = sum((r.get("wins") or 0) for r in results.values())
    total_pnl = round(sum((r.get("total_pnl_usd") or 0.0) for r in results.values()), 2)
    overall_wr = round((total_wins / total_trades) * 100.0, 1) if total_trades else None
    qualified = [
        p for p, r in results.items()
        if (r.get("win_rate_pct") is not None and r["win_rate_pct"] >= 70 and (r.get("trades") or 0) >= 5)
    ]
    summary = {
        "total_trades": total_trades,
        "total_wins": total_wins,
        "total_pnl_usd": total_pnl,
        "overall_win_rate_pct": overall_wr,
        "qualified_pairs": qualified,
        "qualified_count": len(qualified),
        "min_trades_to_qualify": 5,
        "min_win_rate_to_qualify_pct": 70,
        "lookback_days": LOOKBACK_DAYS,
    }

    finished = datetime.now(timezone.utc)
    payload = {
        "as_of": finished.isoformat(),
        "started_at": started.isoformat(),
        "duration_sec": int((finished - started).total_seconds()),
        "lookback_days": LOOKBACK_DAYS,
        "stake_usd": STAKE_USD,
        "payout_pct": PAYOUT_PCT,
        "summary": summary,
        "pairs": results,
    }
    OUTPUT_FILE.write_text(json.dumps(payload, indent=2, default=str))
    log.info(
        "backtester: готово. WR=%s%% trades=%d qualified=%d/%d",
        overall_wr, total_trades, len(qualified), len(config.PAIRS),
    )
    return payload


def run_loop() -> None:
    log.info("backtester loop start (interval=%ds)", REFRESH_INTERVAL_SEC)
    tick = 0
    while True:
        tick += 1
        _heartbeat(tick)
        try:
            run_full_backtest()
        except Exception as e:
            log.exception("backtester run crashed: %s", e)
        # сон с регулярным heartbeat-ом
        for _ in range(REFRESH_INTERVAL_SEC // 60):
            _heartbeat(tick)
            time.sleep(60)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "once":
        run_full_backtest()
    else:
        run_loop()
