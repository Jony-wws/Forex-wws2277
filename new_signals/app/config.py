"""Configuration for Forex Signals system."""
from __future__ import annotations

from datetime import timezone, timedelta

# UTC+5 timezone
TZ_UTC5 = timezone(timedelta(hours=5))

# 28 currency pairs (majors + crosses)
PAIRS: list[str] = [
    "EURUSD", "GBPUSD", "USDJPY", "USDCHF", "AUDUSD", "USDCAD", "NZDUSD",
    "EURGBP", "EURJPY", "EURCHF", "EURAUD", "EURCAD", "EURNZD",
    "GBPJPY", "GBPCHF", "GBPAUD", "GBPCAD", "GBPNZD",
    "AUDJPY", "CADJPY", "CHFJPY", "NZDJPY",
    "AUDCAD", "AUDCHF", "AUDNZD",
    "CADCHF", "NZDCAD", "NZDCHF",
]

PAIR_NAMES_RU: dict[str, str] = {
    "EURUSD": "Евро / Доллар",
    "GBPUSD": "Фунт / Доллар",
    "USDJPY": "Доллар / Йена",
    "USDCHF": "Доллар / Франк",
    "AUDUSD": "Австр. доллар / Доллар",
    "USDCAD": "Доллар / Канад. доллар",
    "NZDUSD": "Новозел. доллар / Доллар",
    "EURGBP": "Евро / Фунт",
    "EURJPY": "Евро / Йена",
    "EURCHF": "Евро / Франк",
    "EURAUD": "Евро / Австр. доллар",
    "EURCAD": "Евро / Канад. доллар",
    "EURNZD": "Евро / Новозел. доллар",
    "GBPJPY": "Фунт / Йена",
    "GBPCHF": "Фунт / Франк",
    "GBPAUD": "Фунт / Австр. доллар",
    "GBPCAD": "Фунт / Канад. доллар",
    "GBPNZD": "Фунт / Новозел. доллар",
    "AUDJPY": "Австр. доллар / Йена",
    "CADJPY": "Канад. доллар / Йена",
    "CHFJPY": "Франк / Йена",
    "NZDJPY": "Новозел. доллар / Йена",
    "AUDCAD": "Австр. доллар / Канад. доллар",
    "AUDCHF": "Австр. доллар / Франк",
    "AUDNZD": "Австр. доллар / Новозел. доллар",
    "CADCHF": "Канад. доллар / Франк",
    "NZDCAD": "Новозел. доллар / Канад. доллар",
    "NZDCHF": "Новозел. доллар / Франк",
}

def yahoo_ticker(pair: str) -> str:
    return f"{pair}=X"

MIN_CONFIDENCE = 80
FORECAST_EXPIRY_HOURS = 5
FORECAST_24H_HOURS = 24
SCAN_INTERVAL_SEC = 10
