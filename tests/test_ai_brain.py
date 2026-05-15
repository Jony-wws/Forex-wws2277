"""Deterministic tests for the new AI brain modules.

These cover the pure-logic helpers (SMC, Wyckoff, Volume Profile,
macro currency-strength, fundamental carry, news veto plumbing) that
run without yfinance/network.  Live brain runs are exercised in CI by
``scripts/ai_brain.py`` directly — this file is the unit-level guard.

Run with: ``python -m pytest tests/test_ai_brain.py -q``
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest


def _synthetic_df(seed: int = 0, n: int = 250, drift: float = 0.0):
    """Build a deterministic OHLCV DataFrame for indicator tests."""
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2025-01-01", periods=n, freq="1h", tz="UTC")
    close = 1.10 + np.cumsum(rng.standard_normal(n) * 0.0006 + drift)
    high = close + np.abs(rng.standard_normal(n) * 0.0004)
    low = close - np.abs(rng.standard_normal(n) * 0.0004)
    open_ = close + rng.standard_normal(n) * 0.0002
    return pd.DataFrame(
        {"Open": open_, "High": high, "Low": low, "Close": close,
         "Volume": rng.random(n) * 1000},
        index=idx,
    )


def test_smc_basic_shape():
    from app.smc import smc_score
    df = _synthetic_df(seed=42)
    out = smc_score(df)
    assert -6 <= out["score"] <= 6, "SMC score must be clamped to ±6"
    assert isinstance(out["order_blocks"], list)
    assert isinstance(out["fvgs"], list)
    assert "structure" in out


def test_smc_empty_dataframe():
    from app.smc import smc_score
    out = smc_score(pd.DataFrame())
    assert out["score"] == 0
    assert out["reasons"] == []


def test_wyckoff_classifies_drift():
    from app.wyckoff import wyckoff_phase
    bull = _synthetic_df(seed=1, drift=+0.0015)
    bear = _synthetic_df(seed=2, drift=-0.0015)
    # Phase labels should *not* both be "range" with strong drift.
    phases = {wyckoff_phase(bull)["phase"], wyckoff_phase(bear)["phase"]}
    assert phases != {"range"}


def test_volume_profile_keys():
    from app.volume_profile import volume_profile
    df = _synthetic_df(seed=3)
    vp = volume_profile(df)
    for k in ("poc", "vah", "val", "score", "reason"):
        assert k in vp


def test_volume_profile_short_data():
    from app.volume_profile import volume_profile
    df = _synthetic_df(seed=4, n=10)
    vp = volume_profile(df)
    assert vp["score"] == 0


def test_macro_currency_strength_zero_input():
    from app.macro import CURRENCIES, currency_strength_from_macro
    out = currency_strength_from_macro({})
    assert set(out.keys()) == set(CURRENCIES)
    assert all(v == 0.0 for v in out.values())


def test_macro_currency_strength_signed_clip():
    from app.macro import currency_strength_from_macro
    out = currency_strength_from_macro({"DXY": 10.0})  # huge bullish USD
    assert out["USD"] == 3.0   # clipped to +3
    assert out["EUR"] < 0      # opposite side


def test_pair_macro_score_eur_usd():
    from app.macro import pair_macro_score
    score = pair_macro_score(
        "EURUSD",
        {"USD": 2.0, "EUR": -2.0, "GBP": 0, "JPY": 0,
         "CHF": 0, "AUD": 0, "CAD": 0, "NZD": 0},
    )
    assert score["score"] <= -3 + 0.001  # EUR much weaker than USD
    assert "EUR" in score["reason"]


def test_brain_fundamental_bias_known_pair():
    from app.brain import _fundamental_bias
    out = _fundamental_bias("USDJPY")  # USD high carry vs JPY low carry
    assert out["score"] >= 1


def test_brain_fundamental_bias_unknown_pair():
    from app.brain import _fundamental_bias
    out = _fundamental_bias("XAGUSD")  # not in POLICY_RATES_PCT
    assert out["score"] == 0


def test_brain_next_cycle_iso_returns_future():
    from datetime import datetime, timezone
    from app.brain import _next_cycle_iso
    iso = _next_cycle_iso()
    parsed = datetime.fromisoformat(iso)
    assert parsed > datetime.now(timezone.utc)


def test_news_brain_tags_currencies():
    from app.news_brain import _tag_currencies
    tags = _tag_currencies("Fed signals rate cut as US inflation cools")
    assert "USD" in tags
    tags = _tag_currencies("ECB official says Germany economy weak")
    assert "EUR" in tags


if __name__ == "__main__":
    pytest.main([__file__, "-q"])
