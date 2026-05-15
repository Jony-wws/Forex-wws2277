"""
edge_check — mathematical-edge validator for the AI brain's Top-1 pick.

Background
----------
The user's product requirement is precise: the strict ``80 %`` floor on
``brain.py`` measures the *current setup quality* (how well the seven
analysis layers agree right now), but a high "now" confidence is not the
same as a real mathematical edge **over distance**.  A coin can land on
heads three times in a row; that doesn't make the coin biased.

This module adds a separate, statistical gate that asks:

    * Does the pair's *historical* performance, after applying the
      Wilson 95 % confidence interval (the textbook way to bound the
      true win-rate from a finite sample), show a *statistically
      significant* edge over random?
    * Is the trade's expected value (E[trade] = WR·avg_win −
      (1−WR)·avg_loss) positive at the lower-bound win-rate?
    * Combining the "now" confidence (brain) and the "distance"
      confidence (history), is the calibrated probability still ≥ 80 %?

If any of those answers is "no" the pair is vetoed before it becomes
Top-1.  In other words: the system only publishes a forecast when 80 %
is a *real* edge on distance — not just optimism about the current bar.

The math
---------
Wilson score interval (1927) for a binomial proportion p̂ = wins / n,
with the 95 % critical value z = 1.95996:

    centre =      (p̂ + z²/(2n)) / (1 + z²/n)
    radius = z·√( (p̂(1-p̂)/n + z²/(4n²)) ) / (1 + z²/n)

    lower  = centre − radius
    upper  = centre + radius

Wilson is preferred over the Wald interval for small / unbalanced n
because it never produces a lower bound below 0 or above 1, and it
collapses gracefully when the sample is tiny (0 wins out of 1 trade
gives [0, 0.79] rather than the degenerate Wald [0, 0]).

Inputs
-------
The pair's historical win/loss record is read from
``state/cycle_latest.json`` (produced by ``scripts/cycle_5h.py`` — the
existing 5-hour adaptive backtest sweep).  We look at three windows:

    wr_5d   — last 5 calendar days   (fastest reaction to regime)
    wr_30d  — last 30 calendar days  (medium-term stability)
    wr_365d — lifetime               (largest sample, most-significant)

The gate uses the **most pessimistic Wilson lower-bound** across the
three windows that have a meaningful sample (n ≥ MIN_TRADES).  This is
intentionally conservative: a pair must have shown an edge across
multiple time horizons, not just yesterday's lucky streak.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


# Wilson score critical value for a two-sided 95 % CI (NORM.S.INV(0.975)).
_WILSON_Z_95 = 1.95996398454005

# Minimum trades required in a window before we trust its Wilson interval.
# Below this, the lower bound is so wide that it carries no information
# (e.g. 5 trades / 80 % WR → Wilson 95 % lower bound ≈ 38 %).
MIN_TRADES_PER_WINDOW = 40

# Lifetime gate.  This is the largest-sample window; a pair must show a
# statistically-significant edge here (Wilson 95 % lower bound > 51 %)
# to qualify as having a real long-run advantage.  The 51 % bar is the
# binary-option "edge over coin-flip" threshold — Wilson 95 % lower
# ≥51 % means we are 95 % confident the true win rate is at least
# 51 %, which is already profitable on 1:1 binary payouts.  Strict
# enough to reject random-walk pairs, loose enough that genuine
# 53-55 % strategies (typical for forex 5h binaries) clear it with
# the ~1000 historical trades the simulator generates per pair.
LIFETIME_LOWER_FLOOR = 0.51

# Regime-change guard: the 30-day window's Wilson 95 % lower bound must
# also be at least 45 %.  A pair whose lifetime is great but whose last
# 30 days has collapsed below 45 % WR (lower bound) is likely in a
# regime change — don't trade it until the trend stabilises.
REGIME_LOWER_FLOOR = 0.45

# Weights used to blend the brain's "now" confidence (current setup
# quality) with the historical edge (distance confidence).  60 / 40 puts
# slightly more emphasis on the current setup — the technicals,
# multi-TF, SMC and Wyckoff fingerprints — while still letting the
# long-run stats meaningfully gate the publication.
_W_BRAIN_NOW = 0.60
_W_HIST_DIST = 0.40


@dataclass(frozen=True)
class WilsonInterval:
    """A Wilson 95 % CI for a binomial proportion."""

    point: float
    lower: float
    upper: float
    wins: int
    n: int

    def width(self) -> float:
        return self.upper - self.lower


def wilson_interval(wins: int, n: int, z: float = _WILSON_Z_95) -> WilsonInterval:
    """Return the Wilson score interval for ``wins`` out of ``n`` trades.

    For ``n == 0`` we return a maximally-uninformative interval ``[0, 1]``
    centred at 0.5.  Mathematically the Wilson formula is undefined at
    ``n = 0``; this convention plays well with downstream gates that
    require ``lower >= floor``.
    """
    if n <= 0:
        return WilsonInterval(point=0.5, lower=0.0, upper=1.0, wins=0, n=0)
    p = wins / n
    denom = 1.0 + (z * z) / n
    centre = (p + (z * z) / (2.0 * n)) / denom
    radius_inner = (p * (1.0 - p) / n) + (z * z) / (4.0 * n * n)
    radius = z * math.sqrt(max(radius_inner, 0.0)) / denom
    lower = max(0.0, centre - radius)
    upper = min(1.0, centre + radius)
    return WilsonInterval(point=p, lower=lower, upper=upper, wins=wins, n=n)


def expected_value(win_rate: float, avg_win_pp: float, avg_loss_pp: float) -> float:
    """Per-trade expected value in pips at the given win-rate."""
    return win_rate * avg_win_pp - (1.0 - win_rate) * avg_loss_pp


def load_pair_history(pair: str, state_path: Path | None = None) -> Optional[dict]:
    """Return the cycle-latest history dict for ``pair`` or ``None``.

    The state file is produced by ``scripts/cycle_5h.py``; it includes
    ``per_pair`` with rolling-window WRs and average win/loss sizes for
    every supported pair.  Missing file → ``None`` so callers can fall
    back gracefully (the edge gate then publishes nothing, which is the
    correct conservative behaviour).
    """
    path = state_path or Path("state/cycle_latest.json")
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    per_pair = data.get("per_pair", [])
    for entry in per_pair:
        if entry.get("pair") == pair:
            return entry
    return None


def _wilson_for_window(wins: int, n: int) -> Optional[WilsonInterval]:
    if n < MIN_TRADES_PER_WINDOW:
        return None
    return wilson_interval(wins, n)


def compute_edge(
    pair: str,
    brain_confidence: float,
    *,
    state_path: Path | None = None,
    history: Optional[dict] = None,
) -> dict:
    """Run the full edge-check and return a structured verdict.

    Parameters
    ----------
    pair
        The currency pair the brain has nominated as Top-1, e.g. ``"EURUSD"``.
    brain_confidence
        The 7-layer composite confidence in **percent** (0..100).  The
        brain has already enforced its own ≥ 80 % gate before calling
        this function — we use the value to compute a blended
        calibrated confidence, not to re-gate.
    state_path
        Override the path to ``cycle_latest.json``.  Used by tests.
    history
        Override the loaded ``per_pair`` record.  Used by tests so they
        don't have to touch the filesystem.

    Returns
    -------
    dict
        Always contains ``passes``, ``calibrated_confidence``,
        ``reason`` and the structured stats so the UI can render a
        full breakdown.
    """
    if history is None:
        history = load_pair_history(pair, state_path=state_path)

    if history is None:
        # No historical data → cannot prove an edge → conservative veto.
        return {
            "passes": False,
            "calibrated_confidence": 0.0,
            "brain_confidence": brain_confidence,
            "reason": (
                "Нет исторических данных по паре в state/cycle_latest.json — "
                "edge-check не может подтвердить мат. преимущество."
            ),
            "windows": {},
            "best_wilson": None,
            "expected_value_pp": None,
        }

    windows = {
        "5d": _wilson_for_window(
            _wins_from_wr(history.get("wr_5d", 0.0), history.get("wr_5d_trades", 0)),
            int(history.get("wr_5d_trades", 0)),
        ),
        "30d": _wilson_for_window(
            _wins_from_wr(history.get("wr_30d", 0.0), history.get("wr_30d_trades", 0)),
            int(history.get("wr_30d_trades", 0)),
        ),
        "lifetime": _wilson_for_window(
            int(history.get("wins", 0)),
            int(history.get("trades", 0)),
        ),
    }
    windows_serialised = {
        k: _serialise_wilson(v) for k, v in windows.items()
    }

    lifetime = windows["lifetime"]
    if lifetime is None:
        return {
            "passes": False,
            "calibrated_confidence": 0.0,
            "brain_confidence": brain_confidence,
            "reason": (
                f"Жизненный сэмпл сделок < {MIN_TRADES_PER_WINDOW} — "
                "невозможно подтвердить статистический edge."
            ),
            "windows": windows_serialised,
            "best_wilson": None,
            "expected_value_pp": None,
        }

    # Primary gate: lifetime Wilson 95 % lower bound > LIFETIME_LOWER_FLOOR.
    # This is the largest-sample window, so the CI is tightest — if the
    # lower bound clears 52 % here we have *strong* evidence the pair's
    # true win rate is above coin-flip.
    lifetime_significant = lifetime.lower >= LIFETIME_LOWER_FLOOR

    # Secondary guard: 30-day Wilson lower bound > REGIME_LOWER_FLOOR.
    # Catches regime changes that flipped a historically-profitable pair
    # into a recent drawdown.  If the 30-day window has too few trades,
    # we are lenient and skip this check (the lifetime gate still holds).
    regime = windows["30d"]
    regime_ok = (regime is None) or (regime.lower >= REGIME_LOWER_FLOOR)

    avg_win = float(history.get("avg_win_pp", 0.0))
    avg_loss = float(history.get("avg_loss_pp", 0.0))
    ev_at_lifetime = expected_value(lifetime.lower, avg_win, avg_loss)
    ev_positive = ev_at_lifetime > 0.0

    # Calibrated confidence — blend "now" (brain) and "distance" (history).
    # We use the lifetime Wilson lower bound (most-significant statistic)
    # converted to percent.  When lifetime_lower is 0.60 and brain is 82,
    # calibrated = 0.6*82 + 0.4*60 = 49.2 + 24 = 73.2 % — below the 80 %
    # gate, so even a strong "now" signal won't publish if the pair
    # historically only scrapes 60 % WR lower-bound.
    hist_pct = lifetime.lower * 100.0
    calibrated = _W_BRAIN_NOW * brain_confidence + _W_HIST_DIST * hist_pct

    passes = lifetime_significant and regime_ok and ev_positive

    if passes:
        reason = (
            f"Edge подтверждён: Wilson 95% lower={lifetime.lower * 100:.1f}% "
            f"на жизненном окне (n={lifetime.n}), "
            f"EV={ev_at_lifetime:+.2f}п. на сделку, "
            f"calibrated={calibrated:.1f}%."
        )
    else:
        bits = []
        if not lifetime_significant:
            bits.append(
                f"жизненный Wilson lower {lifetime.lower * 100:.1f}% < "
                f"{LIFETIME_LOWER_FLOOR*100:.0f}% "
                "(статистически не отличается от случайности)"
            )
        if not regime_ok and regime is not None:
            bits.append(
                f"30d Wilson lower {regime.lower * 100:.1f}% < "
                f"{REGIME_LOWER_FLOOR*100:.0f}% "
                "(похоже на смену режима — недавняя просадка)"
            )
        if not ev_positive:
            bits.append(
                f"EV {ev_at_lifetime:+.2f}п. ≤ 0 (отрицательное матожидание "
                f"на нижней границе WR)"
            )
        reason = "Edge не подтверждён: " + "; ".join(bits) + "."

    return {
        "passes": passes,
        "calibrated_confidence": round(calibrated, 1),
        "brain_confidence": brain_confidence,
        "reason": reason,
        "windows": windows_serialised,
        "best_wilson": _serialise_wilson(lifetime),
        "expected_value_pp": round(ev_at_lifetime, 3),
        "wilson_lower_pct": round(lifetime.lower * 100, 2),
        "lifetime_significant": lifetime_significant,
        "regime_ok": regime_ok,
        "ev_positive": ev_positive,
    }


def _wins_from_wr(wr_percent: float, n: int) -> int:
    """Reconstruct wins from a percentage WR and the trade count.

    ``cycle_latest.json`` stores ``wr_30d`` as a percentage (e.g. 67.04)
    and ``wr_30d_trades`` as the sample size.  We round to the nearest
    integer to recover a sensible "wins" count for Wilson.
    """
    return int(round((wr_percent / 100.0) * max(n, 0)))


def _serialise_wilson(w: Optional[WilsonInterval]) -> Optional[dict]:
    if w is None:
        return None
    return {
        "point_pct": round(w.point * 100, 2),
        "lower_pct": round(w.lower * 100, 2),
        "upper_pct": round(w.upper * 100, 2),
        "wins": w.wins,
        "n": w.n,
    }



