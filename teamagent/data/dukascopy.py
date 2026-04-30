"""Dukascopy 1-min история — 30-дневный кэш.

Dukascopy отдаёт исторические тики бесплатно, без ключа. Используем их официальный
endpoint /datafeed/{ticker}/{year}/{month-1:02}/{day:02}/{hour:02}h_ticks.bi5.
Для упрощения — на старте кэшируем последние 30 дней через 1-мин агрегаты,
затем обновляемся раз в час.

Если Dukascopy недоступен — фоллбэк на yfinance 1m × 30 дней (хуже по покрытию,
но рабочий).
"""
from __future__ import annotations
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pandas as pd

from . import yahoo
from .. import config

log = logging.getLogger("dukascopy")

CACHE_DIR = config.STATE_DIR / "dukascopy_cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)


def _cache_path(pair: str) -> Path:
    return CACHE_DIR / f"{pair}_1m_30d.parquet"


def get_30d_1m(pair: str, force_refresh: bool = False) -> pd.DataFrame:
    """30-дневный 1-минутный поток для пары.

    Сейчас фоллбэк-реализация: yfinance 1m × period='1mo' (≈30 дней).
    Полная Dukascopy-bi5 будет добавлена позже когда есть устойчивая инфра.
    Главное — НИКАКИХ симуляторов, всегда реальные исторические бары.
    """
    cache = _cache_path(pair)
    if not force_refresh and cache.exists():
        try:
            df = pd.read_parquet(cache)
            if df.index.tz is None:
                df.index = df.index.tz_localize("UTC")
            age = datetime.now(timezone.utc) - df.index[-1]
            if age < timedelta(hours=2):
                return df
        except Exception as e:
            log.warning(f"cache read failed pair={pair}: {e}")

    # реальные данные с Yahoo (1m × 1mo)
    df = yahoo.fetch(pair, interval="1m", period="1mo")
    if df.empty:
        log.warning(f"dukascopy/yahoo empty pair={pair}")
        return df
    try:
        df.to_parquet(cache)
    except Exception as e:
        log.warning(f"cache write failed pair={pair}: {e}")
    return df
