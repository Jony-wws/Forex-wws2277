"""Configuration for Forex Signals system."""
from __future__ import annotations
from datetime import datetime, timezone, timedelta

TZ_UTC5 = timezone(timedelta(hours=5))

# Single source of truth for FX trading sessions (UTC hours, [start, end))
# Aligned across the codebase so a given UTC hour belongs to exactly one session.
SESSIONS: dict[str, tuple[int, int]] = {
    "Asia": (0, 7),
    "London": (7, 13),
    "Overlap": (13, 17),
    "NY": (17, 21),
}


def detect_session(now: datetime | None = None) -> str:
    """Return the active session label for the given (or current) UTC time."""
    if now is None:
        now = datetime.now(timezone.utc)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    else:
        now = now.astimezone(timezone.utc)

    hour = now.hour
    for name, (start, end) in SESSIONS.items():
        if start <= hour < end:
            return name
    return "Closed"

# All major forex pairs + crosses
PAIRS: list[str] = [
    # Majors
    "EURUSD", "GBPUSD", "USDJPY", "USDCHF", "AUDUSD", "USDCAD", "NZDUSD",
    # EUR crosses
    "EURGBP", "EURJPY", "EURCHF", "EURAUD", "EURCAD", "EURNZD",
    # GBP crosses
    "GBPJPY", "GBPCHF", "GBPAUD", "GBPCAD", "GBPNZD",
    # JPY crosses
    "AUDJPY", "CADJPY", "CHFJPY", "NZDJPY",
    # Other crosses
    "AUDCAD", "AUDCHF", "AUDNZD",
    "CADCHF", "NZDCAD", "NZDCHF",
]

PAIR_NAMES_RU: dict[str, str] = {
    "EURUSD": "Евро / Доллар США",
    "GBPUSD": "Фунт / Доллар США",
    "USDJPY": "Доллар США / Йена",
    "USDCHF": "Доллар США / Франк",
    "AUDUSD": "Австрал. доллар / Доллар США",
    "USDCAD": "Доллар США / Канад. доллар",
    "NZDUSD": "Новозел. доллар / Доллар США",
    "EURGBP": "Евро / Фунт",
    "EURJPY": "Евро / Йена",
    "EURCHF": "Евро / Франк",
    "EURAUD": "Евро / Австрал. доллар",
    "EURCAD": "Евро / Канад. доллар",
    "EURNZD": "Евро / Новозел. доллар",
    "GBPJPY": "Фунт / Йена",
    "GBPCHF": "Фунт / Франк",
    "GBPAUD": "Фунт / Австрал. доллар",
    "GBPCAD": "Фунт / Канад. доллар",
    "GBPNZD": "Фунт / Новозел. доллар",
    "AUDJPY": "Австрал. доллар / Йена",
    "CADJPY": "Канад. доллар / Йена",
    "CHFJPY": "Франк / Йена",
    "NZDJPY": "Новозел. доллар / Йена",
    "AUDCAD": "Австрал. доллар / Канад. доллар",
    "AUDCHF": "Австрал. доллар / Франк",
    "AUDNZD": "Австрал. доллар / Новозел. доллар",
    "CADCHF": "Канад. доллар / Франк",
    "NZDCAD": "Новозел. доллар / Канад. доллар",
    "NZDCHF": "Новозел. доллар / Франк",
}


def yahoo_ticker(pair: str) -> str:
    return f"{pair}=X"


MIN_CONFIDENCE = 82
FORECAST_EXPIRY_HOURS = 5
SCAN_INTERVAL_SEC = 10

# === THREE-TIER SIGNAL CLASSIFICATION =================================
#
# After a pair is analysed it is classified into one of four buckets,
# strongest first:
#
#   1. "premium"   — final control. ALL premium criteria below must pass.
#                    Designed to surface only pairs with an obvious,
#                    powerful trend that should travel a meaningful
#                    distance from the entry over the 5h forecast window.
#   2. "strict"    — solid setup. Multi-TF aligned + composite quality
#                    + confidence above MIN_CONFIDENCE.
#   3. "fallback"  — no premium nor strict pair on the board OR fewer
#                    than MIN_FORECASTS qualified. The top-N by
#                    trend_quality are promoted so the dashboard never
#                    shows zero forecasts.
#   4. None        — no signal shown for this pair right now.
#
# Premium is intended to be RARE. On a quiet market session it is normal
# for the premium count to be 0 — that just means there is no obviously
# powerful trend right now. The min-3 fallback still kicks in.

# --- Premium tier (strongest filter) ---
PREMIUM_TREND_QUALITY = 85          # composite trend_quality 0..100
PREMIUM_MIN_CONFIDENCE = 88         # signal-confidence 50..92
PREMIUM_MIN_ADX = 28.0              # ADX(1h) — strong-trend cutoff
PREMIUM_MIN_AROON_OSC = 70.0        # |Aroon osc| — directional persistence
PREMIUM_MIN_HA_BULL_EXTREME = 0.83  # bull_ratio >= 0.83 (5/6 candles same dir)
PREMIUM_MIN_HA_BODY = 0.55          # body_strength — strong directional candles
PREMIUM_MIN_MOMENTUM = 0.20         # |momentum %| over 1h
PREMIUM_MIN_MOVE_PIPS_NONJPY = 60.0 # expected ATR-implied 5h move
PREMIUM_MIN_MOVE_PIPS_JPY = 100.0   # JPY pairs travel more pips per ATR unit
PREMIUM_REQUIRE_MULTI_TF = True

# --- Strict tier (solid) ---
STRICT_TREND_QUALITY = 75
STRICT_REQUIRE_MULTI_TF = True

# --- Fallback (always show at least MIN_FORECASTS rows) ---
MIN_FORECASTS = 3
