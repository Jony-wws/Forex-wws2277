"""Ensemble voting across the five analytical layers of the cycle.

The brain already produces a single ``confidence`` number per pair, but
that number is a weighted average of seven layers — it can be dragged
down by *one* uncooperative layer even when four others scream BUY.
Ensemble voting is the dual perspective: each layer casts a vote
(``BUY``, ``SELL`` or ``ABSTAIN``) and we publish only when a weighted
*majority* of the layers agree AND the weighted-confidence is at least
``ENSEMBLE_FLOOR`` (85 %).

The five voters and their weights:

    technical    0.40
    confluence   0.25
    SMC          0.15
    safety       0.10
    edge_check   0.10
    -----------  ----
    total        1.00

Each voter exposes ``{ "side": "BUY"|"SELL"|None, "confidence": 0..100 }``
so the weighted-sum is a clean number we can compare to the floor.
"""
from __future__ import annotations

import logging
from typing import Optional

from .analyzer import analyze_pair
from .confluence import confluence_snapshot
from .edge_check import compute_edge
from .prices import fetch_bars
from .safety import five_hour_projection, reversal_risk_h1
from .smc import smc_score

log = logging.getLogger("ensemble_voting")


ENSEMBLE_WEIGHTS = {
    "technical": 0.40,
    "confluence": 0.25,
    "smc": 0.15,
    "safety": 0.10,
    "edge_check": 0.10,
}
ENSEMBLE_FLOOR = 85.0


def _technical_vote(pair: str) -> dict:
    """Voter #1 — the legacy 15-block analyser.

    Already exposes ``side`` and ``confidence`` so this is a straight
    pass-through.  Returns the abstain sentinel on any failure.
    """
    try:
        ta = analyze_pair(pair) or {}
    except Exception as e:  # noqa: BLE001
        log.debug(f"technical vote failed {pair}: {e}")
        return _abstain("technical")
    side = ta.get("side")
    conf = int(ta.get("confidence") or 0)
    return {
        "voter": "technical",
        "side": side if side in ("BUY", "SELL") else None,
        "confidence": float(conf),
        "weight": ENSEMBLE_WEIGHTS["technical"],
    }


def _confluence_vote(pair: str) -> dict:
    """Voter #2 — multi-timeframe / multi-indicator confluence."""
    try:
        snap = confluence_snapshot(pair) or {}
    except Exception as e:  # noqa: BLE001
        log.debug(f"confluence vote failed {pair}: {e}")
        return _abstain("confluence")
    side = snap.get("side")
    # ``confluence_snapshot`` does not expose a 0..100 confidence,
    # only a normalised score in [-1, +1].  Convert: |score|*100, with
    # a super_confluence bonus of +10 (cap at 100).
    raw = float(snap.get("score") or 0.0)
    conf = min(100.0, abs(raw) * 100.0 + (10.0 if snap.get("super_confluence") else 0.0))
    return {
        "voter": "confluence",
        "side": side if side in ("BUY", "SELL") else None,
        "confidence": conf,
        "weight": ENSEMBLE_WEIGHTS["confluence"],
    }


def _smc_vote(pair: str) -> dict:
    """Voter #3 — Smart Money Concepts (order blocks, FVG, BOS)."""
    try:
        smc = smc_score(pair) or {}
    except Exception as e:  # noqa: BLE001
        log.debug(f"smc vote failed {pair}: {e}")
        return _abstain("smc")
    side = smc.get("side")
    raw = float(smc.get("score") or 0.0)
    conf = min(100.0, abs(raw) * 25.0)  # SMC score is roughly ±4 in scale
    return {
        "voter": "smc",
        "side": side if side in ("BUY", "SELL") else None,
        "confidence": conf,
        "weight": ENSEMBLE_WEIGHTS["smc"],
    }


def _safety_vote(pair: str, hint_side: Optional[str]) -> dict:
    """Voter #4 — safety check (5h projection + reversal risk).

    Safety is *conditional* on a side, so we use ``hint_side`` (the
    technical layer's call) to evaluate the projection.  If safety
    passes for that side, it votes that side with 100 % confidence;
    if it fails or hits a reversal, it ABSTAINs.
    """
    if hint_side not in ("BUY", "SELL"):
        return _abstain("safety")
    try:
        bars_h1 = fetch_bars(pair, "1h", "1mo")
    except Exception as e:  # noqa: BLE001
        log.debug(f"safety vote bars failed {pair}: {e}")
        return _abstain("safety")
    if bars_h1 is None or bars_h1.empty:
        return _abstain("safety")
    try:
        projection = five_hour_projection(bars_h1, hint_side)
        reversal = reversal_risk_h1(bars_h1, hint_side)
    except Exception as e:  # noqa: BLE001
        log.debug(f"safety vote eval failed {pair}: {e}")
        return _abstain("safety")
    if not projection.get("passes") or reversal.get("reversal"):
        return _abstain("safety")
    return {
        "voter": "safety",
        "side": hint_side,
        "confidence": 100.0,
        "weight": ENSEMBLE_WEIGHTS["safety"],
    }


def _edge_check_vote(pair: str, hint_side: Optional[str]) -> dict:
    """Voter #5 — Wilson-95 % statistical-edge gate."""
    if hint_side not in ("BUY", "SELL"):
        return _abstain("edge_check")
    try:
        edge = compute_edge(pair)
    except Exception as e:  # noqa: BLE001
        log.debug(f"edge_check vote failed {pair}: {e}")
        return _abstain("edge_check")
    # compute_edge returns a dataclass-like dict.  We treat a
    # "calibrated_passes=True" gate as a unanimous vote at 100 %;
    # otherwise we abstain (rather than vote the opposite side, since
    # a failed edge check means we don't *know* there's an edge,
    # not that the opposite trade has one).
    if not isinstance(edge, dict):
        return _abstain("edge_check")
    if edge.get("calibrated_passes") is True:
        return {
            "voter": "edge_check",
            "side": hint_side,
            "confidence": 100.0,
            "weight": ENSEMBLE_WEIGHTS["edge_check"],
        }
    return _abstain("edge_check")


def _abstain(voter: str) -> dict:
    return {
        "voter": voter,
        "side": None,
        "confidence": 0.0,
        "weight": ENSEMBLE_WEIGHTS[voter],
    }


def ensemble_vote(pair: str) -> dict:
    """Run the five voters and return the aggregate decision.

    Returns:

    ```python
    {
        "side": "BUY" | "SELL" | None,
        "confidence": 0..100,            # weighted confidence on the winning side
        "passes_floor": bool,            # confidence >= ENSEMBLE_FLOOR
        "votes": [ {voter, side, confidence, weight}, ... ],
        "weighted_buy": float,           # sum of weight*confidence on BUY side
        "weighted_sell": float,          # sum of weight*confidence on SELL side
    }
    ```

    The decision rule:

    1. Tally weighted confidence per side.
    2. The winning side is whichever has the higher tally.
    3. ``confidence`` is the winning tally (already on the 0..100 scale
       because each voter is 0..100 and weights sum to 1.0).
    4. ``passes_floor`` is True only when confidence ≥ 85 %.
    """
    tech = _technical_vote(pair)
    confl = _confluence_vote(pair)
    smc = _smc_vote(pair)
    hint_side = tech["side"] if tech["side"] in ("BUY", "SELL") else confl["side"]
    safety = _safety_vote(pair, hint_side)
    edge = _edge_check_vote(pair, hint_side)
    votes = [tech, confl, smc, safety, edge]

    weighted_buy = 0.0
    weighted_sell = 0.0
    for v in votes:
        if v["side"] == "BUY":
            weighted_buy += v["weight"] * v["confidence"]
        elif v["side"] == "SELL":
            weighted_sell += v["weight"] * v["confidence"]
    if weighted_buy == 0.0 and weighted_sell == 0.0:
        side: Optional[str] = None
        conf = 0.0
    elif weighted_buy >= weighted_sell:
        side = "BUY"
        conf = weighted_buy
    else:
        side = "SELL"
        conf = weighted_sell

    return {
        "side": side,
        "confidence": round(conf, 2),
        "passes_floor": conf >= ENSEMBLE_FLOOR and side is not None,
        "votes": votes,
        "weighted_buy": round(weighted_buy, 2),
        "weighted_sell": round(weighted_sell, 2),
        "floor": ENSEMBLE_FLOOR,
    }
