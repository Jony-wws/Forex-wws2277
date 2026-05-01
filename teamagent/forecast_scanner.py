"""Forecast scanner — ЕДИНЫЙ источник правды для 28 пар × 5-мин цикл.

Никакого «отдельно мета-голосование, отдельно ПРОГНОЗЫ» — теперь это ОДНА таблица.
Каждый прогноз содержит:
- pair, side (BUY/SELL), probability (capped 50–92%)
- recommended_hours (1–4)
- score (вклад каждого правила, для прозрачности)
- agents_for / agents_against (мета-голосование интегрировано сюда же)
- volume_profile snapshot
- timestamp
"""
from __future__ import annotations
import json
import logging
import math
import time
import signal
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pandas as pd

from . import config, indicators, volume_profile
from .data import yahoo, news

log = logging.getLogger("scanner")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.FileHandler(config.LOGS_DIR / "forecast_scanner.log"),
        logging.StreamHandler(sys.stdout),
    ],
)

FORECASTS_FILE = config.STATE_DIR / "forecasts.json"
HEARTBEAT_FILE = config.STATE_DIR / "heartbeat_forecast_scanner.json"


def _heartbeat() -> None:
    HEARTBEAT_FILE.write_text(json.dumps({
        "name": "forecast_scanner",
        "ts": datetime.now(timezone.utc).isoformat(),
        "pid": __import__("os").getpid(),
    }))


def _sigmoid(x: float) -> float:
    return 1.0 / (1.0 + math.exp(-x))


def _score_to_probability(score: int, max_score: int = 44) -> float:
    """Score -44..+44 → probability 0..1, абсолютная.
    Score>0 → BUY вероятность; <0 → SELL вероятность.
    """
    norm = score / max_score              # -1..+1
    p = _sigmoid(norm * 4.0)              # softer sigmoid
    return p


def evaluate_pair(pair: str) -> dict | None:
    """Полная оценка одной пары: TF 4H + 1H + 15m + Volume Profile + новости."""
    # данные
    bars_4h = yahoo.latest_bars(pair, "1h", 240)        # 240×1h ≈ 10 дней
    bars_1h = yahoo.latest_bars(pair, "1h", 100)
    bars_15m = yahoo.latest_bars(pair, "15m", 100)
    if any(df.empty or len(df) < 30 for df in (bars_4h, bars_1h, bars_15m)):
        log.warning(f"{pair}: not enough bars")
        return None

    ind_4h = indicators.all_indicators(bars_4h)
    ind_1h = indicators.all_indicators(bars_1h)
    ind_15m = indicators.all_indicators(bars_15m)

    if not ind_4h or not ind_1h or not ind_15m:
        return None

    score = 0
    score_breakdown: list[dict] = []
    agents_for: list[str] = []
    agents_against: list[str] = []

    def vote(name: str, contrib: int, reason: str) -> None:
        nonlocal score
        score += contrib
        score_breakdown.append({"name": name, "contrib": contrib, "reason": reason})
        if contrib > 0:
            agents_for.append(name)
        elif contrib < 0:
            agents_against.append(name)

    # ───── BLOCK A — VWAP / EMA / структура старшего TF ─────
    if ind_4h["close"] > ind_4h["ema50"] > ind_4h["ema200"]:
        vote("4H_strong_uptrend", +3, "close > ema50 > ema200 (4H)")
    elif ind_4h["close"] < ind_4h["ema50"] < ind_4h["ema200"]:
        vote("4H_strong_downtrend", -3, "close < ema50 < ema200 (4H)")
    elif ind_4h["close"] > ind_4h["ema50"]:
        vote("4H_uptrend", +1, "close > ema50 (4H)")
    elif ind_4h["close"] < ind_4h["ema50"]:
        vote("4H_downtrend", -1, "close < ema50 (4H)")

    # 1H confirmation
    if ind_1h["close"] > ind_1h["ema20"] > ind_1h["ema50"]:
        vote("1H_uptrend", +2, "close > ema20 > ema50 (1H)")
    elif ind_1h["close"] < ind_1h["ema20"] < ind_1h["ema50"]:
        vote("1H_downtrend", -2, "close < ema20 < ema50 (1H)")

    # 15m alignment / entry
    if ind_15m["close"] > ind_15m["ema20"]:
        vote("15m_above_ema20", +1, "close > ema20 (15m)")
    else:
        vote("15m_below_ema20", -1, "close < ema20 (15m)")

    # ───── BLOCK B — RSI ─────
    if 50 < ind_1h["rsi14"] < 70:
        vote("1H_rsi_bullish", +2, f"RSI={ind_1h['rsi14']:.1f}")
    elif 30 < ind_1h["rsi14"] < 50:
        vote("1H_rsi_bearish", -2, f"RSI={ind_1h['rsi14']:.1f}")
    elif ind_1h["rsi14"] >= 70:
        vote("1H_rsi_overbought", -2, f"RSI={ind_1h['rsi14']:.1f} (откат вниз)")
    elif ind_1h["rsi14"] <= 30:
        vote("1H_rsi_oversold", +2, f"RSI={ind_1h['rsi14']:.1f} (отскок вверх)")

    # ───── BLOCK C — Bollinger %B ─────
    if ind_1h["bb_pct"] > 0.95:
        vote("1H_bb_overbought", -1, f"%B={ind_1h['bb_pct']:.2f}")
    elif ind_1h["bb_pct"] < 0.05:
        vote("1H_bb_oversold", +1, f"%B={ind_1h['bb_pct']:.2f}")
    elif 0.5 < ind_1h["bb_pct"] < 0.85:
        vote("1H_bb_above_mid", +1, f"%B={ind_1h['bb_pct']:.2f}")
    elif 0.15 < ind_1h["bb_pct"] < 0.5:
        vote("1H_bb_below_mid", -1, f"%B={ind_1h['bb_pct']:.2f}")

    # ───── BLOCK D — Momentum ─────
    if ind_1h["mom5"] > 0.1:
        vote("1H_momentum_up", +2, f"mom5={ind_1h['mom5']:.2f}%")
    elif ind_1h["mom5"] < -0.1:
        vote("1H_momentum_down", -2, f"mom5={ind_1h['mom5']:.2f}%")

    # ───── BLOCK E — CEI / OFI ─────
    if ind_1h["cei10"] > 60 and ind_1h["ofi10"] > 0.3:
        vote("1H_strong_bull_candles", +2, f"CEI={ind_1h['cei10']:.0f}% OFI={ind_1h['ofi10']:+.2f}")
    elif ind_1h["cei10"] > 60 and ind_1h["ofi10"] < -0.3:
        vote("1H_strong_bear_candles", -2, f"CEI={ind_1h['cei10']:.0f}% OFI={ind_1h['ofi10']:+.2f}")

    # ───── BLOCK F — VWAP relation ─────
    if ind_1h["close"] > ind_1h["vwap"] * 1.001:
        vote("1H_above_vwap", +1, "close выше VWAP")
    elif ind_1h["close"] < ind_1h["vwap"] * 0.999:
        vote("1H_below_vwap", -1, "close ниже VWAP")

    # ───── BLOCK G — BBP regime ─────
    if ind_1h["bbp"] > 0:
        vote("1H_bbp_bull", +1, f"BBP={ind_1h['bbp']:.5f}")
    elif ind_1h["bbp"] < 0:
        vote("1H_bbp_bear", -1, f"BBP={ind_1h['bbp']:.5f}")

    # ───── BLOCK H — Multi-TF agreement (бонус) ─────
    bull_count = int(ind_4h["close"] > ind_4h["ema50"]) \
               + int(ind_1h["close"] > ind_1h["ema20"]) \
               + int(ind_15m["close"] > ind_15m["ema20"])
    if bull_count == 3:
        vote("MTF_full_bull", +3, "все 3 TF выше EMA")
    elif bull_count == 0:
        vote("MTF_full_bear", -3, "все 3 TF ниже EMA")

    # ───── PENALTY: news blackout ─────
    # high-impact новость ±30 мин: снижаем confidence обеих сторон,
    # уменьшая abs(score) на величину penalty (но не ниже нуля).
    now = datetime.now(timezone.utc)
    if news.is_blackout(pair, now):
        penalty = min(config.NEWS_BLACKOUT_PENALTY, abs(score))
        delta = -penalty if score > 0 else (penalty if score < 0 else 0)
        vote("news_blackout", delta, f"high-impact новость ±30 мин — снижаем abs(score) на {penalty}")

    # ───── итог ─────
    if score == 0:
        return None  # нейтрально, не показываем

    side = "BUY" if score > 0 else "SELL"
    p_raw = _score_to_probability(abs(score), 44)
    # cap 50–92
    p = max(0.50, min(config.MAX_PROBABILITY, p_raw))

    # рекомендованная экспирация: больше score → дольше держим
    abs_norm = min(1.0, abs(score) / 20.0)
    recommended_hours = int(round(config.MIN_EXPIRY_HOURS + abs_norm * (config.MAX_EXPIRY_HOURS - config.MIN_EXPIRY_HOURS)))
    recommended_hours = max(config.MIN_EXPIRY_HOURS, min(config.MAX_EXPIRY_HOURS, recommended_hours))

    # volume profile snapshot
    try:
        vp = volume_profile.build(pair)
    except Exception as e:
        log.warning(f"VP failed pair={pair}: {e}")
        vp = {"error": str(e)}

    forecast = {
        "pair": pair,
        "side": side,
        "probability": round(p, 4),
        "probability_pct": round(p * 100.0, 1),
        "score": score,
        "max_score": 44,
        "recommended_hours": recommended_hours,
        "current_price": ind_15m["close"],
        "indicators": {
            "4H": ind_4h,
            "1H": ind_1h,
            "15m": ind_15m,
        },
        "score_breakdown": score_breakdown,
        "agents_for": agents_for,
        "agents_against": agents_against,
        "agents_for_count": len(agents_for),
        "agents_against_count": len(agents_against),
        "volume_profile": vp,
        "as_of": now.isoformat(),
        "session": _current_session(now.hour),
    }
    return forecast


def _current_session(hour: int) -> str:
    for name, (lo, hi) in config.SESSIONS.items():
        if lo <= hour <= hi:
            return name
    return "Off"


def scan_all_pairs() -> dict:
    """Полный обход 28 пар. Сохраняет общий snapshot в state/forecasts.json."""
    snapshot = {
        "scanned_at": datetime.now(timezone.utc).isoformat(),
        "total_pairs": len(config.PAIRS),
        "forecasts": {},
        "rankings": [],
    }
    for pair in config.PAIRS:
        try:
            f = evaluate_pair(pair)
        except Exception as e:
            log.exception(f"evaluate_pair failed pair={pair}: {e}")
            continue
        if f is None:
            continue
        snapshot["forecasts"][pair] = f
        snapshot["rankings"].append({
            "pair": pair,
            "side": f["side"],
            "probability_pct": f["probability_pct"],
            "score": f["score"],
            "recommended_hours": f["recommended_hours"],
            # vote breakdown в выжимке тоже — иначе на дашборде пары показывают 0/0
            "agents_for_count": f.get("agents_for_count", 0),
            "agents_against_count": f.get("agents_against_count", 0),
        })
    snapshot["rankings"].sort(key=lambda x: x["probability_pct"], reverse=True)
    FORECASTS_FILE.write_text(json.dumps(snapshot, indent=2))
    log.info(
        f"scanned {len(config.PAIRS)} pairs, got {len(snapshot['forecasts'])} forecasts; "
        f"top: {snapshot['rankings'][:3]}"
    )
    return snapshot


def run_loop(interval_sec: int | None = None) -> None:
    interval_sec = interval_sec or config.FORECAST_SCANNER_INTERVAL_SEC
    log.info(f"forecast_scanner start (interval={interval_sec}s, pairs={len(config.PAIRS)})")

    stop = {"flag": False}

    def _sig(_a, _b):
        stop["flag"] = True
        log.info("forecast_scanner: SIGTERM/SIGINT — stopping")

    signal.signal(signal.SIGTERM, _sig)
    signal.signal(signal.SIGINT, _sig)

    while not stop["flag"]:
        _heartbeat()
        try:
            scan_all_pairs()
        except Exception as e:
            log.exception(f"scan_all_pairs failed: {e}")
        _heartbeat()
        # дробим sleep чтобы быстрее реагировать на SIGTERM
        for _ in range(interval_sec):
            if stop["flag"]:
                break
            time.sleep(1)

    log.info("forecast_scanner exit")


if __name__ == "__main__":
    run_loop()
