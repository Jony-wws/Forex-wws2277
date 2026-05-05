"""Price Action analysis - candlestick patterns and structure."""
from __future__ import annotations

import pandas as pd


def detect_patterns(df: pd.DataFrame) -> list[dict]:
    """Detect key candlestick patterns in recent bars."""
    if df.empty or len(df) < 5:
        return []

    patterns = []
    o, h, l, c = df["Open"].values, df["High"].values, df["Low"].values, df["Close"].values

    for i in range(max(3, len(df) - 10), len(df)):
        body = abs(c[i] - o[i])
        upper_wick = h[i] - max(c[i], o[i])
        lower_wick = min(c[i], o[i]) - l[i]
        full_range = h[i] - l[i]
        if full_range == 0:
            continue

        bullish = c[i] > o[i]
        body_ratio = body / full_range

        # Doji
        if body_ratio < 0.1:
            patterns.append({"type": "doji", "bar": i, "signal": 0,
                             "name_ru": "Доджи — неопределённость"})

        # Hammer (bullish reversal)
        if lower_wick > body * 2 and upper_wick < body * 0.5 and bullish:
            patterns.append({"type": "hammer", "bar": i, "signal": 1,
                             "name_ru": "Молот — разворот вверх"})

        # Shooting star (bearish reversal)
        if upper_wick > body * 2 and lower_wick < body * 0.5 and not bullish:
            patterns.append({"type": "shooting_star", "bar": i, "signal": -1,
                             "name_ru": "Падающая звезда — разворот вниз"})

        # Engulfing
        if i > 0:
            prev_body = abs(c[i-1] - o[i-1])
            if body > prev_body * 1.3:
                if bullish and c[i-1] < o[i-1]:
                    patterns.append({"type": "bullish_engulfing", "bar": i, "signal": 2,
                                     "name_ru": "Бычье поглощение"})
                elif not bullish and c[i-1] > o[i-1]:
                    patterns.append({"type": "bearish_engulfing", "bar": i, "signal": -2,
                                     "name_ru": "Медвежье поглощение"})

        # Pin bar
        if lower_wick > full_range * 0.6 and body_ratio < 0.25:
            patterns.append({"type": "pin_bar_bull", "bar": i, "signal": 2,
                             "name_ru": "Пин-бар (бычий)"})
        elif upper_wick > full_range * 0.6 and body_ratio < 0.25:
            patterns.append({"type": "pin_bar_bear", "bar": i, "signal": -2,
                             "name_ru": "Пин-бар (медвежий)"})

        # Strong momentum candle
        if body_ratio > 0.8:
            if bullish:
                patterns.append({"type": "strong_bull", "bar": i, "signal": 1,
                                 "name_ru": "Сильная бычья свеча"})
            else:
                patterns.append({"type": "strong_bear", "bar": i, "signal": -1,
                                 "name_ru": "Сильная медвежья свеча"})

    # Three soldiers / three crows
    if len(df) >= 3:
        last3 = df.tail(3)
        o3, c3 = last3["Open"].values, last3["Close"].values
        if all(c3[i] > o3[i] for i in range(3)) and c3[0] < c3[1] < c3[2]:
            patterns.append({"type": "three_soldiers", "bar": len(df)-1, "signal": 3,
                             "name_ru": "Три белых солдата — сильный рост"})
        elif all(c3[i] < o3[i] for i in range(3)) and c3[0] > c3[1] > c3[2]:
            patterns.append({"type": "three_crows", "bar": len(df)-1, "signal": -3,
                             "name_ru": "Три чёрных вороны — сильное падение"})

    return patterns


def price_action_score(df: pd.DataFrame) -> tuple[int, list[str]]:
    """Calculate combined price action score and return reasons."""
    patterns = detect_patterns(df)
    if not patterns:
        return 0, []

    # Take only the most recent patterns (last 5)
    recent = patterns[-5:]
    total = sum(p["signal"] for p in recent)
    reasons = [p["name_ru"] for p in recent if p["signal"] != 0]

    # Cap the score
    total = max(-5, min(5, total))
    return total, reasons
