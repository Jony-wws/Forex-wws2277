"""Тесты для teamagent/free_signals.py — 5 бесплатных источников ансамбля.

Источники: currency strength · DXY · ATR regime · JPY confluence · VP distance.
"""
from __future__ import annotations
import pandas as pd
import pytest

from teamagent import free_signals


def _mk_df(start_close: float, end_close: float, n_bars: int = 30) -> pd.DataFrame:
    """Линейная серия close от start_close до end_close на n_bars."""
    closes = [start_close + (end_close - start_close) * i / (n_bars - 1) for i in range(n_bars)]
    idx = pd.date_range("2026-04-30", periods=n_bars, freq="1h", tz="UTC")
    return pd.DataFrame({"Close": closes, "High": closes, "Low": closes, "Open": closes}, index=idx)


# ─────────────────────── currency strength ───────────────────────

def test_currency_strength_empty():
    out = free_signals.compute_currency_strength_matrix({})
    assert out == {c: 0.0 for c in free_signals.CURRENCIES}


def test_currency_strength_eurusd_up_eur_strong():
    """Если EURUSD выросла, EUR сильнее USD."""
    bulk = {"EURUSD": _mk_df(1.0, 1.005, 25)}  # +0.5% за 24h
    out = free_signals.compute_currency_strength_matrix(bulk, lookback_bars=24)
    assert out["EUR"] > 0
    assert out["USD"] < 0
    assert out["EUR"] == -out["USD"]  # симметрия для одной пары


def test_currency_strength_jpy_weak_across_all_pairs():
    """USDJPY вверх, EURJPY вверх, GBPJPY вверх → JPY весьма слаба."""
    bulk = {
        "USDJPY": _mk_df(150.0, 151.5, 25),  # +1%
        "EURJPY": _mk_df(165.0, 166.65, 25),  # +1%
        "GBPJPY": _mk_df(190.0, 191.9, 25),  # +1%
    }
    out = free_signals.compute_currency_strength_matrix(bulk, lookback_bars=24)
    assert out["JPY"] < -10
    assert out["USD"] > 0


def test_pair_strength_signal_buy():
    sm = {"EUR": 60.0, "USD": -40.0, "JPY": 0.0, "GBP": 0.0,
          "CHF": 0.0, "AUD": 0.0, "CAD": 0.0, "NZD": 0.0}
    sig = free_signals.pair_strength_signal("EURUSD", sm)
    assert sig is not None
    assert sig["side"] == "BUY"
    assert sig["pts"] == 2  # diff = 100, > 50


def test_pair_strength_signal_sell():
    sm = {"EUR": -50.0, "USD": 30.0, "JPY": 0.0, "GBP": 0.0,
          "CHF": 0.0, "AUD": 0.0, "CAD": 0.0, "NZD": 0.0}
    sig = free_signals.pair_strength_signal("EURUSD", sm)
    assert sig is not None
    assert sig["side"] == "SELL"
    assert sig["pts"] < 0


def test_pair_strength_signal_neutral():
    sm = {"EUR": 5.0, "USD": -5.0, "JPY": 0.0, "GBP": 0.0,
          "CHF": 0.0, "AUD": 0.0, "CAD": 0.0, "NZD": 0.0}
    sig = free_signals.pair_strength_signal("EURUSD", sm)
    # diff = 10, < 25 threshold
    assert sig is None


def test_pair_strength_signal_missing_currency():
    out = free_signals.pair_strength_signal("EURUSD", {"EUR": 50})
    assert out is None  # USD missing


# ─────────────────────── DXY ───────────────────────

def test_dxy_signal_no_data():
    out = free_signals.pair_dxy_signal("EURUSD", None)
    assert out is None


def test_dxy_signal_skip_non_usd():
    df = _mk_df(105.0, 106.0, 30)
    out = free_signals.pair_dxy_signal("EURGBP", df)
    assert out is None  # пара не содержит USD


def test_dxy_up_xxxusd_sells():
    """DXY +0.5% за 24ч, EURUSD должен получить SELL bias."""
    df = _mk_df(105.0, 105.6, 30)  # +0.57%
    out = free_signals.pair_dxy_signal("EURUSD", df, lookback_bars=24)
    assert out is not None
    assert out["side"] == "SELL"
    assert out["pts"] < 0


def test_dxy_up_usdxxx_buys():
    """DXY +0.5%, USDJPY должен получить BUY bias."""
    df = _mk_df(105.0, 105.6, 30)
    out = free_signals.pair_dxy_signal("USDJPY", df, lookback_bars=24)
    assert out is not None
    assert out["side"] == "BUY"
    assert out["pts"] > 0


def test_dxy_flat_no_signal():
    df = _mk_df(105.0, 105.05, 30)  # +0.05% — ниже 0.3%
    out = free_signals.pair_dxy_signal("EURUSD", df, lookback_bars=24)
    assert out is None


# ─────────────────────── ATR regime ───────────────────────

def test_atr_regime_insufficient_data():
    out = free_signals.pair_atr_regime([], lookback_baseline=480)
    assert out is None
    out = free_signals.pair_atr_regime([(None, None, None, None, None)] * 100,
                                       lookback_baseline=480)
    assert out is None


def test_atr_regime_low_volatility():
    """Recent ATR < 0.7 × baseline → LOW."""
    snaps = []
    # baseline period: high ATR
    for i in range(480):
        snaps.append((None, None, None, {"atr14": 0.001}, None))
    # recent 24h: low ATR
    for i in range(24):
        snaps[-(24 - i) - 1] = (None, None, None, {"atr14": 0.0003}, None)
    # rebuild correctly: baseline first 456 high, last 24 low
    snaps = [(None, None, None, {"atr14": 0.001}, None) for _ in range(456)]
    snaps += [(None, None, None, {"atr14": 0.0003}, None) for _ in range(24)]
    out = free_signals.pair_atr_regime(snaps, lookback_recent=24, lookback_baseline=480)
    assert out is not None
    assert out["regime"] == "LOW"


def test_atr_regime_high_volatility():
    snaps = [(None, None, None, {"atr14": 0.001}, None) for _ in range(456)]
    snaps += [(None, None, None, {"atr14": 0.002}, None) for _ in range(24)]
    out = free_signals.pair_atr_regime(snaps, lookback_recent=24, lookback_baseline=480)
    assert out is not None
    assert out["regime"] == "HIGH"


# ─────────────────────── JPY confluence ───────────────────────

def test_jpy_confluence_skip_non_jpy():
    out = free_signals.jpy_confluence_signal("EURUSD", {"USDJPY": _mk_df(150, 151)})
    assert out is None


def test_jpy_confluence_all_up_xxxjpy_buys():
    """Все 4 JPY-пары вверх → JPY слаба → EURJPY должен получить BUY."""
    bulk = {
        "USDJPY": _mk_df(150.0, 150.5, 25),
        "EURJPY": _mk_df(165.0, 165.5, 25),
        "GBPJPY": _mk_df(190.0, 190.5, 25),
        "AUDJPY": _mk_df(95.0, 95.5, 25),
    }
    out = free_signals.jpy_confluence_signal("EURJPY", bulk, lookback_bars=24)
    assert out is not None
    assert out["side"] == "BUY"
    assert out["all_up"] is True
    assert out["pts"] > 0


def test_jpy_confluence_all_down_jpyxxx_buys():
    """Все JPY-пары вниз → JPY сильная. CHFJPY это XXXJPY (JPY=quote).
    JPY сильная → BUY JPY = SELL CHFJPY."""
    bulk = {
        "USDJPY": _mk_df(150.0, 149.0, 25),
        "EURJPY": _mk_df(165.0, 164.0, 25),
        "GBPJPY": _mk_df(190.0, 189.0, 25),
        "AUDJPY": _mk_df(95.0, 94.5, 25),
    }
    out = free_signals.jpy_confluence_signal("CHFJPY", bulk, lookback_bars=24)
    assert out is not None
    assert out["side"] == "SELL"
    assert out["all_down"] is True


def test_jpy_confluence_mixed_no_signal():
    bulk = {
        "USDJPY": _mk_df(150.0, 151.0, 25),  # up
        "EURJPY": _mk_df(165.0, 164.0, 25),  # down
        "GBPJPY": _mk_df(190.0, 191.0, 25),
        "AUDJPY": _mk_df(95.0, 95.0, 25),
    }
    out = free_signals.jpy_confluence_signal("USDJPY", bulk, lookback_bars=24)
    assert out is None  # not all same direction


# ─────────────────────── VP distance ───────────────────────

def test_vp_distance_no_file(tmp_path, monkeypatch):
    from teamagent import config
    monkeypatch.setattr(config, "STATE_DIR", tmp_path)
    out = free_signals.pair_vp_distance_signal("EURUSD")
    assert out is None
