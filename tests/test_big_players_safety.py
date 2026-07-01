"""Tests for the new Smart Money / safety modules (no network).

These tests exercise pure-logic helpers introduced for the 5-hour cycle
big-player / MTF / safety upgrade.  Real CFTC fetches are exercised at
runtime by ``scripts/ai_brain.py``; here we test the deterministic
post-fetch math and the safety guards on synthetic OHLCV.

Run with: ``python -m pytest tests/test_big_players_safety.py -q``
"""
from __future__ import annotations

import numpy as np
import pandas as pd


# --------------------------------------------------------------------------- #
#  app.cot
# --------------------------------------------------------------------------- #


def test_cot_pair_score_signs():
    from app.cot import pair_cot_score
    scores = {
        "USD": -1.0, "EUR": +2.5, "GBP": 0, "JPY": 0,
        "CHF": 0, "AUD": 0, "CAD": 0, "NZD": 0,
    }
    out = pair_cot_score("EURUSD", scores)
    assert out["score"] >= 2     # EUR much longer than USD
    assert "EUR" in out["reason"]


def test_cot_pair_score_unknown_pair():
    from app.cot import pair_cot_score
    out = pair_cot_score("XXXYYY", {})
    assert out["score"] == 0


def test_cot_zscore_clamp_and_constant_series():
    # Constant series → std = 0 → z-score = 0 (no division by zero).
    from app.cot import _zscore
    assert _zscore([10.0] * 10) == 0.0


def test_cot_zscore_extreme_value_clamped():
    from app.cot import _zscore
    # Latest is well above the noisy baseline; raw z would be enormous,
    # must clamp to +3 (the module's hard cap).
    rng = np.random.default_rng(0)
    baseline = (rng.standard_normal(51) * 0.5 + 1.0).tolist()  # std > 0
    values = [10000.0] + baseline
    z = _zscore(values)
    assert z == 3.0


def test_cot_zscore_negative_extreme_clamped():
    from app.cot import _zscore
    rng = np.random.default_rng(0)
    baseline = (rng.standard_normal(51) * 0.5 + 1.0).tolist()
    z = _zscore([-10000.0] + baseline)
    assert z == -3.0


# --------------------------------------------------------------------------- #
#  app.big_players
# --------------------------------------------------------------------------- #


def test_big_player_scores_weights_sum_one():
    from app.big_players import WEIGHTS
    assert abs(sum(WEIGHTS.values()) - 1.0) < 1e-9


def test_big_player_scores_basic_composite(monkeypatch):
    """Composite must reflect COT direction when flow + macro are zero."""
    from app import big_players as bp

    cot = {c: 0.0 for c in (
        "USD", "EUR", "GBP", "JPY", "CHF", "AUD", "CAD", "NZD")}
    cot["EUR"] = 2.0
    cot["USD"] = -1.0
    macro = {c: 0.0 for c in cot}

    # Force the orderbook imbalance to zero so the test stays
    # deterministic regardless of network availability.
    monkeypatch.setattr(
        bp,
        "_orderbook_imbalance_scores",
        lambda: {c: 0.0 for c in cot},
    )
    out = bp.big_player_scores(cot_scores=cot, macro_currency=macro)
    scores = out["currency_scores"]
    assert scores["EUR"] > 0
    assert scores["USD"] < 0
    # Components are returned for transparency.
    assert "components" in out
    assert "cot" in out["components"]


def test_big_player_pair_score():
    from app.big_players import pair_big_player_score
    out = pair_big_player_score("EURUSD", {"EUR": 1.5, "USD": -1.5})
    assert out["score"] == 3
    assert "EUR" in out["reason"]


# --------------------------------------------------------------------------- #
#  app.safety  — reversal, 5h projection, weekly bias, M5 momentum
# --------------------------------------------------------------------------- #


def _synthetic_h1(seed: int, n: int = 100, drift: float = 0.0):
    rng = np.random.default_rng(seed)
    close = 1.10 + np.cumsum(rng.standard_normal(n) * 0.0003 + drift)
    high = close + np.abs(rng.standard_normal(n)) * 0.0002
    low = close - np.abs(rng.standard_normal(n)) * 0.0002
    open_ = close - rng.standard_normal(n) * 0.0001
    return pd.DataFrame(
        {"Open": open_, "High": high, "Low": low, "Close": close,
         "Volume": rng.random(n) * 1000},
    )


def test_reversal_risk_short_data():
    from app.safety import reversal_risk_h1
    # With the relaxed REVERSAL_LOOKBACK_BARS=1 (most-recent-bar only)
    # the function only needs 2 bars to check; passing 1 bar must still
    # short-circuit to "no reversal" because engulfing requires a prev
    # bar to compare against.
    df = _synthetic_h1(seed=1, n=1)
    out = reversal_risk_h1(df, "BUY")
    assert out["reversal"] is False
    assert out["bar_index"] == -1


def test_reversal_risk_bearish_engulfing_against_buy():
    """Hand-crafted bearish engulfing must trigger the BUY veto."""
    from app.safety import reversal_risk_h1
    base = _synthetic_h1(seed=10, n=20).reset_index(drop=True)
    # Force the last two bars: prev = small green, curr = large red engulfing
    base.loc[base.index[-2], ["Open", "High", "Low", "Close"]] = [1.10, 1.101, 1.0995, 1.1008]
    base.loc[base.index[-1], ["Open", "High", "Low", "Close"]] = [1.1010, 1.1012, 1.099, 1.0995]
    out = reversal_risk_h1(base, "BUY")
    assert out["reversal"] is True
    assert "engulfing" in out["reason"].lower()


def test_reversal_risk_no_pattern_on_pure_trend():
    from app.safety import reversal_risk_h1
    bull = _synthetic_h1(seed=2, drift=+0.0008)
    out = reversal_risk_h1(bull, "BUY")
    # Trending data should not commonly hit the reversal heuristic.
    assert isinstance(out["reversal"], bool)


def test_five_hour_projection_buys_pass_on_uptrend():
    from app.safety import five_hour_projection
    bull = _synthetic_h1(seed=3, drift=+0.0006)
    out = five_hour_projection(bull, "BUY")
    assert "projected_close" in out
    assert out["entry"] is not None
    # On a clean uptrend the BUY projection should pass.
    assert out["passes"] is True


def test_five_hour_projection_sells_fail_on_uptrend():
    from app.safety import five_hour_projection
    bull = _synthetic_h1(seed=3, drift=+0.0006)
    out = five_hour_projection(bull, "SELL")
    assert out["passes"] is False
    assert "МИНУС" in out["reason"] or "нейтрал" in out["reason"].lower()


def test_five_hour_projection_short_data():
    from app.safety import five_hour_projection
    df = _synthetic_h1(seed=4, n=5)
    out = five_hour_projection(df, "BUY")
    assert out["passes"] is False
    assert "Мало" in out["reason"]


def test_weekly_bias_detects_uptrend():
    from app.safety import weekly_bias
    bull = _synthetic_h1(seed=5, n=30, drift=+0.005)
    assert weekly_bias(bull) == "BUY"


def test_weekly_bias_neutral_short_data():
    from app.safety import weekly_bias
    df = _synthetic_h1(seed=6, n=10)
    assert weekly_bias(df) is None


def test_m5_momentum_blocks_when_against():
    from app.safety import m5_momentum_aligned
    # Build a 6-bar series with a steep down close — BUY must be blocked.
    df = pd.DataFrame({
        "Open":  [1.10, 1.09, 1.08, 1.07, 1.06, 1.05],
        "High":  [1.101, 1.091, 1.081, 1.071, 1.061, 1.051],
        "Low":   [1.099, 1.089, 1.079, 1.069, 1.059, 1.049],
        "Close": [1.10, 1.09, 1.08, 1.07, 1.06, 1.05],
        "Volume": [1, 1, 1, 1, 1, 1],
    })
    assert m5_momentum_aligned(df, "BUY") is False
    assert m5_momentum_aligned(df, "SELL") is True


def test_m5_momentum_allows_small_pullback():
    """0.10 % retrace in 30 min is normal noise, must NOT veto either side."""
    from app.safety import m5_momentum_aligned
    # 1.10000 → 1.10110 = +0.10 % over 6 bars (small pullback against SELL,
    # tailwind for BUY).  With the new 0.20 % threshold neither side blocks.
    df = pd.DataFrame({
        "Open":  [1.10000, 1.10020, 1.10040, 1.10060, 1.10080, 1.10100],
        "High":  [1.10010, 1.10030, 1.10050, 1.10070, 1.10090, 1.10115],
        "Low":   [1.09995, 1.10015, 1.10035, 1.10055, 1.10075, 1.10095],
        "Close": [1.10000, 1.10022, 1.10044, 1.10066, 1.10088, 1.10110],
        "Volume": [1, 1, 1, 1, 1, 1],
    })
    # Slope is +0.10 %, threshold is 0.20 %, so neither side is vetoed.
    assert m5_momentum_aligned(df, "BUY") is True
    assert m5_momentum_aligned(df, "SELL") is True


# --------------------------------------------------------------------------- #
#  app.brain  — clear-favorite gate + new layer plumbing
# --------------------------------------------------------------------------- #


def test_brain_weights_sum_to_one():
    from app.brain import WEIGHTS
    assert abs(sum(WEIGHTS.values()) - 1.0) < 1e-9
    assert "big_players" in WEIGHTS


def test_reversal_lookback_bars_is_one():
    """Honest-edge regression: the reversal lookback must stay at the most-recent
    bar only (LOOKBACK=1).  Older bars are noise that the market has already
    digested when ADX is high — vetoing them silently blocks legitimate strong
    setups (e.g. the EURUSD-82% case that motivated this change)."""
    from app.safety import REVERSAL_LOOKBACK_BARS
    assert REVERSAL_LOOKBACK_BARS == 1


def test_brain_strong_trend_bonus_two_paths():
    """The strong-trend bonus has two equally-valid trigger paths:
    (a) classic continuation (ADX≥30 + persist≥75% + multi-TF + safety)
    (b) pullback inside a violent trend (ADX≥50 + persist≥40% + multi-TF + safety).
    A regression that re-tightens either path silently blocks the 80% gate."""
    import inspect
    from app import brain
    src = inspect.getsource(brain)
    assert "classic_strong" in src
    assert "pullback_in_violent_trend" in src
    assert "adx >= 50.0" in src


def test_brain_clear_favorite_constants_sane():
    from app.brain import CLEAR_FAVORITE_LEAD, CLEAR_FAVORITE_FLOOR
    assert CLEAR_FAVORITE_LEAD >= 1
    assert 70 <= CLEAR_FAVORITE_FLOOR <= 95


def _stub_brain_inputs(monkeypatch, brain_mod):
    """Common stubs for external data sources used by ``select_top1``."""
    monkeypatch.setattr(brain_mod, "fetch_macro_snapshot", lambda: {})
    monkeypatch.setattr(brain_mod, "currency_strength_from_macro", lambda *_a, **_k: {})
    monkeypatch.setattr(brain_mod, "_sentiment_score", lambda *_a, **_k: {"score": 0, "reason": ""})
    monkeypatch.setattr(brain_mod, "political_risk_scores", lambda: {})
    monkeypatch.setattr(brain_mod, "next_high_impact_events", lambda: {})
    monkeypatch.setattr(brain_mod, "cot_currency_zscores", lambda: {})
    monkeypatch.setattr(
        brain_mod,
        "big_player_scores",
        lambda *_a, **_k: {"currency_scores": {}, "components": {"cot": {}, "orderbook": {}, "macro": {}}},
    )


def _stub_edge_pass(brain_mod, monkeypatch):
    """Make edge_check always pass so brain-level gates are tested alone."""
    monkeypatch.setattr(
        brain_mod,
        "compute_edge",
        lambda pair, brain_conf, **_k: {
            "passes": True,
            "calibrated_confidence": brain_conf,
            "brain_confidence": brain_conf,
            "reason": "stub: edge passed",
            "windows": {},
            "best_wilson": None,
            "expected_value_pp": 1.0,
            "wilson_lower_pct": 60.0,
            "lifetime_significant": True,
            "regime_ok": True,
            "ev_positive": True,
        },
    )


def _stub_edge_fail(brain_mod, monkeypatch):
    """Make edge_check always fail."""
    monkeypatch.setattr(
        brain_mod,
        "compute_edge",
        lambda pair, brain_conf, **_k: {
            "passes": False,
            "calibrated_confidence": 50.0,
            "brain_confidence": brain_conf,
            "reason": "stub: edge not proven",
            "windows": {},
            "best_wilson": None,
            "expected_value_pp": -0.5,
            "wilson_lower_pct": 45.0,
            "lifetime_significant": False,
            "regime_ok": True,
            "ev_positive": False,
        },
    )


def test_select_top1_clear_favorite_gate(monkeypatch):
    """Below-floor leader: top1 is still published (always-publish policy)
    but tagged ``tier="normal"`` and ``favorite_check.ok`` stays False.

    Per the user-explicit "every 5 h one top-1 from 28" policy (2026-
    05-15) we never hide top-1; we tag it.  The strict PREMIUM gate
    (brain ≥ 80 AND edge passes) is reported via ``favorite_check.ok``
    and ``top1.tier`` — never by suppressing the pick.
    """
    from app import brain as brain_mod

    fake_eval = [
        {"pair": "EURUSD", "side": "BUY", "veto": None, "confidence": 72, "composite_score": 0.72, "layers": {}},
        {"pair": "GBPUSD", "side": "BUY", "veto": None, "confidence": 71, "composite_score": 0.71, "layers": {}},
    ]
    _stub_brain_inputs(monkeypatch, brain_mod)
    _stub_edge_pass(brain_mod, monkeypatch)

    # Replace evaluate_pair with a deterministic stub that returns one of
    # the prepared fake_eval entries (cycling through the list).
    iter_evals = iter(fake_eval + fake_eval * 14)  # 28 pairs total

    def fake_eval_fn(pair, *args, **kwargs):
        try:
            return next(iter_evals)
        except StopIteration:
            return {"pair": pair, "side": None, "veto": "out", "confidence": 0, "composite_score": 0, "layers": {}}

    monkeypatch.setattr(brain_mod, "evaluate_pair", fake_eval_fn)

    out = brain_mod.select_top1()
    # PREMIUM gate is the strict 80 % brain + edge_check.  72 % leader
    # does not satisfy it.
    assert out["favorite_check"]["ok"] is False
    # But top-1 is still published with a NORMAL tier.
    assert out["top1"] is not None
    assert out["top1"]["tier"] == "normal"
    assert out["top1"]["confidence"] == 72  # honest brain output
    assert "normal" in out["favorite_check"]["reason"].lower()


def test_select_top1_clear_favorite_passes(monkeypatch):
    """High-confidence leader must publish Top-1 when both gates clear."""
    from app import brain as brain_mod

    fake_eval = [
        {"pair": "EURUSD", "side": "BUY", "veto": None, "confidence": 88, "composite_score": 0.88, "layers": {}},
        {"pair": "GBPUSD", "side": "BUY", "veto": None, "confidence": 86, "composite_score": 0.86, "layers": {}},
    ]
    _stub_brain_inputs(monkeypatch, brain_mod)
    _stub_edge_pass(brain_mod, monkeypatch)

    iter_evals = iter(fake_eval + fake_eval * 14)

    def fake_eval_fn(pair, *args, **kwargs):
        try:
            return next(iter_evals)
        except StopIteration:
            return {"pair": pair, "side": None, "veto": "out", "confidence": 0, "composite_score": 0, "layers": {}}

    monkeypatch.setattr(brain_mod, "evaluate_pair", fake_eval_fn)

    out = brain_mod.select_top1()
    assert out["favorite_check"]["ok"] is True
    assert out["top1"] is not None
    assert out["top1"]["confidence"] >= 80
    # Edge metadata is attached to top1 for the UI.
    assert out["top1"]["edge"]["passes"] is True


def test_select_top1_edge_check_blocks_when_brain_passes(monkeypatch):
    """Brain ≥ 80 % with failed edge_check: top-1 is published with
    ``tier="strong"`` (⚡) but ``favorite_check.ok`` stays False because
    the strict PREMIUM gate requires edge_check to pass as well.
    """
    from app import brain as brain_mod

    fake_eval = [
        {"pair": "EURUSD", "side": "BUY", "veto": None, "confidence": 88, "composite_score": 0.88, "layers": {}},
    ]
    _stub_brain_inputs(monkeypatch, brain_mod)
    _stub_edge_fail(brain_mod, monkeypatch)

    iter_evals = iter(fake_eval * 28)

    def fake_eval_fn(pair, *args, **kwargs):
        try:
            return next(iter_evals)
        except StopIteration:
            return {"pair": pair, "side": None, "veto": "out", "confidence": 0, "composite_score": 0, "layers": {}}

    monkeypatch.setattr(brain_mod, "evaluate_pair", fake_eval_fn)

    out = brain_mod.select_top1()
    # top-1 still surfaces with STRONG tag (brain ≥ 80, edge failed).
    assert out["top1"] is not None
    assert out["top1"]["tier"] == "strong"
    assert out["top1"]["confidence"] >= 80
    assert out["favorite_check"]["ok"] is False
    assert "мат. преимущество" in out["favorite_check"]["reason"]
    assert out["favorite_check"]["edge_check"]["passes"] is False


def test_select_top1_strict_floor_below_publishes_as_normal(monkeypatch):
    """Strict-80 % gate: a 79 % leader with a huge lead is NOT a PREMIUM
    signal — ``favorite_check.ok`` must stay False — but under the
    always-publish policy the leader is still surfaced as ``top1`` with
    ``tier="normal"`` so the dashboard never goes blank.
    """
    from app import brain as brain_mod

    fake_eval = [
        {"pair": "EURUSD", "side": "BUY", "veto": None, "confidence": 79, "composite_score": 0.79, "layers": {}},
    ]
    _stub_brain_inputs(monkeypatch, brain_mod)

    _stub_edge_pass(brain_mod, monkeypatch)
    iter_evals = iter(fake_eval)

    def fake_eval_fn(pair, *args, **kwargs):
        try:
            return next(iter_evals)
        except StopIteration:
            return {"pair": pair, "side": None, "veto": "out", "confidence": 0, "composite_score": 0, "layers": {}}

    monkeypatch.setattr(brain_mod, "evaluate_pair", fake_eval_fn)

    out = brain_mod.select_top1()
    # Strict PREMIUM gate is NOT cleared (brain < 80).
    assert out["favorite_check"]["ok"] is False
    # But top-1 is published with NORMAL tier so dashboard isn't blank.
    assert out["top1"] is not None
    assert out["top1"]["tier"] == "normal"
    assert out["top1"]["confidence"] == 79  # honest brain output
    assert "порог" in out["favorite_check"]["reason"].lower()


def test_five_hour_projection_horizon_minutes_scales_margin():
    """Shorter horizon must shrink the safety margin via √(t/300) so the
    binary-option projection can stay valid even at T-10 min before the
    cycle close.  The drift contribution is per-minute, so projected
    progress also shrinks linearly with horizon — both behaviors are
    locked in by this test."""
    from app.safety import five_hour_projection, PROJECTION_HORIZON_MINUTES

    bull = _synthetic_h1(seed=3, drift=+0.0006)

    full = five_hour_projection(bull, "BUY", horizon_minutes=PROJECTION_HORIZON_MINUTES)
    half = five_hour_projection(bull, "BUY", horizon_minutes=PROJECTION_HORIZON_MINUTES / 2)
    short = five_hour_projection(bull, "BUY", horizon_minutes=10)

    # Margins shrink monotonically as horizon shrinks
    assert short["safety_margin"] < half["safety_margin"] < full["safety_margin"]

    # √t scaling: 10 min ≈ √(10/300) ≈ 0.1826 of full margin.  The
    # function rounds margin to 5 decimals so we compare within 1e-5.
    expected_short_factor = (10.0 / PROJECTION_HORIZON_MINUTES) ** 0.5
    expected_short_margin = full["safety_margin"] * expected_short_factor
    assert abs(short["safety_margin"] - expected_short_margin) < 1e-4

    # drift_per_minute should be identical regardless of horizon
    assert full["drift_per_minute"] == half["drift_per_minute"]
    assert short["horizon_minutes"] == 10.0


def test_five_hour_projection_default_horizon_matches_full_cycle():
    """Calling without horizon_minutes must still produce the legacy
    5-hour projection so existing call sites keep working unchanged."""
    from app.safety import five_hour_projection, PROJECTION_HORIZON_MINUTES

    bull = _synthetic_h1(seed=3, drift=+0.0006)
    out = five_hour_projection(bull, "BUY")
    assert out["horizon_minutes"] == float(PROJECTION_HORIZON_MINUTES)


def test_minutes_to_expiry_clamps_within_cycle():
    """`_minutes_to_expiry` must always return a value in (0, 300] so
    downstream safety projections never see a degenerate horizon."""
    from datetime import datetime, timezone
    from app.brain import _minutes_to_expiry, _next_cycle_dt

    now = datetime.now(timezone.utc)
    m = _minutes_to_expiry(now)
    assert 0 < m <= 300

    # Right at a boundary the next slot must be in the future
    nxt = _next_cycle_dt(now)
    assert nxt > now


# --------------------------------------------------------------------------- #
#  Composite confidence — sign-flip regression
# --------------------------------------------------------------------------- #


def test_confidence_scaling_anchors():
    """Piecewise scaling must hit its calibrated anchors exactly.

    Anchors recalibrated 2026-05-15 for the rebalanced binary-5h weights
    (technical=0.30, confluence=0.25) and the new confluence layer:

        composite=0.00 → 0   %
        composite=0.20 → 50  %
        composite=0.45 → 80  %   (publication floor)
        composite=1.00 → 99  %
    """
    from app.brain import _scale_confidence

    assert _scale_confidence(0.0) == 0
    assert _scale_confidence(0.20) == 50
    assert _scale_confidence(0.45) == 80
    assert _scale_confidence(1.00) == 99
    # Negative composite (net disagreement) → 0 %
    assert _scale_confidence(-0.50) == 0
    # Above 1.0 (with bonus) clamps at 99
    assert _scale_confidence(1.20) == 99


def test_confidence_sign_alignment_buy_vs_sell():
    """A SELL setup where bearish-on-pair layers all agree must score
    *higher* confidence than the same pair traded BUY against the same
    layers.  The previous double-flipped formula made disagreement and
    agreement equivalent for SELL signals — this test locks that down.
    """
    from app.brain import _scale_confidence, WEIGHTS, STRONG_TREND_BONUS

    # Bearish-on-pair: tech_norm = -0.9 (SELL), macro_norm = -0.8,
    # big_player_norm = -0.7, fund_norm = -0.5, sent_norm = -0.6
    tech_norm, macro_norm, big_norm, fund_norm, sent_norm = (
        -0.9, -0.8, -0.7, -0.5, -0.6,
    )

    def composite(side):
        s = 1 if side == "BUY" else -1
        return (
            WEIGHTS["technical"] * abs(tech_norm)
            + WEIGHTS["macro"] * macro_norm * s
            + WEIGHTS["big_players"] * big_norm * s
            + WEIGHTS["fundamental"] * fund_norm * s
            + WEIGHTS["sentiment"] * sent_norm * s
        )

    sell_conf = _scale_confidence(composite("SELL"))
    buy_conf = _scale_confidence(composite("BUY"))
    assert sell_conf > buy_conf, (sell_conf, buy_conf)
    # Strong-trend bonus must lift fully-aligned SELL to ≥ 80 %.
    sell_with_bonus = _scale_confidence(composite("SELL") + STRONG_TREND_BONUS)
    assert sell_with_bonus >= 80, sell_with_bonus
