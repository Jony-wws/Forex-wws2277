"""Volume Profile (Стакан) с прогнозом до 00:00 UTC+5.

Что отдаём дашборду:
- бакеты «уровень → объём (или time-at-price)» (от 50)
- POC (Point of Control) — самый крупный уровень
- VAH/VAL (70% объёма)
- крупные игроки (≥80-perc) — это «киты» institutional levels
- forecast_to_midnight: куда цена не вернётся до 00:00 UTC+5
- direction: BUY/SELL (откуда уходит и не возвратится)
"""
from __future__ import annotations
from datetime import datetime, timezone, timedelta
from typing import Any

import numpy as np
import pandas as pd

from . import config
from .data import yahoo


def _utc5_midnight(now: datetime | None = None) -> datetime:
    """00:00 по UTC+5 — это 19:00 предыдущего UTC-дня. Возвращаем ближайшее
    ПРЕДСТОЯЩЕЕ 00:00 UTC+5.
    """
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    cutoff_utc_hour = (24 - config.UTC_OFFSET_HOURS) % 24  # = 19
    target = now.replace(hour=cutoff_utc_hour, minute=0, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return target


def build(pair: str, df: pd.DataFrame | None = None, buckets: int | None = None) -> dict[str, Any]:
    """Построить полный snapshot Volume Profile + прогноз для пары."""
    buckets = buckets or config.VP_BUCKETS
    if df is None or df.empty:
        df = yahoo.latest_bars(pair, "1m", config.VP_BARS)
    if df is None or df.empty:
        return {"error": "no_data", "pair": pair}

    high, low = float(df["High"].max()), float(df["Low"].min())
    if high <= low:
        return {"error": "flat_range", "pair": pair}

    edges = np.linspace(low, high, buckets + 1)
    typical = ((df["High"] + df["Low"] + df["Close"]) / 3.0).to_numpy()
    vol = df["Volume"].to_numpy() if "Volume" in df.columns else np.ones(len(df))
    if vol.sum() <= 0:
        vol = np.ones_like(vol)   # time-at-price вместо real volume

    hist, _ = np.histogram(typical, bins=edges, weights=vol)

    centers = (edges[:-1] + edges[1:]) / 2.0
    total = float(hist.sum())
    if total <= 0:
        return {"error": "no_volume", "pair": pair}

    pct = (hist / total * 100.0).tolist()
    levels = [
        {"price": float(centers[i]), "weight_pct": pct[i]} for i in range(buckets)
    ]

    poc_idx = int(np.argmax(hist))
    poc_price = float(centers[poc_idx])

    # value area (70% объёма вокруг POC)
    sorted_idx = np.argsort(hist)[::-1]
    cum, va_set = 0.0, set()
    for i in sorted_idx:
        cum += hist[i]
        va_set.add(int(i))
        if cum / total >= 0.70:
            break
    va_indices = sorted(va_set)
    val_price = float(centers[va_indices[0]])
    vah_price = float(centers[va_indices[-1]])

    # крупные игроки = ≥80-perc по объёму
    threshold = float(np.percentile(hist, config.VP_BIG_PLAYER_PCTL))
    big_players = []
    for i in range(buckets):
        if hist[i] >= threshold and hist[i] > 0:
            kind = "support" if centers[i] <= poc_price else "resistance"
            big_players.append({
                "price": float(centers[i]),
                "weight_pct": pct[i],
                "kind": kind,
            })

    # текущая цена и направление импульса
    current_price = float(df["Close"].iloc[-1])
    last_hour = df.tail(60) if len(df) > 60 else df
    direction_up = float(last_hour["Close"].iloc[-1]) > float(last_hour["Close"].iloc[0])

    # forecast: «куда цена НЕ вернётся до 00:00 UTC+5»
    # Логика: если идём вверх — нижние уровни big_players с большим расстоянием
    # становятся «не вернётся вниз сюда»; если вниз — наоборот.
    no_return_levels = []
    for bp in big_players:
        if direction_up and bp["price"] < current_price:
            no_return_levels.append({**bp, "side": "below"})
        elif not direction_up and bp["price"] > current_price:
            no_return_levels.append({**bp, "side": "above"})
    # топ-3 самых далёких
    no_return_levels.sort(key=lambda x: abs(x["price"] - current_price), reverse=True)
    no_return_levels = no_return_levels[:3]

    return {
        "pair": pair,
        "as_of": datetime.now(timezone.utc).isoformat(),
        "current_price": current_price,
        "high": high,
        "low": low,
        "poc": poc_price,
        "vah": vah_price,
        "val": val_price,
        "buckets": levels,
        "big_players": big_players,
        "direction": "UP" if direction_up else "DOWN",
        "forecast_to_utc5_midnight": {
            "deadline_utc": _utc5_midnight().isoformat(),
            "no_return_levels": no_return_levels,
            "explanation": (
                "Цена не вернётся к нижним big-player уровням до 00:00 UTC+5"
                if direction_up
                else "Цена не вернётся к верхним big-player уровням до 00:00 UTC+5"
            ),
        },
    }
