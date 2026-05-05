"""Signal analyzer - multi-timeframe technical analysis + Price Action.

Generates BUY/SELL signals with confidence levels.
Only shows signal when confidence >= 80%.
Uses: RSI, MACD, EMA, Bollinger, Stochastic, ADX, Williams %R,
Ichimoku, Momentum, VWAP, ATR, Volume analysis, Price Action patterns.
"""
from __future__ import annotations

import logging
import math

from . import indicators
from .prices import fetch_bars
from .price_action import price_action_score

log = logging.getLogger("analyzer")


def _sigmoid(x: float) -> float:
    return 1.0 / (1.0 + math.exp(-x))


def analyze_pair(pair: str) -> dict | None:
    """Full multi-TF analysis of one pair."""
    bars_4h = fetch_bars(pair, "1h", "1mo")
    bars_1h = fetch_bars(pair, "1h", "5d")
    bars_15m = fetch_bars(pair, "15m", "5d")

    if any(df.empty or len(df) < 30 for df in (bars_4h, bars_1h, bars_15m)):
        return None

    ind_4h = indicators.compute_all(bars_4h)
    ind_1h = indicators.compute_all(bars_1h)
    ind_15m = indicators.compute_all(bars_15m)

    if not ind_4h or not ind_1h or not ind_15m:
        return None

    score = 0
    max_possible = 0
    details: list[dict] = []

    def vote(name: str, contrib: int, reason: str, weight: int = 0) -> None:
        nonlocal score, max_possible
        score += contrib
        max_possible += abs(weight) if weight else abs(contrib)
        details.append({"name": name, "value": contrib, "reason": reason})

    # === BLOCK A: 4H Trend Structure (weight: 3) ===
    if ind_4h["close"] > ind_4h["ema50"] > ind_4h["ema200"]:
        vote("4H Тренд", +3, "Сильный восходящий тренд (4H)", 3)
    elif ind_4h["close"] < ind_4h["ema50"] < ind_4h["ema200"]:
        vote("4H Тренд", -3, "Сильный нисходящий тренд (4H)", 3)
    elif ind_4h["close"] > ind_4h["ema50"]:
        vote("4H Тренд", +1, "Умеренный рост (4H)", 3)
    elif ind_4h["close"] < ind_4h["ema50"]:
        vote("4H Тренд", -1, "Умеренное падение (4H)", 3)
    else:
        vote("4H Тренд", 0, "Нейтральный (4H)", 3)

    # === BLOCK B: 1H Confirmation (weight: 2) ===
    if ind_1h["close"] > ind_1h["ema20"] > ind_1h["ema50"]:
        vote("1H Тренд", +2, "Подтверждён рост (1H)", 2)
    elif ind_1h["close"] < ind_1h["ema20"] < ind_1h["ema50"]:
        vote("1H Тренд", -2, "Подтверждено падение (1H)", 2)
    else:
        vote("1H Тренд", 0, "Без подтверждения (1H)", 2)

    # === BLOCK C: 15M Entry (weight: 1) ===
    if ind_15m["close"] > ind_15m["ema20"]:
        vote("15М Вход", +1, "Цена выше EMA20 (15М)", 1)
    else:
        vote("15М Вход", -1, "Цена ниже EMA20 (15М)", 1)

    # === BLOCK D: RSI (weight: 3) ===
    rsi_val = ind_1h["rsi14"]
    if 55 < rsi_val < 70:
        vote("RSI", +2, f"RSI {rsi_val:.0f} — бычья зона", 3)
    elif 30 < rsi_val < 45:
        vote("RSI", -2, f"RSI {rsi_val:.0f} — медвежья зона", 3)
    elif rsi_val >= 70:
        vote("RSI", -3, f"RSI {rsi_val:.0f} — перекупленность, ожидается откат", 3)
    elif rsi_val <= 30:
        vote("RSI", +3, f"RSI {rsi_val:.0f} — перепроданность, ожидается отскок", 3)
    else:
        vote("RSI", 0, f"RSI {rsi_val:.0f} — нейтральная зона", 3)

    # === BLOCK E: MACD (weight: 3) ===
    macd_h = ind_1h["macd_hist"]
    macd_prev = ind_1h["macd_prev_hist"]
    if macd_h > 0 and macd_prev <= 0:
        vote("MACD", +3, "Пересечение MACD вверх — сильный бычий сигнал", 3)
    elif macd_h < 0 and macd_prev >= 0:
        vote("MACD", -3, "Пересечение MACD вниз — сильный медвежий сигнал", 3)
    elif macd_h > 0 and macd_h > macd_prev:
        vote("MACD", +2, "MACD гистограмма растёт", 3)
    elif macd_h < 0 and macd_h < macd_prev:
        vote("MACD", -2, "MACD гистограмма падает", 3)
    elif macd_h > 0:
        vote("MACD", +1, "MACD положительный", 3)
    elif macd_h < 0:
        vote("MACD", -1, "MACD отрицательный", 3)

    # === BLOCK F: Bollinger Bands (weight: 2) ===
    bb = ind_1h["bb_pct"]
    if bb > 0.95:
        vote("Боллинджер", -2, "Цена у верхней границы — перекупленность", 2)
    elif bb < 0.05:
        vote("Боллинджер", +2, "Цена у нижней границы — перепроданность", 2)
    elif bb > 0.65:
        vote("Боллинджер", +1, "Цена выше средней полосы", 2)
    elif bb < 0.35:
        vote("Боллинджер", -1, "Цена ниже средней полосы", 2)
    else:
        vote("Боллинджер", 0, "Цена в середине канала", 2)

    # === BLOCK G: Stochastic (weight: 2) ===
    sk, sd = ind_1h["stoch_k"], ind_1h["stoch_d"]
    if sk < 20 and sd < 20:
        vote("Стохастик", +2, f"K={sk:.0f} D={sd:.0f} — перепроданность", 2)
    elif sk > 80 and sd > 80:
        vote("Стохастик", -2, f"K={sk:.0f} D={sd:.0f} — перекупленность", 2)
    elif sk > sd and sk < 80:
        vote("Стохастик", +1, f"K={sk:.0f} > D={sd:.0f} — бычий", 2)
    elif sk < sd and sk > 20:
        vote("Стохастик", -1, f"K={sk:.0f} < D={sd:.0f} — медвежий", 2)
    else:
        vote("Стохастик", 0, f"K={sk:.0f} D={sd:.0f} — нейтральный", 2)

    # === BLOCK H: ADX Trend Strength (weight: 3) ===
    adx_val = ind_1h["adx"]
    if adx_val < 15:
        penalty = int(round(abs(score) * 0.5))
        if penalty > 0:
            direction = -1 if score > 0 else 1
            vote("ADX", direction * penalty,
                 f"ADX {adx_val:.0f} — слабый тренд, штраф сигнала", 3)
    elif adx_val > 30:
        if ind_1h["plus_di"] > ind_1h["minus_di"]:
            vote("ADX", +3, f"ADX {adx_val:.0f} — сильный восходящий тренд", 3)
        else:
            vote("ADX", -3, f"ADX {adx_val:.0f} — сильный нисходящий тренд", 3)
    elif adx_val > 20:
        if ind_1h["plus_di"] > ind_1h["minus_di"]:
            vote("ADX", +1, f"ADX {adx_val:.0f} — умеренный тренд вверх", 3)
        else:
            vote("ADX", -1, f"ADX {adx_val:.0f} — умеренный тренд вниз", 3)

    # === BLOCK I: Williams %R (weight: 1) ===
    wr = ind_1h["williams_r"]
    if wr > -20:
        vote("Williams %R", -1, f"W%R {wr:.0f} — перекупленность", 1)
    elif wr < -80:
        vote("Williams %R", +1, f"W%R {wr:.0f} — перепроданность", 1)

    # === BLOCK J: Ichimoku Cloud (weight: 3) ===
    above = ind_1h["ichimoku_above_cloud"]
    below = ind_1h["ichimoku_below_cloud"]
    tenkan = ind_1h["ichimoku_tenkan"]
    kijun = ind_1h["ichimoku_kijun"]
    if above and tenkan > kijun:
        vote("Ишимоку", +3, "Выше облака + Tenkan > Kijun — сильный бычий", 3)
    elif below and tenkan < kijun:
        vote("Ишимоку", -3, "Ниже облака + Tenkan < Kijun — сильный медвежий", 3)
    elif above:
        vote("Ишимоку", +1, "Цена выше облака Ишимоку", 3)
    elif below:
        vote("Ишимоку", -1, "Цена ниже облака Ишимоку", 3)
    else:
        vote("Ишимоку", 0, "Цена внутри облака — неопределённость", 3)

    # === BLOCK K: Momentum (weight: 2) ===
    mom = ind_1h["momentum"]
    if mom > 0.15:
        vote("Моментум", +2, f"Моментум +{mom:.2f}% — сильный рост", 2)
    elif mom < -0.15:
        vote("Моментум", -2, f"Моментум {mom:.2f}% — сильное падение", 2)
    elif mom > 0.05:
        vote("Моментум", +1, f"Моментум +{mom:.2f}% — рост", 2)
    elif mom < -0.05:
        vote("Моментум", -1, f"Моментум {mom:.2f}% — падение", 2)

    # === BLOCK L: VWAP (weight: 1) ===
    if ind_1h["close"] > ind_1h["vwap"] * 1.001:
        vote("VWAP", +1, "Цена выше VWAP — бычий настрой", 1)
    elif ind_1h["close"] < ind_1h["vwap"] * 0.999:
        vote("VWAP", -1, "Цена ниже VWAP — медвежий настрой", 1)

    # === BLOCK M: Multi-TF Agreement (weight: 3) ===
    bull_count = (
        int(ind_4h["close"] > ind_4h["ema50"])
        + int(ind_1h["close"] > ind_1h["ema20"])
        + int(ind_15m["close"] > ind_15m["ema20"])
    )
    if bull_count == 3:
        vote("Мульти-ТФ", +3, "Все 3 таймфрейма бычьи — сильное подтверждение", 3)
    elif bull_count == 0:
        vote("Мульти-ТФ", -3, "Все 3 таймфрейма медвежьи — сильное подтверждение", 3)

    # === BLOCK N: Price Action (weight: 3) ===
    pa_score, pa_reasons = price_action_score(bars_1h)
    if pa_score != 0:
        capped = max(-3, min(3, pa_score))
        reason = "; ".join(pa_reasons[:3]) if pa_reasons else "Price Action"
        vote("Price Action", capped, reason, 3)

    # === BLOCK O: EMA Cross on 15m (early entry signal) ===
    if ind_15m["ema20"] > ind_15m["ema50"]:
        vote("EMA Кросс 15М", +1, "EMA20 > EMA50 на 15М — бычий кросс", 1)
    elif ind_15m["ema20"] < ind_15m["ema50"]:
        vote("EMA Кросс 15М", -1, "EMA20 < EMA50 на 15М — медвежий кросс", 1)

    # === Calculate confidence ===
    abs_score = abs(score)
    # Realistic max score when most indicators agree strongly is ~25-30
    # Score 8+ means strong agreement, 15+ means very strong
    # Map: 0->50%, 5->65%, 8->75%, 10->80%, 15->87%, 20->92%, 25->95%
    if abs_score >= 20:
        confidence = 92
    elif abs_score >= 15:
        confidence = 85 + int((abs_score - 15) * 1.4)
    elif abs_score >= 10:
        confidence = 80 + int((abs_score - 10) * 1.0)
    elif abs_score >= 8:
        confidence = 75 + int((abs_score - 8) * 2.5)
    elif abs_score >= 5:
        confidence = 65 + int((abs_score - 5) * 3.3)
    elif abs_score >= 3:
        confidence = 58 + int((abs_score - 3) * 3.5)
    else:
        confidence = 50 + int(abs_score * 2.7)
    confidence = max(50, min(95, confidence))

    side = "BUY" if score > 0 else "SELL" if score < 0 else None

    # Strength
    if abs_score >= 18:
        strength = "Очень сильный"
    elif abs_score >= 12:
        strength = "Сильный"
    elif abs_score >= 7:
        strength = "Умеренный"
    elif abs_score >= 3:
        strength = "Слабый"
    else:
        strength = "Нейтральный"

    # 5h forecast direction and strength
    forecast_5h = None
    forecast_24h = None
    if side and confidence >= 80:
        forecast_5h = {
            "direction": "Рост" if side == "BUY" else "Снижение",
            "strength": strength,
            "confidence": confidence,
        }
        if abs_score >= 12:
            f24_str = "Сильное движение"
        elif abs_score >= 7:
            f24_str = "Умеренное движение"
        else:
            f24_str = "Слабое движение"
        forecast_24h = {
            "direction": "Рост" if side == "BUY" else "Снижение",
            "strength": f24_str,
        }

    return {
        "pair": pair,
        "side": side,
        "score": score,
        "max_score": max_possible,
        "confidence": confidence,
        "strength": strength,
        "details": details,
        "forecast_5h": forecast_5h,
        "forecast_24h": forecast_24h,
        "indicators": {
            "RSI": round(ind_1h["rsi14"], 1),
            "MACD": round(ind_1h["macd_hist"], 6),
            "Stochastic_K": round(ind_1h["stoch_k"], 1),
            "ADX": round(ind_1h["adx"], 1),
            "Williams_R": round(ind_1h["williams_r"], 1),
            "Bollinger_%B": round(ind_1h["bb_pct"], 2),
            "Momentum": round(ind_1h["momentum"], 3),
            "EMA20": round(ind_1h["ema20"], 5),
            "EMA50": round(ind_1h["ema50"], 5),
        },
    }
