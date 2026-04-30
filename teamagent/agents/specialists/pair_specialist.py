"""Per-pair specialist — каждый занимается одной парой.

Каждые 60 секунд:
- скачивает свежие 1m/15m/1h
- считает индикаторы
- хранит свою «специализированную» оценку для пары (для UI / forecast_scanner)
"""
from __future__ import annotations
from datetime import datetime, timezone

from ... import indicators
from ...data import yahoo
from ..base import Agent


class PairSpecialist(Agent):
    category = "specialist"

    def __init__(self, pair: str) -> None:
        self.pair = pair
        self.name = f"specialist_{pair}"
        self.interval_sec = 60
        super().__init__()

    def tick(self) -> dict:
        df_1h = yahoo.latest_bars(self.pair, "1h", 100)
        df_15m = yahoo.latest_bars(self.pair, "15m", 100)
        if df_1h.empty or df_15m.empty:
            return {"pair": self.pair, "status": "no_data"}

        ind_1h = indicators.all_indicators(df_1h)
        ind_15m = indicators.all_indicators(df_15m)
        if not ind_1h or not ind_15m:
            return {"pair": self.pair, "status": "insufficient_bars"}

        bias = "BULL" if ind_1h["close"] > ind_1h["ema50"] else "BEAR"
        confidence = abs(ind_1h["mom5"]) * 50 + abs(ind_1h["bb_pct"] - 0.5) * 100
        confidence = min(100.0, confidence)

        return {
            "pair": self.pair,
            "bias": bias,
            "confidence": round(confidence, 2),
            "rsi_1h": round(ind_1h["rsi14"], 2),
            "atr_1h": ind_1h["atr14"],
            "bb_pct_1h": round(ind_1h["bb_pct"], 3),
            "close_15m": ind_15m["close"],
        }
