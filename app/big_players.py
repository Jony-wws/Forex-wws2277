"""Smart Money / big-player composite layer for the AI brain.

The user explicitly asked for a system that "goes inside the market"
and knows where the big players sit — not just the chart.  We answer
that with a three-source composite per currency:

    1.  CFTC COT positioning (60 %).  Net non-commercial = literal
        institutional money on CME currency futures.
    2.  Bid/Ask imbalance from ``app.orderbook`` (20 %).  Proxies
        short-term liquidity asymmetry the technical analyser misses.
    3.  Macro flow from ``app.macro`` (20 %).  Real DXY / yield /
        commodity moves that move actual dollars between currencies.

Output per currency is a signed score in [-3, +3] interpreted as
"smart money is positioned long this currency".  The brain rescales
that to a per-pair score (base − quote) and folds it into the Top-1
composite with weight ``WEIGHTS["big_players"]`` (see ``app.brain``).

The module never raises — every input is degraded to zero on failure
so the cycle keeps publishing even when CFTC or the order book lookup
times out.
"""
from __future__ import annotations

import logging
from typing import Optional

from .cot import CURRENCIES, cot_currency_zscores, pair_cot_score
from .macro import currency_strength_from_macro

log = logging.getLogger("big_players")

# Composite weights — sum to 1.0.  COT dominates because it reflects
# the actual positions of large institutional players whereas the
# bid/ask proxy is a same-bar derivative and macro flow is a 5-bar
# % change.
WEIGHTS = {
    "cot": 0.60,
    "orderbook": 0.20,
    "macro": 0.20,
}

# Currencies → pairs whose bid/ask imbalance feeds the order-flow
# inference for the currency.  We use the seven majors-vs-USD that
# have the most retail volume on Yahoo Finance.
_FLOW_PAIRS: dict[str, tuple[str, int]] = {
    # (pair, sign).  sign = +1 means "imbalance up = currency up".
    "USD": ("EURUSD", -1),   # EURUSD up → USD down
    "EUR": ("EURUSD", +1),
    "GBP": ("GBPUSD", +1),
    "JPY": ("USDJPY", -1),   # USDJPY up → JPY down
    "CHF": ("USDCHF", -1),
    "AUD": ("AUDUSD", +1),
    "CAD": ("USDCAD", -1),
    "NZD": ("NZDUSD", +1),
}


def _orderbook_imbalance_scores() -> dict[str, float]:
    """Per-currency [-3, +3] score from synthetic Bid/Ask imbalance.

    For each major-vs-USD pair we read the order book proxy from
    ``app.orderbook.get_orderbook`` (already used in the dashboard).
    Imbalance = (total bid depth − total ask depth) / total depth, in
    [-1, +1]; we scale to ±3 and apply the per-currency sign so that
    "USD imbalance up" lines up with "USD net-long".
    """
    out: dict[str, float] = {c: 0.0 for c in CURRENCIES}
    try:
        from .orderbook import get_orderbook
    except Exception as e:
        log.warning(f"orderbook module unavailable: {e}")
        return out

    for ccy, (pair, sign) in _FLOW_PAIRS.items():
        try:
            ob = get_orderbook(pair)
        except Exception as e:
            log.warning(f"orderbook fetch failed {pair}: {e}")
            continue
        if not ob:
            continue
        # app.orderbook returns a flat ``depth`` list with side='bid'/'ask'
        # and ``volume_pct`` proxying the size of each level.
        depth = ob.get("depth", []) or []
        bid_vol = float(sum(_level_size(d) for d in depth if d.get("side") == "bid"))
        ask_vol = float(sum(_level_size(d) for d in depth if d.get("side") == "ask"))
        total = bid_vol + ask_vol
        if total <= 0:
            continue
        imbalance = (bid_vol - ask_vol) / total          # [-1, +1]
        out[ccy] = round(max(-3.0, min(3.0, sign * imbalance * 3.0)), 2)
    return out


def _level_size(level: dict) -> float:
    """Read the size proxy field from a depth level defensively.

    ``app.orderbook`` writes ``volume_pct`` for every level — fall back to
    ``size`` / ``volume`` for forward compatibility if the schema grows.
    """
    if not isinstance(level, dict):
        return 0.0
    for key in ("volume_pct", "size", "volume", "amount", "qty"):
        if key in level:
            try:
                return float(level[key])
            except (TypeError, ValueError):
                pass
    return 0.0


def big_player_scores(
    cot_scores: Optional[dict[str, float]] = None,
    macro_currency: Optional[dict[str, float]] = None,
) -> dict:
    """Return the composite Smart Money scores plus a breakdown.

    Parameters are injectable so the brain can pass in already-fetched
    snapshots (avoids hitting CFTC / Yahoo twice per cycle).  Each is
    optional — pass ``None`` to let this module fetch fresh data.
    """
    cot = cot_scores if cot_scores is not None else cot_currency_zscores()
    flow = _orderbook_imbalance_scores()
    macro = macro_currency if macro_currency is not None else {}

    composite: dict[str, float] = {}
    for ccy in CURRENCIES:
        c_score = cot.get(ccy, 0.0)
        o_score = flow.get(ccy, 0.0)
        m_score = macro.get(ccy, 0.0)
        raw = (
            WEIGHTS["cot"] * c_score
            + WEIGHTS["orderbook"] * o_score
            + WEIGHTS["macro"] * m_score
        )
        composite[ccy] = round(max(-3.0, min(3.0, raw)), 2)

    return {
        "currency_scores": composite,
        "components": {
            "cot": cot,
            "orderbook": flow,
            "macro": macro,
        },
    }


def pair_big_player_score(pair: str, currency_scores: dict[str, float]) -> dict:
    """Return a per-pair Smart Money score in [-3, +3]."""
    if len(pair) != 6:
        return {"score": 0, "reason": "Smart money: пара неизвестна"}
    base, quote = pair[:3], pair[3:]
    bs = currency_scores.get(base, 0.0)
    qs = currency_scores.get(quote, 0.0)
    diff = bs - qs
    score = int(round(max(-3, min(3, diff))))
    if score >= 2:
        verdict = f"Smart money сильно лонг {base} против {quote}"
    elif score == 1:
        verdict = f"Smart money умеренно лонг {base} против {quote}"
    elif score == 0:
        verdict = f"Smart money нейтрален по {base}/{quote}"
    elif score == -1:
        verdict = f"Smart money умеренно шорт {base} против {quote}"
    else:
        verdict = f"Smart money сильно шорт {base} против {quote}"
    return {
        "score": score,
        "base": bs,
        "quote": qs,
        "reason": f"{verdict} (base {bs:+.2f}, quote {qs:+.2f})",
    }


def fetch_big_player_snapshot(
    macro_raw: Optional[dict[str, float]] = None,
) -> dict:
    """Convenience helper: fetch every input and return the composite.

    Used by the AI brain on the slow (5-hour) cycle path.  The fast
    5-minute path can re-use cached COT scores via ``cot_currency_zscores``.
    """
    cot = cot_currency_zscores()
    macro_currency = (
        currency_strength_from_macro(macro_raw) if macro_raw is not None else {}
    )
    snap = big_player_scores(cot_scores=cot, macro_currency=macro_currency)
    snap["pair_cot_demo"] = pair_cot_score("EURUSD", cot)
    return snap
