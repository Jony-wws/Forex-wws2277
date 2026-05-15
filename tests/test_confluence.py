"""Unit tests for ``app.confluence`` — multi-TF + multi-indicator
confluence scorer.

These tests stub out ``fetch_bars`` so they run offline (no network).
They lock down the score sign, the ``super_confluence`` boolean's
preconditions, and the ``confluence_norm`` normalisation contract.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from app import confluence as conf_mod
from app.confluence import confluence_norm, confluence_snapshot


def _trend_frame(n: int, direction: int, noise: float = 0.00002) -> pd.DataFrame:
    """Synthetic OHLCV frame trending in ``direction`` (+1 up, -1 down)."""
    rng = np.random.default_rng(direction + 7)
    base = 1.0 + direction * 0.0002 * np.arange(n, dtype=float)
    base += rng.normal(0, noise, size=n)
    open_ = base
    close = base + direction * 0.0001
    high = np.maximum(open_, close) + 0.0001
    low = np.minimum(open_, close) - 0.0001
    vol = np.full(n, 1000.0)
    idx = pd.date_range("2026-01-01", periods=n, freq="1h")
    return pd.DataFrame(
        {"Open": open_, "High": high, "Low": low, "Close": close, "Volume": vol},
        index=idx,
    )


def _flat_frame(n: int) -> pd.DataFrame:
    """Flat OHLCV frame — no trend, indicators should all be neutral."""
    flat = np.full(n, 1.0)
    rng = np.random.default_rng(0)
    flat = flat + rng.normal(0, 0.00001, size=n)
    open_ = flat
    close = flat
    high = flat + 0.00001
    low = flat - 0.00001
    vol = np.full(n, 1000.0)
    idx = pd.date_range("2026-01-01", periods=n, freq="1h")
    return pd.DataFrame(
        {"Open": open_, "High": high, "Low": low, "Close": close, "Volume": vol},
        index=idx,
    )


def _patch_fetch_bars(monkeypatch, direction: int):
    """Patch fetch_bars so every TF gets a trending frame in ``direction``."""

    def _fake_fetch(pair, interval, period):
        # Senior TFs get longer history so EMA200 has enough samples.
        if interval in ("1wk", "1d"):
            return _trend_frame(250, direction)
        return _trend_frame(120, direction)

    monkeypatch.setattr(conf_mod, "fetch_bars", _fake_fetch)


def _patch_fetch_bars_flat(monkeypatch):
    def _fake_fetch(pair, interval, period):
        if interval in ("1wk", "1d"):
            return _flat_frame(250)
        return _flat_frame(120)

    monkeypatch.setattr(conf_mod, "fetch_bars", _fake_fetch)


# ─── confluence_snapshot ─────────────────────────────────────────────


def test_confluence_snapshot_uptrend_returns_buy(monkeypatch):
    _patch_fetch_bars(monkeypatch, +1)
    snap = confluence_snapshot("EURUSD")
    assert snap is not None
    assert snap["side"] == "BUY"
    assert snap["score"] > 0
    assert snap["tf_aligned"] is True
    assert snap["bull_votes"] >= snap["bear_votes"]


def test_confluence_snapshot_downtrend_returns_sell(monkeypatch):
    _patch_fetch_bars(monkeypatch, -1)
    snap = confluence_snapshot("EURUSD")
    assert snap is not None
    assert snap["side"] == "SELL"
    assert snap["score"] < 0
    assert snap["tf_aligned"] is True
    assert snap["bear_votes"] >= snap["bull_votes"]


def test_confluence_snapshot_flat_does_not_super_confluence(monkeypatch):
    """On a flat market multi-TF alignment must not fire, super_confluence
    must remain False, and ADX must stay well below the trend threshold.

    A flat market with no real direction can still have noise-driven
    bullish or bearish votes by chance, but ``tf_aligned`` requires
    *all five* timeframes to point the same way — which won't happen
    on a truly directionless price.  ``super_confluence`` additionally
    needs ADX(H1) ≥ 22, which only fires on real trends.
    """
    _patch_fetch_bars_flat(monkeypatch)
    snap = confluence_snapshot("EURUSD")
    if snap is None:
        return
    assert snap["super_confluence"] is False
    assert snap["tf_aligned"] is False
    assert snap["adx_h1"] < 22.0


def test_confluence_snapshot_returns_none_when_short_data(monkeypatch):
    def _short_fetch(pair, interval, period):
        return _trend_frame(15, +1)

    monkeypatch.setattr(conf_mod, "fetch_bars", _short_fetch)
    assert confluence_snapshot("EURUSD") is None


def test_super_confluence_requires_alignment_adx_and_votes(monkeypatch):
    _patch_fetch_bars(monkeypatch, +1)
    snap = confluence_snapshot("EURUSD")
    assert snap is not None
    # On a clean, strongly-trending synthetic uptrend every condition
    # should fire: all 5 TFs aligned, ≥7 votes, ADX high, momentum aligned.
    assert snap["tf_aligned"] is True
    assert snap["bull_votes"] >= 7
    assert snap["adx_h1"] >= 22.0


# ─── confluence_norm ─────────────────────────────────────────────────


def test_confluence_norm_none_returns_zero():
    assert confluence_norm(None) == 0.0


def test_confluence_norm_no_side_returns_zero():
    snap = {"side": None, "score": 10, "max_score": 20}
    assert confluence_norm(snap) == 0.0


def test_confluence_norm_returns_signed_ratio():
    snap = {"side": "BUY", "score": 17, "max_score": 34}
    assert abs(confluence_norm(snap) - 0.5) < 1e-9


def test_confluence_norm_clamped_to_unit_interval():
    snap = {"side": "BUY", "score": 100, "max_score": 34}
    # Score > max gets clamped to +1.
    assert confluence_norm(snap) == 1.0
    snap2 = {"side": "SELL", "score": -100, "max_score": 34}
    assert confluence_norm(snap2) == -1.0
