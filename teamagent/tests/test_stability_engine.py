"""Unit-тесты для stability_engine — без фейковых данных и без random.

Все тесты используют либо детерминированные входы, либо реальные state-файлы
этого репозитория. Все тесты должны проходить за <2 сек суммарно.
"""
from __future__ import annotations
import math
import unittest

from teamagent import stability_engine as se


class TestWilsonCI(unittest.TestCase):
    def test_zero_total(self):
        lo, hi = se.wilson_lower_upper(0, 0, 0.95)
        self.assertEqual((lo, hi), (0.0, 1.0))

    def test_all_wins(self):
        lo, hi = se.wilson_lower_upper(10, 10, 0.95)
        self.assertGreater(lo, 0.6)
        self.assertEqual(hi, 1.0)

    def test_50_50(self):
        lo, hi = se.wilson_lower_upper(5, 10, 0.95)
        # WR 50% on 10 trades: ~24%..76%
        self.assertLess(lo, 0.30)
        self.assertGreater(hi, 0.70)

    def test_70_pct_30_trades(self):
        lo, hi = se.wilson_lower_upper(21, 30, 0.95)
        # known interval ~52% .. 84%
        self.assertGreater(lo, 0.50)
        self.assertLess(hi, 0.90)

    def test_lower_below_upper(self):
        for w, n in [(1, 5), (3, 7), (15, 50), (70, 100)]:
            lo, hi = se.wilson_lower_upper(w, n, 0.95)
            self.assertLessEqual(lo, hi)


class TestBootstrapCI(unittest.TestCase):
    def test_empty(self):
        out = se.bootstrap_ci([], n_iter=200)
        self.assertEqual(out["n"], 0)

    def test_constant(self):
        out = se.bootstrap_ci([1.0] * 30, n_iter=500, seed=42)
        self.assertAlmostEqual(out["mean"], 1.0)
        self.assertAlmostEqual(out["lower"], 1.0, places=3)
        self.assertAlmostEqual(out["upper"], 1.0, places=3)

    def test_reproducible(self):
        a = se.bootstrap_ci([0.5, -1.0, 0.85, -1.0, 0.85], n_iter=2000, seed=123)
        b = se.bootstrap_ci([0.5, -1.0, 0.85, -1.0, 0.85], n_iter=2000, seed=123)
        self.assertEqual(a, b)

    def test_lower_le_mean_le_upper(self):
        out = se.bootstrap_ci([0.85, -1.0, 0.85, -1.0, 0.85, -1.0], n_iter=1000, seed=7)
        self.assertLessEqual(out["lower"], out["mean"])
        self.assertLessEqual(out["mean"], out["upper"])


class TestRiskMetrics(unittest.TestCase):
    def test_var_cvar(self):
        rs = [-1, -0.8, -0.5, 0, 0.5, 0.85, 0.85, 0.85, 0.85, 0.85]
        out = se.var_cvar(rs, 0.95)
        self.assertLess(out["cvar"], out["var"] + 1e-9)
        # tail (5%) of 10 obs = 1 obs = -1
        self.assertAlmostEqual(out["var"], -1.0, delta=0.5)

    def test_sharpe_constant(self):
        # zero variance -> sharpe is 0 by convention
        self.assertEqual(se.sharpe_ratio([1.0, 1.0, 1.0]), 0.0)

    def test_sortino_no_downside(self):
        self.assertEqual(se.sortino_ratio([0.1, 0.2, 0.3, 0.4]), 0.0)

    def test_max_drawdown(self):
        eq = [10, 20, 30, 25, 15, 35, 40]
        mdd = se.max_drawdown(eq)
        # 30 -> 15 = -50%
        self.assertAlmostEqual(mdd["mdd_pct"], -50.0, places=0)

    def test_profit_factor(self):
        self.assertAlmostEqual(se.profit_factor([1, 2, -1]), 3.0)
        self.assertEqual(se.profit_factor([]), 0.0)
        self.assertEqual(se.profit_factor([-1, -2]), 0.0)
        self.assertEqual(se.profit_factor([1, 2]), float("inf"))

    def test_kelly(self):
        # WR=70%, payout 0.85 → f* = (0.7*0.85 - 0.3)/0.85 ≈ 0.347; half ≈ 0.174
        f = se.kelly_fraction(0.70, 0.85, half=True)
        self.assertGreater(f, 0.10)
        self.assertLess(f, 0.20)

    def test_kelly_negative_edge_zero(self):
        # WR=50%, payout 0.85 → negative, clamped to 0
        self.assertEqual(se.kelly_fraction(0.50, 0.85, half=True), 0.0)

    def test_break_even(self):
        # payout 0.85 -> 1/(1+0.85) ≈ 0.5405
        self.assertAlmostEqual(se.break_even_probability(0.85), 0.5405, places=3)


class TestDistributionStats(unittest.TestCase):
    def test_zero_std(self):
        out = se.distribution_stats([1.0, 1.0, 1.0, 1.0])
        self.assertEqual(out["std"], 0.0)
        self.assertEqual(out["skew"], 0.0)

    def test_normal_like(self):
        # symmetric -> low skew
        rs = [-3, -2, -1, 0, 1, 2, 3]
        out = se.distribution_stats(rs)
        self.assertAlmostEqual(out["skew"], 0.0, places=2)


class TestCalibration(unittest.TestCase):
    def test_perfect_brier(self):
        # if probs match outcomes exactly -> 0
        b = se.brier_score([1.0, 0.0, 1.0, 0.0], [1, 0, 1, 0])
        self.assertEqual(b, 0.0)

    def test_worst_brier(self):
        b = se.brier_score([0.0, 1.0, 0.0, 1.0], [1, 0, 1, 0])
        self.assertEqual(b, 1.0)

    def test_log_loss_perfect(self):
        ll = se.log_loss([0.99, 0.01, 0.99, 0.01], [1, 0, 1, 0])
        self.assertLess(ll, 0.05)


class TestStreaks(unittest.TestCase):
    def test_no_trades(self):
        s = se.streak_analysis([])
        self.assertEqual(s["longest_win_streak"], 0)
        self.assertEqual(s["current_streak_kind"], "—")

    def test_alternating(self):
        trades = [
            {"close_time": "2026-01-01", "result": "WIN"},
            {"close_time": "2026-01-02", "result": "LOSS"},
            {"close_time": "2026-01-03", "result": "WIN"},
            {"close_time": "2026-01-04", "result": "WIN"},
            {"close_time": "2026-01-05", "result": "WIN"},
        ]
        s = se.streak_analysis(trades)
        self.assertEqual(s["longest_win_streak"], 3)
        self.assertEqual(s["longest_loss_streak"], 1)
        self.assertEqual(s["current_streak_kind"], "WIN")
        self.assertEqual(s["current_streak"], 3)


class TestSystemReport(unittest.TestCase):
    def test_runs_without_error(self):
        """Главный отчёт должен выполняться без exception на текущем state."""
        rep = se.system_stability_report()
        self.assertIn("stability_score_0_100", rep)
        self.assertIn("wilson_wr_lower_95", rep)
        self.assertIn("var_95", rep)
        self.assertIsNotNone(rep["as_of"])

    def test_min_guarantee_runs(self):
        mg = se.min_guarantee_per_trade(1.0, 0.85)
        self.assertIn("expected_pnl_lower_per_trade", mg)
        self.assertLessEqual(mg["expected_pnl_lower_per_trade"], mg["expected_pnl_mean_per_trade"])
        self.assertLessEqual(mg["expected_pnl_mean_per_trade"], mg["expected_pnl_upper_per_trade"])


class TestHurst(unittest.TestCase):
    def test_random_walk(self):
        # cumsum of zero-mean noise -> H around 0.5 (analytical)
        import numpy as np
        rng = np.random.default_rng(20260501)
        x = np.cumsum(rng.standard_normal(2000))
        h = se.hurst_exponent(list(x), max_lag=50)
        # rough check: should be near 0.5
        self.assertGreater(h, 0.30)
        self.assertLess(h, 0.70)


class TestVarianceRatio(unittest.TestCase):
    def test_iid_returns(self):
        import numpy as np
        rng = np.random.default_rng(20260501)
        rets = rng.standard_normal(2000) * 0.001
        vr = se.variance_ratio(list(rets), lag=5)
        # iid returns -> VR ≈ 1
        self.assertGreater(vr, 0.7)
        self.assertLess(vr, 1.3)


if __name__ == "__main__":
    unittest.main()
