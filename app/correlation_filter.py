"""Correlation-aware pair filter.

When the system surfaces multiple top picks at once, several of them
are often highly correlated (e.g. EURUSD + GBPUSD + AUDUSD all moving
on a DXY swing).  Holding three "diversified" trades that are really
the same trade dressed in different ticker symbols inflates risk
without inflating expected return.

This module computes a pairwise correlation matrix of *daily log
returns* over the most recent ``CORRELATION_LOOKBACK_DAYS`` window and
exposes :func:`filter_correlated_pairs`, which keeps the strongest pair
of any cluster whose pairwise correlation is above
``CORRELATION_THRESHOLD``.  The result is a deduplicated list of pairs
suitable for downstream selection (top-N picker, ensemble vote, etc.).

The filter is *order-aware*: callers pass pairs ranked by quality
(highest first), so we keep the *first occurrence* of each correlated
cluster and drop the rest.  This way the best pair always wins ties.
"""
from __future__ import annotations

import logging
from typing import Iterable, Optional, Sequence

import numpy as np
import pandas as pd

from .prices import fetch_bars

log = logging.getLogger("correlation_filter")


CORRELATION_LOOKBACK_DAYS = 30
CORRELATION_THRESHOLD = 0.8
MIN_OVERLAP_BARS = 15  # need at least 15 days of overlapping data to trust a corr


def _daily_log_returns(pair: str, days: int) -> Optional[pd.Series]:
    """Fetch the last ``days`` of daily closes and return log-returns.

    ``yfinance`` accepts strings like ``"60d"``, so we always pull a bit
    more than ``days`` to absorb missing-bar gaps and then trim to the
    last ``days`` rows.  Missing data returns ``None`` so the caller
    can skip the pair gracefully.
    """
    try:
        bars = fetch_bars(pair, "1d", "6mo")
    except Exception as e:  # noqa: BLE001
        log.debug(f"correlation fetch failed {pair}: {e}")
        return None
    if bars is None or bars.empty or len(bars) < MIN_OVERLAP_BARS:
        return None
    closes = bars["Close"].astype(float).dropna()
    if len(closes) < MIN_OVERLAP_BARS:
        return None
    closes = closes.tail(days + 1)
    returns = np.log(closes / closes.shift(1)).dropna()
    if len(returns) < MIN_OVERLAP_BARS:
        return None
    returns.name = pair
    return returns


def compute_correlation_matrix(
    pairs: Sequence[str],
    *,
    days: int = CORRELATION_LOOKBACK_DAYS,
) -> pd.DataFrame:
    """Return a square correlation matrix indexed and columned by ``pairs``.

    Pairs whose returns can't be fetched are filled with ``NaN`` rows
    so the caller can skip them.  Diagonal is forced to 1.0 (a pair is
    perfectly correlated with itself by definition).
    """
    series_by_pair: dict[str, pd.Series] = {}
    for p in pairs:
        s = _daily_log_returns(p, days)
        if s is not None:
            series_by_pair[p] = s
    if not series_by_pair:
        return pd.DataFrame(index=pairs, columns=pairs, dtype=float)
    df = pd.concat(series_by_pair.values(), axis=1, join="outer")
    corr = df.corr(min_periods=MIN_OVERLAP_BARS)
    # Reindex to the original ``pairs`` order so callers can rely on
    # the layout even if some pairs had no data.
    corr = corr.reindex(index=pairs, columns=pairs)
    # Diagonal — explicitly 1.0 for pairs that *had* data; left NaN for
    # pairs that returned no data so callers can detect the gap.
    for p in pairs:
        if p in series_by_pair:
            corr.loc[p, p] = 1.0
    return corr


def filter_correlated_pairs(
    pairs: Iterable[str],
    *,
    threshold: float = CORRELATION_THRESHOLD,
    days: int = CORRELATION_LOOKBACK_DAYS,
    correlation_matrix: Optional[pd.DataFrame] = None,
) -> list[str]:
    """Return ``pairs`` with highly-correlated duplicates removed.

    The input order is preserved — the *first* pair in any correlated
    cluster is kept and later ones are dropped.  Pass pairs in
    descending-quality order so the highest-quality pair wins.

    A pair ``Q`` is dropped if it has ``|corr(Q, K)| >= threshold`` with
    any already-kept pair ``K``.  When correlation data is missing
    (``NaN``) we conservatively *keep* the pair so the filter never
    silently drops a candidate due to data gaps.
    """
    seq = list(pairs)
    if not seq:
        return []
    if correlation_matrix is None:
        correlation_matrix = compute_correlation_matrix(seq, days=days)

    kept: list[str] = []
    for p in seq:
        is_dup = False
        for k in kept:
            try:
                c = correlation_matrix.loc[p, k]
            except (KeyError, TypeError):
                c = np.nan
            if pd.isna(c):
                continue
            if abs(float(c)) >= threshold:
                is_dup = True
                break
        if not is_dup:
            kept.append(p)
    return kept


def diversification_report(
    pairs: Sequence[str],
    *,
    threshold: float = CORRELATION_THRESHOLD,
    days: int = CORRELATION_LOOKBACK_DAYS,
) -> dict:
    """Build a small report for the dashboard / Telegram.

    Returns a dict with the kept pairs, dropped pairs grouped by
    "anchor" pair (the one we kept), and the raw correlation matrix as
    a JSON-friendly dict.  Designed to be cheap to render — does not
    include any heavy timeseries.
    """
    corr = compute_correlation_matrix(pairs, days=days)
    kept = filter_correlated_pairs(
        pairs, threshold=threshold, days=days, correlation_matrix=corr
    )
    kept_set = set(kept)
    dropped_by_anchor: dict[str, list[dict]] = {k: [] for k in kept}
    for p in pairs:
        if p in kept_set:
            continue
        # Find the *kept* pair this dropped pair was most correlated with.
        best_anchor: Optional[str] = None
        best_abs: float = 0.0
        best_signed: float = 0.0
        for k in kept:
            try:
                c = corr.loc[p, k]
            except (KeyError, TypeError):
                continue
            if pd.isna(c):
                continue
            ac = abs(float(c))
            if ac > best_abs:
                best_abs = ac
                best_signed = float(c)
                best_anchor = k
        if best_anchor is not None:
            dropped_by_anchor[best_anchor].append(
                {"pair": p, "correlation": round(best_signed, 3)}
            )

    return {
        "kept": kept,
        "dropped_by_anchor": dropped_by_anchor,
        "threshold": threshold,
        "lookback_days": days,
        "matrix": _matrix_to_json(corr),
    }


def _matrix_to_json(corr: pd.DataFrame) -> dict[str, dict[str, Optional[float]]]:
    """Lossy JSON-friendly serialisation of a correlation matrix."""
    out: dict[str, dict[str, Optional[float]]] = {}
    for row in corr.index:
        out[row] = {}
        for col in corr.columns:
            val = corr.loc[row, col]
            out[row][col] = (
                None if pd.isna(val) else round(float(val), 3)
            )
    return out
