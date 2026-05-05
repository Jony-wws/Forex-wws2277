"""Configuration for Forex Signals system."""
from __future__ import annotations
from datetime import timezone, timedelta

TZ_UTC5 = timezone(timedelta(hours=5))

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


MIN_CONFIDENCE = 80
FORECAST_EXPIRY_HOURS = 5
SCAN_INTERVAL_SEC = 10
