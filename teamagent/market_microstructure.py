"""market_microstructure — PRO-уровень: «что происходит ВНУТРИ рынка».

Запрос Jony 2026-05-01 (verbatim, сохранено в Knowledge Note
note-84aa6452502a48a099562667fbe93867):

    «И пусть система будет видет что присходит внутри рынка и пусть это как
    то мне будет показывать визуально и внутри факти и внешний будет выдет
    он хватит теперь работает как средный нужно про уровень а для про нужно
    новый технологии».

Это НЕ ещё один сканер. Это диагностический модуль, который для каждой пары
строит «портрет внутренностей рынка» поверх обычных индикаторов:

    1. Cumulative Delta            — куда уходят деньги (signed volume proxy)
    2. Footprint Grid              — price-buckets × bid/ask volume сетка
    3. Smart Money Concepts:
       - Order Block (OB)          — последний обратный бар перед импульсом
       - Fair Value Gap (FVG)      — ценовой зазор; рынок «закроет» его
       - Liquidity Sweep           — false-breakout / hunt стопов
    4. Wyckoff Stage               — Accumulation / Markup / Distribution / Markdown
    5. Whale Activity              — bars с range ≥ 3×median (institutional impulses)
    6. Hurst Exponent              — >0.5 trend / <0.5 mean-revert / =0.5 random

Поскольку Yahoo не отдаёт реальный bid/ask volume, мы аппроксимируем:
- delta ≈ sign(close − open) × volume   (если close > open → bull volume)
- footprint грид строим на 1m барах за последние 60 мин по ценовым корзинам.

Ничего из этого НЕ открывает сделок самостоятельно — это «PRO-обоснование»
для UI и для будущих стратегий.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Optional

import numpy as np
import pandas as pd

from . import indicators as ind
from .data import yahoo

log = logging.getLogger("microstructure")

# ────────── Параметры (умолчания консервативные) ──────────

CUMDELTA_BARS = 240               # последние 240 1m баров (= 4 часа)
FOOTPRINT_BARS = 60               # последний час
FOOTPRINT_BUCKETS = 12            # 12 ценовых корзин по диапазону
WYCKOFF_LOOKBACK = 200            # 200 1h баров (~8 дней)
WHALE_BAR_RANGE_MULT = 3.0        # range ≥ 3×median → whale
WHALE_LOOKBACK = 200              # 200 1m баров
ORDER_BLOCK_LOOKBACK = 100        # 100 5m баров для поиска OB
FVG_LOOKBACK = 80                 # 80 5m баров
LIQUIDITY_SWEEP_LOOKBACK = 80     # 80 5m баров
HURST_LAGS = (2, 4, 8, 16, 32)    # лаги для Hurst R/S


# ═════════════════════════════════════════════════════════════════
# 1) Cumulative Delta
# ═════════════════════════════════════════════════════════════════

def cumulative_delta(bars_1m: pd.DataFrame) -> dict:
    """Аппроксимация cumulative delta из 1m bars: sign(close-open)*volume."""
    if bars_1m is None or bars_1m.empty:
        return {"available": False, "reason": "no_bars"}
    df = bars_1m.tail(CUMDELTA_BARS).copy()
    if "Volume" not in df.columns:
        return {"available": False, "reason": "no_volume"}

    df["sign"] = np.sign(df["Close"] - df["Open"]).replace(0, 1).astype(int)
    df["delta"] = df["sign"] * df["Volume"].fillna(0)
    df["cum_delta"] = df["delta"].cumsum()

    cur_cum = float(df["cum_delta"].iloc[-1])
    abs_max = float(df["cum_delta"].abs().max() or 1)
    norm_pct = max(-100.0, min(100.0, (cur_cum / abs_max) * 100.0))

    # divergence: цена растёт но delta падает (или наоборот)
    px_change = float(df["Close"].iloc[-1] - df["Close"].iloc[0])
    divergence = (px_change > 0 and cur_cum < 0) or (px_change < 0 and cur_cum > 0)

    if cur_cum > 0:
        bias = "BUY"
    elif cur_cum < 0:
        bias = "SELL"
    else:
        bias = "NEUTRAL"

    # компактный ряд для графика (downsample до 60 точек)
    history = df["cum_delta"].tolist()
    step = max(1, len(history) // 60)
    series = [round(float(v), 2) for v in history[::step]]

    return {
        "available": True,
        "value": round(cur_cum, 2),
        "norm_pct": round(norm_pct, 1),  # -100..+100
        "bias": bias,
        "divergence": divergence,
        "series": series,
        "bars_used": len(df),
    }


# ═════════════════════════════════════════════════════════════════
# 2) Footprint Grid (price-bucket × delta heatmap)
# ═════════════════════════════════════════════════════════════════

def footprint_grid(bars_1m: pd.DataFrame) -> dict:
    """Сетка цена×delta за последний час."""
    if bars_1m is None or bars_1m.empty:
        return {"available": False, "reason": "no_bars"}
    df = bars_1m.tail(FOOTPRINT_BARS).copy()
    if "Volume" not in df.columns or df["Volume"].sum() == 0:
        return {"available": False, "reason": "no_volume"}

    lo = float(df["Low"].min())
    hi = float(df["High"].max())
    if hi <= lo:
        return {"available": False, "reason": "flat_range"}

    edges = np.linspace(lo, hi, FOOTPRINT_BUCKETS + 1)
    bull_vol = np.zeros(FOOTPRINT_BUCKETS)
    bear_vol = np.zeros(FOOTPRINT_BUCKETS)

    for _, row in df.iterrows():
        mid = float((row["High"] + row["Low"]) / 2)
        bucket = int(np.searchsorted(edges, mid) - 1)
        bucket = max(0, min(FOOTPRINT_BUCKETS - 1, bucket))
        v = float(row["Volume"] or 0)
        if row["Close"] >= row["Open"]:
            bull_vol[bucket] += v
        else:
            bear_vol[bucket] += v

    total_bull = float(bull_vol.sum())
    total_bear = float(bear_vol.sum())
    total = total_bull + total_bear
    if total == 0:
        return {"available": False, "reason": "no_activity"}

    # POC = бакет с максимальным total
    totals = bull_vol + bear_vol
    poc_idx = int(np.argmax(totals))
    poc_price = float((edges[poc_idx] + edges[poc_idx + 1]) / 2)

    grid = []
    for i in range(FOOTPRINT_BUCKETS):
        grid.append({
            "price_lo": round(float(edges[i]), 5),
            "price_hi": round(float(edges[i + 1]), 5),
            "bull": round(float(bull_vol[i]), 1),
            "bear": round(float(bear_vol[i]), 1),
            "delta": round(float(bull_vol[i] - bear_vol[i]), 1),
        })

    return {
        "available": True,
        "buckets": FOOTPRINT_BUCKETS,
        "poc_price": round(poc_price, 5),
        "bull_pct": round(total_bull / total * 100.0, 1),
        "bear_pct": round(total_bear / total * 100.0, 1),
        "grid": grid,
    }


# ═════════════════════════════════════════════════════════════════
# 3) Smart Money Concepts
# ═════════════════════════════════════════════════════════════════

def detect_order_blocks(bars_5m: pd.DataFrame, max_blocks: int = 3) -> list[dict]:
    """Order Block = последний bear-бар перед сильным импульсом вверх (bullish OB),
    или последний bull-бар перед сильным импульсом вниз (bearish OB).
    Импульс = последующие 3 бара дают суммарный rally ≥ 1.5×ATR.
    """
    if bars_5m is None or bars_5m.empty or len(bars_5m) < 20:
        return []
    df = bars_5m.tail(ORDER_BLOCK_LOOKBACK).copy().reset_index(drop=True)
    atr_ser = ind.atr(df, 14).ffill().bfill()
    blocks: list[dict] = []
    for i in range(len(df) - 4):
        cur = df.iloc[i]
        a = float(atr_ser.iloc[i] or 0)
        if a <= 0:
            continue
        # Look at next 3 bars for impulse
        future = df.iloc[i + 1:i + 4]
        net_move = float(future["Close"].iloc[-1] - cur["Close"])
        if net_move >= 1.5 * a and cur["Close"] < cur["Open"]:
            # bullish OB (last red bar before up impulse)
            blocks.append({
                "kind": "bullish_ob",
                "high": round(float(cur["High"]), 5),
                "low": round(float(cur["Low"]), 5),
                "ts": str(df.index[i]) if hasattr(df, "index") else str(i),
            })
        elif net_move <= -1.5 * a and cur["Close"] > cur["Open"]:
            blocks.append({
                "kind": "bearish_ob",
                "high": round(float(cur["High"]), 5),
                "low": round(float(cur["Low"]), 5),
                "ts": str(df.index[i]) if hasattr(df, "index") else str(i),
            })
    return blocks[-max_blocks:]


def detect_fvgs(bars_5m: pd.DataFrame, max_gaps: int = 3) -> list[dict]:
    """Fair Value Gap: trio of bars where bar[i-2].high < bar[i].low (bullish FVG)
    or bar[i-2].low > bar[i].high (bearish FVG). The gap zone is a price magnet.
    """
    if bars_5m is None or bars_5m.empty or len(bars_5m) < 5:
        return []
    df = bars_5m.tail(FVG_LOOKBACK).copy().reset_index(drop=True)
    gaps: list[dict] = []
    for i in range(2, len(df)):
        prev_high = float(df.iloc[i - 2]["High"])
        prev_low = float(df.iloc[i - 2]["Low"])
        cur_high = float(df.iloc[i]["High"])
        cur_low = float(df.iloc[i]["Low"])
        if prev_high < cur_low:
            gaps.append({
                "kind": "bullish_fvg",
                "lo": round(prev_high, 5),
                "hi": round(cur_low, 5),
                "size_pips": round((cur_low - prev_high) * 10000, 1),
            })
        elif prev_low > cur_high:
            gaps.append({
                "kind": "bearish_fvg",
                "lo": round(cur_high, 5),
                "hi": round(prev_low, 5),
                "size_pips": round((prev_low - cur_high) * 10000, 1),
            })
    return gaps[-max_gaps:]


def detect_liquidity_sweeps(bars_5m: pd.DataFrame, max_events: int = 3) -> list[dict]:
    """Liquidity Sweep = бар пробивает локальный high/low за N≥10 баров,
    но закрывается ВНУТРЬ диапазона → ловушка / охота за стопами.
    """
    if bars_5m is None or bars_5m.empty or len(bars_5m) < 20:
        return []
    df = bars_5m.tail(LIQUIDITY_SWEEP_LOOKBACK).copy().reset_index(drop=True)
    events: list[dict] = []
    win = 10
    for i in range(win, len(df)):
        cur = df.iloc[i]
        prior = df.iloc[i - win:i]
        prior_hi = float(prior["High"].max())
        prior_lo = float(prior["Low"].min())
        if cur["High"] > prior_hi and cur["Close"] < prior_hi:
            # swept the highs but reversed
            events.append({
                "kind": "sell_side_liquidity_taken",
                "level": round(prior_hi, 5),
                "wick_pips": round((float(cur["High"]) - float(cur["Close"])) * 10000, 1),
                "implication": "expect_down",
            })
        elif cur["Low"] < prior_lo and cur["Close"] > prior_lo:
            events.append({
                "kind": "buy_side_liquidity_taken",
                "level": round(prior_lo, 5),
                "wick_pips": round((float(cur["Close"]) - float(cur["Low"])) * 10000, 1),
                "implication": "expect_up",
            })
    return events[-max_events:]


# ═════════════════════════════════════════════════════════════════
# 4) Wyckoff Stage classifier
# ═════════════════════════════════════════════════════════════════

def wyckoff_stage(bars_1h: pd.DataFrame) -> dict:
    """Простая heuristic:
    - Цена в верхних 20% диапазона за 200 баров и плоская → Distribution.
    - Цена в нижних 20% и плоская → Accumulation.
    - Тренд вверх (последние 50 баров higher highs/lows) → Markup.
    - Тренд вниз → Markdown.
    """
    if bars_1h is None or bars_1h.empty or len(bars_1h) < 60:
        return {"stage": "UNKNOWN", "confidence": 0, "reason": "insufficient_data"}
    df = bars_1h.tail(WYCKOFF_LOOKBACK).copy()
    hi = float(df["High"].max())
    lo = float(df["Low"].min())
    if hi <= lo:
        return {"stage": "UNKNOWN", "confidence": 0, "reason": "flat_range"}
    cur = float(df["Close"].iloc[-1])
    pos = (cur - lo) / (hi - lo)  # 0..1

    # тренд по линейной регрессии последних 50 закрытий
    closes = df["Close"].to_numpy()
    last50 = closes[-50:] if len(closes) >= 50 else closes
    x = np.arange(len(last50))
    slope = float(np.polyfit(x, last50, 1)[0])
    rng_pct = (hi - lo) / cur if cur else 0
    slope_norm = slope / cur if cur else 0  # per-bar % move

    if abs(slope_norm) < 0.0001:
        flat = True
    else:
        flat = False

    if pos >= 0.8 and flat:
        stage = "DISTRIBUTION"; conf = 0.7
    elif pos <= 0.2 and flat:
        stage = "ACCUMULATION"; conf = 0.7
    elif slope_norm > 0.0002:
        stage = "MARKUP"; conf = min(1.0, abs(slope_norm) * 5000)
    elif slope_norm < -0.0002:
        stage = "MARKDOWN"; conf = min(1.0, abs(slope_norm) * 5000)
    else:
        stage = "RANGE"; conf = 0.3

    return {
        "stage": stage,
        "confidence": round(conf * 100, 1),
        "position_in_range_pct": round(pos * 100, 1),
        "trend_slope_per_bar_pct": round(slope_norm * 100, 4),
        "range_pct": round(rng_pct * 100, 2),
    }


# ═════════════════════════════════════════════════════════════════
# 5) Whale Activity
# ═════════════════════════════════════════════════════════════════

def whale_bars(bars_1m: pd.DataFrame, max_events: int = 5) -> list[dict]:
    """1m бары с range ≥ WHALE_BAR_RANGE_MULT × median range за последние WHALE_LOOKBACK."""
    if bars_1m is None or bars_1m.empty or len(bars_1m) < 30:
        return []
    df = bars_1m.tail(WHALE_LOOKBACK).copy()
    df["range"] = df["High"] - df["Low"]
    median_r = float(df["range"].median())
    if median_r <= 0:
        return []
    threshold = WHALE_BAR_RANGE_MULT * median_r
    whales = df[df["range"] >= threshold].copy()
    out: list[dict] = []
    for ts, row in whales.tail(max_events).iterrows():
        out.append({
            "ts": str(ts),
            "range_pips": round(float(row["range"]) * 10000, 1),
            "volume": float(row.get("Volume") or 0),
            "side": "BUY" if row["Close"] >= row["Open"] else "SELL",
        })
    return out


# ═════════════════════════════════════════════════════════════════
# 6) Hurst exponent
# ═════════════════════════════════════════════════════════════════

def hurst_exponent(closes: np.ndarray) -> dict:
    """R/S analysis (упрощённая). H≈0.5 random, H>0.5 persistent (trend),
    H<0.5 anti-persistent (mean-revert)."""
    if closes is None or len(closes) < 64:
        return {"available": False, "reason": "insufficient_data"}
    try:
        x = np.asarray(closes, dtype=float)
        if np.isnan(x).any() or np.std(x) == 0:
            return {"available": False, "reason": "nan_or_flat"}
        log_returns = np.diff(np.log(x))
        if len(log_returns) < 32:
            return {"available": False, "reason": "too_few_returns"}
        rs_pairs = []
        for lag in HURST_LAGS:
            if lag >= len(log_returns):
                break
            chunks = len(log_returns) // lag
            if chunks < 2:
                continue
            rs_vals = []
            for c in range(chunks):
                seg = log_returns[c * lag:(c + 1) * lag]
                mean_seg = seg.mean()
                cum_dev = np.cumsum(seg - mean_seg)
                R = cum_dev.max() - cum_dev.min()
                S = seg.std()
                if S > 0 and R > 0:
                    rs_vals.append(R / S)
            if rs_vals:
                rs_pairs.append((math.log(lag), math.log(np.mean(rs_vals))))
        if len(rs_pairs) < 3:
            return {"available": False, "reason": "too_few_lags"}
        xs, ys = zip(*rs_pairs)
        H = float(np.polyfit(xs, ys, 1)[0])
        H = max(0.0, min(1.0, H))
        if H > 0.55:
            regime = "TRENDING"
        elif H < 0.45:
            regime = "MEAN_REVERTING"
        else:
            regime = "RANDOM"
        return {
            "available": True,
            "H": round(H, 3),
            "regime": regime,
        }
    except Exception as e:
        return {"available": False, "reason": f"error:{type(e).__name__}", "msg": str(e)}


# ═════════════════════════════════════════════════════════════════
# Public API: analyze(pair) → all microstructure facts
# ═════════════════════════════════════════════════════════════════

@dataclass
class MicrostructurePayload:
    pair: str
    cumulative_delta: dict
    footprint: dict
    order_blocks: list
    fair_value_gaps: list
    liquidity_sweeps: list
    wyckoff: dict
    whales: list
    hurst: dict
    summary: dict

    def to_dict(self) -> dict:
        return {
            "pair": self.pair,
            "cumulative_delta": self.cumulative_delta,
            "footprint": self.footprint,
            "order_blocks": self.order_blocks,
            "fair_value_gaps": self.fair_value_gaps,
            "liquidity_sweeps": self.liquidity_sweeps,
            "wyckoff": self.wyckoff,
            "whales": self.whales,
            "hurst": self.hurst,
            "summary": self.summary,
        }


def _summarize(pair: str, parts: dict) -> dict:
    """«Внутренний факт» + «Внешний вид» — две короткие строки для карточки."""
    inner_lines: list[str] = []
    outer_lines: list[str] = []

    cd = parts.get("cumulative_delta") or {}
    if cd.get("available"):
        bias = cd.get("bias")
        norm = cd.get("norm_pct", 0)
        inner_lines.append(f"Cumulative Delta {('+' if norm >= 0 else '')}{norm:.0f}% → {bias}")
        if cd.get("divergence"):
            inner_lines.append("⚠️ delta divergence with price")

    wy = parts.get("wyckoff") or {}
    if wy.get("stage") and wy["stage"] != "UNKNOWN":
        outer_lines.append(f"Wyckoff: {wy['stage']} @ {wy.get('position_in_range_pct', 0):.0f}%")

    obs = parts.get("order_blocks") or []
    if obs:
        last = obs[-1]
        inner_lines.append(f"Last OB: {last['kind']} @ {last['low']}-{last['high']}")

    fvgs = parts.get("fair_value_gaps") or []
    if fvgs:
        last = fvgs[-1]
        outer_lines.append(f"FVG {last['kind']} @ {last['lo']}-{last['hi']} ({last['size_pips']} pips)")

    sweeps = parts.get("liquidity_sweeps") or []
    if sweeps:
        last = sweeps[-1]
        inner_lines.append(f"Sweep: {last['kind']} → {last['implication']}")

    whales = parts.get("whales") or []
    if whales:
        last = whales[-1]
        inner_lines.append(f"Whale: {last['range_pips']} pip {last['side']} bar")

    h = parts.get("hurst") or {}
    if h.get("available"):
        outer_lines.append(f"Hurst H={h['H']} ({h['regime']})")

    fp = parts.get("footprint") or {}
    if fp.get("available"):
        outer_lines.append(f"Footprint POC {fp['poc_price']} bull {fp['bull_pct']}%")

    return {
        "inner_facts": inner_lines,
        "outer_view": outer_lines,
    }


def analyze(pair: str) -> Optional[dict]:
    """Возвращает полный microstructure-payload по паре.
    Безопасно: если данные недоступны — соответствующая секция помечена available=False.
    """
    try:
        bars_1m = yahoo.latest_bars(pair, "1m", 300)
    except Exception as e:
        log.warning(f"{pair}: 1m bars fetch failed: {e}")
        bars_1m = None
    try:
        bars_5m = yahoo.latest_bars(pair, "5m", 200)
    except Exception as e:
        log.warning(f"{pair}: 5m bars fetch failed: {e}")
        bars_5m = None
    try:
        bars_1h = yahoo.latest_bars(pair, "1h", 300)
    except Exception as e:
        log.warning(f"{pair}: 1h bars fetch failed: {e}")
        bars_1h = None

    cd = cumulative_delta(bars_1m) if bars_1m is not None else {"available": False}
    fp = footprint_grid(bars_1m) if bars_1m is not None else {"available": False}
    obs = detect_order_blocks(bars_5m) if bars_5m is not None else []
    fvgs = detect_fvgs(bars_5m) if bars_5m is not None else []
    sweeps = detect_liquidity_sweeps(bars_5m) if bars_5m is not None else []
    wy = wyckoff_stage(bars_1h) if bars_1h is not None else {"stage": "UNKNOWN", "confidence": 0}
    whales = whale_bars(bars_1m) if bars_1m is not None else []

    closes = bars_1h["Close"].to_numpy() if bars_1h is not None and not bars_1h.empty else None
    h = hurst_exponent(closes) if closes is not None else {"available": False}

    parts = {
        "cumulative_delta": cd,
        "footprint": fp,
        "order_blocks": obs,
        "fair_value_gaps": fvgs,
        "liquidity_sweeps": sweeps,
        "wyckoff": wy,
        "whales": whales,
        "hurst": h,
    }
    summary = _summarize(pair, parts)

    return MicrostructurePayload(
        pair=pair,
        cumulative_delta=cd,
        footprint=fp,
        order_blocks=obs,
        fair_value_gaps=fvgs,
        liquidity_sweeps=sweeps,
        wyckoff=wy,
        whales=whales,
        hurst=h,
        summary=summary,
    ).to_dict()
