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

# Strict trend-quality gate. A pair only gets a BUY/SELL "strict" signal on
# the dashboard when ALL of the following hold:
#   - trend_quality >= STRICT_TREND_QUALITY (composite metric of ADX + Aroon
#     + Heiken Ashi + multi-TF + ATR + momentum + score ratio)
#   - confidence >= MIN_CONFIDENCE
#   - multi_tf_aligned (all 4 senior timeframes D1+H4+H1+M15 in same direction)
# Tuned to surface only forecasts that should travel meaningfully far from
# the entry price, not chop sideways.
STRICT_TREND_QUALITY = 75
STRICT_REQUIRE_MULTI_TF = True

# Minimum number of forecasts that the dashboard MUST always show, even
# when the market is quiet and no pair clears the strict gate. We then
# fall back to the top-N pairs sorted by trend_quality desc — the best
# available even if they are not strictly above the gate. Marked with
# `signal_kind = "fallback"` (badge "ТОП-3") so users know it's the best
# available rather than a strict signal.
MIN_FORECASTS = 3
