"""Unit tests for ``app.edge_check`` — Wilson interval and edge gate."""

from app.edge_check import (
    LIFETIME_LOWER_FLOOR,
    MIN_TRADES_PER_WINDOW,
    REGIME_LOWER_FLOOR,
    compute_edge,
    expected_value,
    wilson_interval,
)


# ─── Wilson interval ───────────────────────────────────────────────────


def test_wilson_basic_70_100_known_value():
    """70/100 has a textbook 95 % Wilson CI of [0.601, 0.788]."""
    w = wilson_interval(70, 100)
    assert abs(w.point - 0.70) < 1e-9
    # Known Wilson 95 % bounds for 70/100 (NIST/Wikipedia reference).
    assert 0.599 < w.lower < 0.606, f"lower={w.lower}"
    assert 0.778 < w.upper < 0.784, f"upper={w.upper}"


def test_wilson_80_100():
    w = wilson_interval(80, 100)
    assert abs(w.point - 0.80) < 1e-9
    assert 0.709 < w.lower < 0.715
    assert 0.864 < w.upper < 0.870


def test_wilson_handles_zero_trades():
    """n=0 must return a maximally-uninformative interval, not crash."""
    w = wilson_interval(0, 0)
    assert w.point == 0.5
    assert w.lower == 0.0 and w.upper == 1.0
    assert w.n == 0


def test_wilson_handles_perfect_record():
    """100 / 100 — Wilson 95 % lower bound is below 1.0 (correctly)."""
    w = wilson_interval(100, 100)
    assert w.point == 1.0
    # Wilson upper bound mathematically pins to 1.0 but floats land at ~0.99999…
    assert w.upper > 0.999
    # The Wilson lower bound for 100/100 at 95 % is ~0.963 — never 1.0.
    assert 0.95 < w.lower < 1.0


def test_wilson_small_sample_wide_interval():
    """5 / 5 — even a perfect 5-trade record yields a wide CI."""
    w = wilson_interval(5, 5)
    assert w.point == 1.0
    assert w.lower < 0.7  # huge uncertainty


# ─── Expected value ────────────────────────────────────────────────────


def test_expected_value_positive_when_win_rate_dominates():
    ev = expected_value(0.60, avg_win_pp=10.0, avg_loss_pp=10.0)
    # 60 % WR with 1:1 R:R → EV per trade = 0.6*10 - 0.4*10 = +2 pp.
    assert abs(ev - 2.0) < 1e-9


def test_expected_value_negative_when_loss_dominates():
    ev = expected_value(0.45, avg_win_pp=8.0, avg_loss_pp=12.0)
    # 0.45*8 - 0.55*12 = 3.6 - 6.6 = -3.0 pp.
    assert abs(ev - (-3.0)) < 1e-9


# ─── compute_edge integration ──────────────────────────────────────────


def _hist(*, wins, trades, wr_5d, n5, wr_30d, n30, avg_win=10.0, avg_loss=10.0):
    """Build a synthetic ``per_pair`` history entry for compute_edge."""
    return {
        "wins": wins,
        "trades": trades,
        "wr_5d": wr_5d,
        "wr_5d_trades": n5,
        "wr_30d": wr_30d,
        "wr_30d_trades": n30,
        "wr_365d": 100.0 * wins / max(trades, 1),
        "wr_365d_trades": trades,
        "avg_win_pp": avg_win,
        "avg_loss_pp": avg_loss,
    }


def test_compute_edge_passes_for_strong_pair():
    """A pair with 67 % lifetime WR over 450 trades clears every gate."""
    history = _hist(
        wins=319,
        trades=450,
        wr_5d=57.14, n5=56,
        wr_30d=67.04, n30=179,
        avg_win=14.8, avg_loss=15.8,
    )
    verdict = compute_edge("AUDNZD", 85.0, history=history)
    assert verdict["passes"] is True
    assert verdict["wilson_lower_pct"] >= LIFETIME_LOWER_FLOOR * 100
    assert verdict["expected_value_pp"] > 0
    assert verdict["calibrated_confidence"] > 70
    # Calibrated blends 60 % brain (=85) + 40 % wilson_lower (~66.5) ≈ 77.6
    assert 75 < verdict["calibrated_confidence"] < 80


def test_compute_edge_blocks_coin_flip_pair():
    """A pair with 50 % WR and a vast sample is statistically random."""
    history = _hist(
        wins=1122,
        trades=2239,
        wr_5d=55.87, n5=179,
        wr_30d=49.87, n30=786,
    )
    verdict = compute_edge("EURUSD", 85.0, history=history)
    assert verdict["passes"] is False
    assert verdict["wilson_lower_pct"] < LIFETIME_LOWER_FLOOR * 100
    assert "edge не подтверждён" in verdict["reason"].lower()


def test_compute_edge_blocks_regime_change():
    """A pair with great lifetime but recent 30d collapse must be vetoed."""
    history = _hist(
        wins=600,                 # 60 % WR lifetime
        trades=1000,
        wr_5d=40.0, n5=60,
        wr_30d=40.0, n30=200,     # 30d Wilson lower will be ~33 % — below 45 %
    )
    verdict = compute_edge("EURGBP", 85.0, history=history)
    assert verdict["passes"] is False
    # Lifetime stat is fine, but the 30-day regime guard fires.
    assert verdict["regime_ok"] is False
    assert "режима" in verdict["reason"]


def test_compute_edge_returns_no_history_gracefully():
    """Missing per_pair entry must yield a clean failure verdict."""
    verdict = compute_edge("XXXYYY", 90.0, history=None)
    assert verdict["passes"] is False
    assert verdict["calibrated_confidence"] == 0.0
    assert "нет исторических данных" in verdict["reason"].lower()


def test_compute_edge_min_trades_threshold():
    """Lifetime sample below MIN_TRADES_PER_WINDOW cannot confirm an edge."""
    history = _hist(
        wins=int(0.9 * (MIN_TRADES_PER_WINDOW - 5)),
        trades=MIN_TRADES_PER_WINDOW - 5,
        wr_5d=0, n5=0,
        wr_30d=0, n30=0,
    )
    verdict = compute_edge("AAABBB", 85.0, history=history)
    assert verdict["passes"] is False


# ─── Sanity-check the floor constants stay sane ────────────────────────


def test_floors_sensible():
    assert 0.50 <= LIFETIME_LOWER_FLOOR <= 0.60
    assert 0.40 <= REGIME_LOWER_FLOOR <= 0.50
    assert REGIME_LOWER_FLOOR <= LIFETIME_LOWER_FLOOR
