"""Signal analyzer - multi-timeframe technical analysis engine.

Generates BUY/SELL signals with confidence levels.
Only shows signal when confidence >= 80%.
"""
from __future__ import annotations

import logging
import math
from datetime import datetime

from . import indicators
from .prices import fetch_bars

log = logging.getLogger("analyzer")


def _sigmoid(x: float) -> float:
    return 1.0 / (1.0 + math.exp(-x))


def analyze_pair(pair: str) -> dict | None:
    """Full analysis of one pair across 3 timeframes."""
    bars_4h = fetch_bars(pair, "1h", "1mo")    # ~240 1h bars
    bars_1h = fetch_bars(pair, "1h", "5d")     # ~120 1h bars
    bars_15m = fetch_bars(pair, "15m", "5d")   # ~480 15m bars

    if any(df.empty or len(df) < 30 for df in (bars_4h, bars_1h, bars_15m)):
        return None

    ind_4h = indicators.compute_all(bars_4h)
    ind_1h = indicators.compute_all(bars_1h)
    ind_15m = indicators.compute_all(bars_15m)

    if not ind_4h or not ind_1h or not ind_15m:
        return None

    score = 0
    details: list[dict] = []
    total_checks = 0

    def vote(name: str, contrib: int, reason: str) -> None:
        nonlocal score, total_checks
        score += contrib
        total_checks += 1
        details.append({"name": name, "value": contrib, "reason": reason})

    # === BLOCK A: Trend structure (4H) ===
    if ind_4h["close"] > ind_4h["ema50"] > ind_4h["ema200"]:
        vote("4H_trend", +3, "Сильный восходящий тренд")
    elif ind_4h["close"] < ind_4h["ema50"] < ind_4h["ema200"]:
        vote("4H_trend", -3, "Сильный нисходящий тренд")
    elif ind_4h["close"] > ind_4h["ema50"]:
        vote("4H_trend", +1, "Умеренный рост")
    elif ind_4h["close"] < ind_4h["ema50"]:
        vote("4H_trend", -1, "Умеренное снижение")

    # === BLOCK B: 1H Trend confirmation ===
    if ind_1h["close"] > ind_1h["ema20"] > ind_1h["ema50"]:
        vote("1H_trend", +2, "Подтверждение роста на 1H")
    elif ind_1h["close"] < ind_1h["ema20"] < ind_1h["ema50"]:
        vote("1H_trend", -2, "Подтверждение снижения на 1H")

    # === BLOCK C: 15m Entry ===
    if ind_15m["close"] > ind_15m["ema20"]:
        vote("15m_entry", +1, "Цена выше EMA20 на 15М")
    else:
        vote("15m_entry", -1, "Цена ниже EMA20 на 15М")

    # === BLOCK D: RSI ===
    rsi_val = ind_1h["rsi14"]
    if 50 < rsi_val < 70:
        vote("RSI", +2, f"RSI={rsi_val:.0f} — бычья зона")
    elif 30 < rsi_val < 50:
        vote("RSI", -2, f"RSI={rsi_val:.0f} — медвежья зона")
    elif rsi_val >= 70:
        vote("RSI", -2, f"RSI={rsi_val:.0f} — перекупленность")
    elif rsi_val <= 30:
        vote("RSI", +2, f"RSI={rsi_val:.0f} — перепроданность")

    # === BLOCK E: Bollinger Bands ===
    bb = ind_1h["bb_pct"]
    if bb > 0.95:
        vote("Bollinger", -1, "Цена у верхней границы")
    elif bb < 0.05:
        vote("Bollinger", +1, "Цена у нижней границы")
    elif bb > 0.5:
        vote("Bollinger", +1, "Цена выше средней")
    else:
        vote("Bollinger", -1, "Цена ниже средней")

    # === BLOCK F: MACD ===
    macd_h = ind_1h["macd_hist"]
    macd_prev = ind_1h["macd_prev_hist"]
    if macd_h > 0 and macd_prev <= 0:
        vote("MACD", +3, "MACD пересечение вверх")
    elif macd_h < 0 and macd_prev >= 0:
        vote("MACD", -3, "MACD пересечение вниз")
    elif macd_h > 0 and macd_h > macd_prev:
        vote("MACD", +1, "MACD растёт")
    elif macd_h < 0 and macd_h < macd_prev:
        vote("MACD", -1, "MACD падает")

    # === BLOCK G: Stochastic ===
    sk, sd = ind_1h["stoch_k"], ind_1h["stoch_d"]
    if sk < 20 and sd < 20:
        vote("Stochastic", +2, "Перепроданность")
    elif sk > 80 and sd > 80:
        vote("Stochastic", -2, "Перекупленность")
    elif sk > sd and sk < 80:
        vote("Stochastic", +1, "Бычий сигнал")
    elif sk < sd and sk > 20:
        vote("Stochastic", -1, "Медвежий сигнал")

    # === BLOCK H: ADX ===
    adx_val = ind_1h["adx"]
    if adx_val < 15:
        penalty = int(round(abs(score) * 0.5))
        if penalty > 0:
            if score > 0:
                vote("ADX", -penalty, f"ADX={adx_val:.0f} — слабый тренд, штраф")
            elif score < 0:
                vote("ADX", +penalty, f"ADX={adx_val:.0f} — слабый тренд, штраф")
    elif adx_val > 30:
        if ind_1h["plus_di"] > ind_1h["minus_di"]:
            vote("ADX", +3, f"ADX={adx_val:.0f} — сильный рост")
        else:
            vote("ADX", -3, f"ADX={adx_val:.0f} — сильное падение")
    elif adx_val > 20:
        if ind_1h["plus_di"] > ind_1h["minus_di"]:
            vote("ADX", +1, f"ADX={adx_val:.0f} — умеренный тренд вверх")
        else:
            vote("ADX", -1, f"ADX={adx_val:.0f} — умеренный тренд вниз")

    # === BLOCK I: Williams %R ===
    wr = ind_1h["williams_r"]
    if wr > -20:
        vote("Williams_R", -1, f"Williams %R={wr:.0f} — перекупленность")
    elif wr < -80:
        vote("Williams_R", +1, f"Williams %R={wr:.0f} — перепроданность")

    # === BLOCK J: Ichimoku Cloud ===
    above = ind_1h["ichimoku_above_cloud"]
    below = ind_1h["ichimoku_below_cloud"]
    tenkan = ind_1h["ichimoku_tenkan"]
    kijun = ind_1h["ichimoku_kijun"]
    if above and tenkan > kijun:
        vote("Ichimoku", +3, "Выше облака + Tenkan > Kijun")
    elif below and tenkan < kijun:
        vote("Ichimoku", -3, "Ниже облака + Tenkan < Kijun")
    elif above:
        vote("Ichimoku", +1, "Цена выше облака")
    elif below:
        vote("Ichimoku", -1, "Цена ниже облака")

    # === BLOCK K: Momentum ===
    mom = ind_1h["momentum"]
    if mom > 0.1:
        vote("Momentum", +2, f"Моментум={mom:.2f}% — рост")
    elif mom < -0.1:
        vote("Momentum", -2, f"Моментум={mom:.2f}% — падение")

    # === BLOCK L: VWAP ===
    if ind_1h["close"] > ind_1h["vwap"] * 1.001:
        vote("VWAP", +1, "Цена выше VWAP")
    elif ind_1h["close"] < ind_1h["vwap"] * 0.999:
        vote("VWAP", -1, "Цена ниже VWAP")

    # === BLOCK M: Multi-TF agreement bonus ===
    bull_count = (
        int(ind_4h["close"] > ind_4h["ema50"])
        + int(ind_1h["close"] > ind_1h["ema20"])
        + int(ind_15m["close"] > ind_15m["ema20"])
    )
    if bull_count == 3:
        vote("MTF", +3, "Все таймфреймы бычьи")
    elif bull_count == 0:
        vote("MTF", -3, "Все таймфреймы медвежьи")

    # Calculate confidence from score
    max_possible_score = 30
    norm = abs(score) / max_possible_score
    raw_confidence = _sigmoid(norm * 5.0) * 100
    confidence = int(min(95, max(50, raw_confidence)))

    side = "BUY" if score > 0 else "SELL" if score < 0 else None
    abs_score = abs(score)

    # Strength description
    if abs_score >= 15:
        strength = "Очень сильный"
    elif abs_score >= 10:
        strength = "Сильный"
    elif abs_score >= 6:
        strength = "Умеренный"
    elif abs_score >= 3:
        strength = "Слабый"
    else:
        strength = "Нейтральный"

    return {
        "pair": pair,
        "side": side,
        "score": score,
        "confidence": confidence,
        "strength": strength,
        "details": details,
        "indicators": {
            "RSI": round(ind_1h["rsi14"], 1),
            "MACD": round(ind_1h["macd_hist"], 6),
            "Stochastic": round(ind_1h["stoch_k"], 1),
            "ADX": round(ind_1h["adx"], 1),
            "Williams_R": round(ind_1h["williams_r"], 1),
            "Bollinger": round(ind_1h["bb_pct"], 2),
        },
    }
