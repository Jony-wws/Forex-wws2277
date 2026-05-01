"""Yahoo Finance — основной источник live-цен и истории.

Бесплатный, без ключа. Возвращает 1m / 5m / 15m / 1h / 4h / 1d свечи.
"""
from __future__ import annotations
import time
import logging
from datetime import datetime, timezone, timedelta

import pandas as pd
import yfinance as yf

from .. import config

log = logging.getLogger("yahoo")

# простой in-memory кэш чтобы не долбить API
_CACHE: dict[tuple[str, str, str], tuple[float, pd.DataFrame]] = {}
_TTL: dict[str, int] = {
    "1m":  60,        # 1 мин кэш
    "5m":  60,
    "15m": 90,
    "1h":  120,
    "4h":  300,
    "1d":  900,
}


def _cached(key: tuple[str, str, str], ttl: int) -> pd.DataFrame | None:
    e = _CACHE.get(key)
    if not e:
        return None
    ts, df = e
    if time.time() - ts > ttl:
        return None
    return df.copy()


def _put(key: tuple[str, str, str], df: pd.DataFrame) -> None:
    _CACHE[key] = (time.time(), df.copy())


def fetch(pair: str, interval: str = "5m", period: str = "5d") -> pd.DataFrame:
    """Скачать свечи с Yahoo Finance.

    Args:
        pair: "EURUSD" (без =X)
        interval: "1m" / "5m" / "15m" / "1h" / "4h" / "1d"
        period: "1d", "5d", "1mo", "3mo", "6mo", "1y", ...

    Returns:
        DataFrame с колонками Open/High/Low/Close/Volume и индексом UTC.
        Пустой DataFrame если данных нет.
    """
    key = (pair, interval, period)
    ttl = _TTL.get(interval, 120)
    cached = _cached(key, ttl)
    if cached is not None:
        return cached

    ticker = config.yahoo_ticker(pair)
    try:
        df = yf.download(
            ticker,
            interval=interval,
            period=period,
            progress=False,
            auto_adjust=False,
            prepost=False,
            threads=False,
        )
    except Exception as e:
        log.warning(f"yahoo fetch failed pair={pair} interval={interval}: {e}")
        return pd.DataFrame()

    if df is None or df.empty:
        log.warning(f"yahoo empty pair={pair} interval={interval}")
        return pd.DataFrame()

    # yfinance иногда отдаёт MultiIndex колонки (ticker × поле). Сплющиваем.
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = [c[0] if isinstance(c, tuple) else c for c in df.columns]

    # tz → UTC
    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC")
    else:
        df.index = df.index.tz_convert("UTC")

    df = df[["Open", "High", "Low", "Close", "Volume"]].dropna()
    _put(key, df)
    return df


def latest_price(pair: str) -> float | None:
    """Последняя 1-минутная цена закрытия. Реальный источник, никаких симуляторов."""
    df = fetch(pair, interval="1m", period="1d")
    if df.empty:
        return None
    return float(df["Close"].iloc[-1])


def latest_bars(pair: str, interval: str, n: int) -> pd.DataFrame:
    """Последние n баров заданного TF."""
    period_map = {
        "1m":  "5d",
        "5m":  "1mo",
        "15m": "1mo",
        "1h":  "3mo",
        "4h":  "6mo",
        "1d":  "1y",
    }
    df = fetch(pair, interval=interval, period=period_map.get(interval, "1mo"))
    if df.empty:
        return df
    return df.tail(n)


def settlement_price(pair: str, expiry_utc: datetime) -> float | None:
    """Цена закрытия 1-мин бара ровно на момент expiry_utc.

    Используется paper_trader для честного закрытия бинарного опциона.

    Логика:
      - Если бар точно на expiry_utc есть (±5 мин) — берём его (онлайн торговля).
      - Если expiry попал на закрытый рынок (Friday 22:00 UTC – Sunday 22:00 UTC)
        и ближайший бар в прошлом ≤ 3 дней назад — settle по этому бару (как
        делает реальный broker для weekend-expired binary options).
      - Возвращаем None только если данных нет совсем или expiry — будущий бар
        в АКТИВНЫЕ торговые часы.
    """
    df = fetch(pair, interval="1m", period="5d")
    if df.empty:
        return None
    target = expiry_utc.replace(second=0, microsecond=0)
    # find closest bar at-or-before expiry
    idx = df.index[df.index <= target]
    if len(idx) == 0:
        return None
    last = idx[-1]
    gap = target - last
    if gap <= timedelta(minutes=5):
        # обычный случай — бар прямо на expiry или почти
        return float(df.loc[last, "Close"])
    # expiry дальше чем 5 мин от последнего бара — рынок мог закрыться на выходные.
    # Если разрыв ≤ 3 суток — это нормальный weekend-gap (Fri 22:00 → Mon 00:00 ≈ 50ч)
    # и broker settle по последней цене перед закрытием. Если больше — данных нет.
    if gap <= timedelta(days=3):
        log.info(
            f"settlement_price {pair} expiry={target.isoformat()} settle@{last.isoformat()} "
            f"(weekend/closed-market gap={gap})"
        )
        return float(df.loc[last, "Close"])
    return None
