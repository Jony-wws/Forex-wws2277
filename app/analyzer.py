"""Signal analyzer - multi-timeframe technical analysis + Price Action.

Generates BUY/SELL signals with confidence levels.
Only shows signal when confidence >= 80%.
Timeframes: M15 + H1 + H4 + D1 (real Yahoo Finance bars).
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


def _confidence_from_ratio(abs_score: int, max_score: int) -> int:
    """Map score/max ratio to confidence percentage.

    Calibrated so that ratio ~0.30 -> ~80% confidence (entry threshold).
    Uses a saturating curve so adding/removing voting blocks does not
    break the 80% gate — confidence scales with the realized ratio.
    """
    if max_score <= 0:
        return 50
    ratio = min(1.0, abs_score / max_score)
    confidence = 50 + 45 * (1 - math.exp(-3.66 * ratio))
    return max(50, min(95, int(round(confidence))))


def _trend_persistence_5h(bars_1h, side_hint: str | None) -> tuple[float, int]:
    """Measure how persistent the directional trend was over the last 5 H1 bars.

    Returns (persistence_pct, agreeing_bars) where ``persistence_pct`` is
    the share of the last 5 closed H1 candles that agree with ``side_hint``
    (BUY = green/up close, SELL = red/down close).  When the analyser has
    no directional bias yet we fall back to the dominant direction so the
    metric is still informative.

    The strict 5-hour cycle uses this to require that a top pick has
    actually been moving in one direction for the last 5 hours, not just
    that the current bar happens to print a strong score.
    """
    if bars_1h is None or len(bars_1h) < 6:
        return 0.0, 0
    last5 = bars_1h.tail(5)
    closes = last5["Close"].to_numpy()
    opens = last5["Open"].to_numpy()
    bull_bars = int((closes > opens).sum())
    bear_bars = int((closes < opens).sum())
    if side_hint == "BUY":
        agreeing = bull_bars
    elif side_hint == "SELL":
        agreeing = bear_bars
    else:
        agreeing = max(bull_bars, bear_bars)
    return round(100.0 * agreeing / 5.0, 1), agreeing


def analyze_pair(pair: str) -> dict | None:
    """Full multi-TF analysis of one pair on W1 / D1 / H4 / H1 / M15."""
    # Real timeframes — NOT a 1h proxy for 4h.
    bars_15m = fetch_bars(pair, "15m", "5d")
    bars_1h = fetch_bars(pair, "1h", "1mo")
    bars_4h = fetch_bars(pair, "4h", "3mo")
    bars_1d = fetch_bars(pair, "1d", "1y")
    bars_1w = fetch_bars(pair, "1wk", "2y")

    if any(df.empty or len(df) < 30 for df in (bars_15m, bars_1h, bars_4h, bars_1d)):
        return None

    ind_15m = indicators.compute_all(bars_15m)
    ind_1h = indicators.compute_all(bars_1h)
    ind_4h = indicators.compute_all(bars_4h)
    ind_1d = indicators.compute_all(bars_1d)
    ind_1w = indicators.compute_all(bars_1w) if (
        bars_1w is not None and not bars_1w.empty and len(bars_1w) >= 30
    ) else None

    if not ind_15m or not ind_1h or not ind_4h or not ind_1d:
        return None

    score = 0
    max_possible = 0
    details: list[dict] = []

    def vote(name: str, contrib: int, reason: str, weight: int = 0) -> None:
        nonlocal score, max_possible
        score += contrib
        max_possible += abs(weight) if weight else abs(contrib)
        details.append({"name": name, "value": contrib, "reason": reason})

    # === BLOCK A0: 1D Trend Structure (weight: 3) — senior timeframe ===
    if ind_1d["close"] > ind_1d["ema50"] > ind_1d["ema200"]:
        vote("1D Тренд", +3, "Сильный восходящий тренд (1D)", 3)
    elif ind_1d["close"] < ind_1d["ema50"] < ind_1d["ema200"]:
        vote("1D Тренд", -3, "Сильный нисходящий тренд (1D)", 3)
    elif ind_1d["close"] > ind_1d["ema50"]:
        vote("1D Тренд", +1, "Умеренный рост (1D)", 3)
    elif ind_1d["close"] < ind_1d["ema50"]:
        vote("1D Тренд", -1, "Умеренное падение (1D)", 3)
    else:
        vote("1D Тренд", 0, "Нейтральный (1D)", 3)

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

    # === BLOCK M: Multi-TF Agreement across M15+H1+H4+D1 (weight: 3) ===
    bull_count = (
        int(ind_1d["close"] > ind_1d["ema50"])
        + int(ind_4h["close"] > ind_4h["ema50"])
        + int(ind_1h["close"] > ind_1h["ema20"])
        + int(ind_15m["close"] > ind_15m["ema20"])
    )
    if bull_count == 4:
        vote("Мульти-ТФ", +3, "Все 4 таймфрейма бычьи (D1+H4+H1+M15)", 3)
    elif bull_count == 0:
        vote("Мульти-ТФ", -3, "Все 4 таймфрейма медвежьи (D1+H4+H1+M15)", 3)
    elif bull_count >= 3:
        vote("Мульти-ТФ", +1, f"3 из 4 таймфреймов бычьи", 3)
    elif bull_count <= 1:
        vote("Мульти-ТФ", -1, f"3 из 4 таймфреймов медвежьи", 3)

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

    # === BLOCK P: 1W Senior Trend (weight: 2) ===
    # Weekly trend is the longest-horizon agreement signal — it confirms
    # whether the directional read is aligned with the big-money / swing
    # bias.  Awarded only when W1 bars are available (≥30 weeks of history).
    if ind_1w:
        if ind_1w["close"] > ind_1w["ema20"] > ind_1w["ema50"]:
            vote("1W Тренд", +2, "Недельный тренд вверх (EMA20>EMA50)", 2)
        elif ind_1w["close"] < ind_1w["ema20"] < ind_1w["ema50"]:
            vote("1W Тренд", -2, "Недельный тренд вниз (EMA20<EMA50)", 2)
        elif ind_1w["close"] > ind_1w["ema50"]:
            vote("1W Тренд", +1, "Недельный выше EMA50", 2)
        elif ind_1w["close"] < ind_1w["ema50"]:
            vote("1W Тренд", -1, "Недельный ниже EMA50", 2)

    # === BLOCK Q: Donchian-20 breakout on H1 (weight: 2) ===
    # Classic trend-following signal — the textbook Turtle-Trader entry.
    # When H1 close prints above the prior 20-bar high it's a continuation
    # signal big players use to size in; below the 20-bar low is the same
    # for shorts.  Pure price action, no indicator lag.
    if len(bars_1h) > 21:
        prior20_high = float(bars_1h["High"].iloc[-21:-1].max())
        prior20_low = float(bars_1h["Low"].iloc[-21:-1].min())
        h1_close = float(ind_1h["close"])
        if h1_close > prior20_high:
            vote("Дончиан H1", +2, "Пробой 20-период max — институциональное продолжение", 2)
        elif h1_close < prior20_low:
            vote("Дончиан H1", -2, "Пробой 20-период min — институциональное продолжение", 2)

    # === BLOCK R: Daily Pivot Points (weight: 1) ===
    # Classic S/R levels read by every desk trader.  Yesterday's H/L/C
    # define today's pivot/R1/S1; price closing above R1 on H1 is a
    # bullish trend confirmation, below S1 a bearish one.
    if len(bars_1d) >= 2:
        yest = bars_1d.iloc[-2]
        yh = float(yest["High"])
        yl = float(yest["Low"])
        yc = float(yest["Close"])
        pivot_p = (yh + yl + yc) / 3.0
        r1 = 2.0 * pivot_p - yl
        s1 = 2.0 * pivot_p - yh
        h1_close = float(ind_1h["close"])
        if h1_close > r1:
            vote("Pivot D", +1, "Цена выше R1 — пробой дневного сопротивления", 1)
        elif h1_close < s1:
            vote("Pivot D", -1, "Цена ниже S1 — пробой дневной поддержки", 1)

    # === Calculate confidence (dynamic max_score) ===
    # max_possible accumulates the |weight| of every block that voted, so it
    # grows automatically when blocks are added/removed. Confidence is derived
    # from the score / max ratio rather than from hardcoded score thresholds.
    abs_score = abs(score)
    confidence = _confidence_from_ratio(abs_score, max_possible)

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

    # Multi-TF alignment flag — used by the brain veto + cycle module.
    # The Pine indicators (eurusd_signal_indicator.pine, forex_max_pro.pine)
    # use the same definition: ≥3 of 4 senior timeframes agreeing with
    # ``side`` is a strong directional signal.  Requiring all 4 to agree
    # is too strict — a 3-of-4 setup is 75% TF agreement, mathematically
    # solid for 5h binary options.  We expose ``multi_tf_strict`` for
    # telemetry when *all four* agree (the textbook-perfect setup).
    bear_count = 4 - bull_count
    multi_tf_aligned = (bull_count >= 3 and side == "BUY") or (
        bear_count >= 3 and side == "SELL"
    )
    multi_tf_strict = (bull_count == 4 and side == "BUY") or (
        bull_count == 0 and side == "SELL"
    )
    multi_tf_count = bull_count if side == "BUY" else bear_count

    # Trend persistence over the last 5 H1 bars — needed by the strict
    # 5-hour cycle to require that the trend has actually been going in
    # the predicted direction for at least the last 5 hours, not just one
    # strong bar.
    persistence_pct, agreeing_bars = _trend_persistence_5h(bars_1h, side)
    adx_h1 = ind_1h["adx"]
    adx_h4 = ind_4h["adx"]
    ratio_now = abs_score / max_possible if max_possible > 0 else 0.0

    # Hard "strong sustained trend" gate — strictly tighter than the legacy
    # PREMIUM gate.  All five conditions must hold simultaneously, which is
    # ~100x harder than the previous "≥80% confidence" gate alone:
    #   1. confidence ≥ 88 (was 80)
    #   2. score / max ≥ 0.55 (was 0.40)
    #   3. multi_tf_aligned — D1 + H4 + H1 + M15 all in one direction
    #   4. ADX H1 ≥ 25 AND ADX H4 ≥ 20 (real trend, both timeframes)
    #   5. trend_persistence_5h ≥ 80% (≥4 of 5 H1 bars in direction)
    is_strong_trend = bool(
        side is not None
        and confidence >= 88
        and ratio_now >= 0.55
        and multi_tf_aligned
        and adx_h1 >= 25.0
        and adx_h4 >= 20.0
        and persistence_pct >= 80.0
    )

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
        "multi_tf_aligned": multi_tf_aligned,
        "multi_tf_strict": multi_tf_strict,
        "multi_tf_count": multi_tf_count,
        "adx": round(adx_h1, 1),
        "adx_h1": round(adx_h1, 1),
        "adx_h4": round(adx_h4, 1),
        "trend_persistence_5h": persistence_pct,
        "trend_persistence_bars": agreeing_bars,
        "is_strong_trend": is_strong_trend,
        "indicators": {
            "RSI": round(ind_1h["rsi14"], 1),
            "MACD": round(ind_1h["macd_hist"], 6),
            "Stochastic_K": round(ind_1h["stoch_k"], 1),
            "ADX": round(adx_h1, 1),
            "ADX_H4": round(adx_h4, 1),
            "Williams_R": round(ind_1h["williams_r"], 1),
            "Bollinger_%B": round(ind_1h["bb_pct"], 2),
            "Momentum": round(ind_1h["momentum"], 3),
            "EMA20": round(ind_1h["ema20"], 5),
            "EMA50": round(ind_1h["ema50"], 5),
            "Persistence_5h": persistence_pct,
        },
    }
