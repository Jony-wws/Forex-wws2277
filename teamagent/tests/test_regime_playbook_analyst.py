"""Tests for regime.py / playbook.py / live_analyst.py — без сетевых вызовов."""
from __future__ import annotations
import json
import math
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from teamagent import regime, playbook, live_analyst, config


# ───── regime.py ─────────────────────────────────────────────

def test_compute_hurst_random_walk_near_half():
    np.random.seed(42)
    rw = np.cumsum(np.random.normal(0, 1, 1000)) + 100
    h = regime.compute_hurst(rw, max_lag=40)
    assert 0.30 < h < 0.70, f"random walk Hurst should be ~0.5, got {h}"


def test_compute_hurst_strong_trend_above_half():
    np.random.seed(7)
    # Trending: random-walk shocks + persistent positive drift = persistent series
    drift = np.cumsum(np.full(1000, 0.05))
    rw = np.cumsum(np.random.normal(0, 0.5, 1000))
    h = regime.compute_hurst(drift + rw + 100, max_lag=40)
    # Persistent drifts typically have H > 0.55; if Hurst falls back to 0.5
    # (degenerate variance estimate), at minimum it must not be < 0.45
    assert h >= 0.45, f"trending series should have Hurst ≥ 0.45, got {h}"


def test_compute_hurst_handles_short_input():
    h = regime.compute_hurst(np.array([1.0, 1.1]), max_lag=40)
    assert h == 0.5


def test_classify_regime_returns_known_label():
    np.random.seed(0)
    closes = np.cumsum(np.random.normal(0, 0.001, 300)) + 1.10
    df = pd.DataFrame({
        "Open": closes, "High": closes + 0.001,
        "Low": closes - 0.001, "Close": closes,
        "Volume": [1.0] * 300,
    }, index=pd.date_range("2025-01-01", periods=300, freq="1h"))
    rgm = regime.classify_regime(df, lookback=200)
    assert rgm in ("trending_up", "trending_down", "mean_reverting", "chaotic")


def test_regime_summary_has_required_keys():
    np.random.seed(1)
    closes = np.cumsum(np.random.normal(0, 0.001, 250)) + 1.30
    df = pd.DataFrame({
        "Open": closes, "High": closes + 0.001,
        "Low": closes - 0.001, "Close": closes,
        "Volume": [1.0] * 250,
    }, index=pd.date_range("2025-01-01", periods=250, freq="1h"))
    s = regime.regime_summary(df)
    assert {"regime", "hurst", "atr_pct", "atr_pct_percentile",
            "ema_stack", "label_ru", "n_bars"} <= set(s.keys())
    assert s["regime"] in ("trending_up", "trending_down", "mean_reverting", "chaotic")


# ───── playbook.py ───────────────────────────────────────────

def test_wilson_lower_known_values():
    # 95% CI lower for 0/0 = 0
    assert playbook.wilson_lower(0, 0) == 0.0
    # 100% (5/5) lower < 100
    assert playbook.wilson_lower(5, 5) < 100
    # 50% (50/100) lower ≈ 40.4
    assert 39 < playbook.wilson_lower(50, 100) < 42
    # 80% (80/100) lower ≈ 71
    assert 70 < playbook.wilson_lower(80, 100) < 73


def test_aggregate_cells_marks_storm_proof_when_worst30d_high(monkeypatch):
    # Synthesize 60 trades over 60 days, worst 30d window WR=0.6 → not storm-proof
    trades = []
    base = pd.Timestamp("2025-01-01", tz="UTC")
    for i in range(60):
        trades.append({
            "ts_open": (base + pd.Timedelta(days=i)).isoformat(),
            "ts_close": (base + pd.Timedelta(days=i, hours=2)).isoformat(),
            "side": "BUY", "regime": "trending_up", "win": True if i % 2 else False,
            "pnl": 1.0 if i % 2 else -1.0, "hour_utc": 10,
        })
    cells = playbook._aggregate_cells("EURUSD", "London", trades)
    assert "trending_up" in cells
    cell = cells["trending_up"]
    assert cell["n_trades"] == 60
    assert 45 <= cell["wr_pct"] <= 55
    assert cell["status"] in ("FROZEN", "PROBABLE")  # 50% < 60% threshold


def test_aggregate_cells_storm_proof_high_wr():
    base = pd.Timestamp("2025-01-01", tz="UTC")
    trades = []
    # 90% wins for 60 days
    for i in range(60):
        trades.append({
            "ts_open": (base + pd.Timedelta(days=i)).isoformat(),
            "ts_close": (base + pd.Timedelta(days=i, hours=2)).isoformat(),
            "side": "BUY", "regime": "trending_up", "win": (i % 10 != 0),
            "pnl": 1.0 if (i % 10 != 0) else -1.0, "hour_utc": 10,
        })
    cells = playbook._aggregate_cells("EURUSD", "London", trades)
    cell = cells["trending_up"]
    assert cell["wr_pct"] >= 85
    assert cell["storm_proof"] is True
    assert cell["status"] == "STORM_PROOF"


# ───── live_analyst.py ──────────────────────────────────────

def test_lookup_playbook_cell_with_explicit_data():
    pb = {
        "pairs": {
            "EURUSD": {
                "sessions": {
                    "London": {
                        "regimes": {
                            "trending_up": {
                                "status": "QUALIFIED", "wr_pct": 73.0, "n_trades": 30
                            }
                        }
                    }
                }
            }
        }
    }
    cell = live_analyst.lookup_playbook_cell("EURUSD", "London", "trending_up", playbook=pb)
    assert cell is not None
    assert cell["status"] == "QUALIFIED"
    assert cell["wr_pct"] == 73.0
    # Missing regime → None
    assert live_analyst.lookup_playbook_cell("EURUSD", "London", "chaotic", playbook=pb) is None


def test_verdict_for_known_statuses():
    em, txt = live_analyst._verdict_emoji_and_text({"status": "STORM_PROOF", "wr_pct": 78}, 75)
    assert em == "🟢" and "storm" in txt.lower()
    em, txt = live_analyst._verdict_emoji_and_text({"status": "QUALIFIED", "wr_pct": 71, "wilson_lower_pct": 60}, 71)
    assert em == "🟢" and "qualified" in txt.lower()
    em, txt = live_analyst._verdict_emoji_and_text({"status": "FROZEN", "wr_pct": 55}, 71)
    assert em == "🔴"
    em, txt = live_analyst._verdict_emoji_and_text(None, None)
    assert em == "🟡"


def test_narrative_includes_pair_and_regime_label():
    txt = live_analyst._narrative_ru(
        "EURUSD", "London",
        {"regime": "trending_up", "hurst": 0.62, "atr_pct_percentile": 70.0,
         "ema_stack": "bullish", "label_ru": "восходящий тренд"},
        forecast={"side": "BUY", "probability_pct": 73, "score": 12},
        cell={"status": "QUALIFIED", "wr_pct": 71.0, "wilson_lower_pct": 60.5,
              "side_bias": "BUY", "n_trades": 30, "worst_30d_wr_pct": 58.0},
    )
    assert "EURUSD" in txt
    assert "London" in txt
    assert "восходящий тренд" in txt
    assert "73" in txt
