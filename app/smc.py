"""Smart Money Concepts (SMC) primitives for the AI brain.

Implements the institutional-style structure analysis that pure
indicator stacks miss:

- Swing high/low pivots (fractal-style)
- Break of Structure (BOS) / Change of Character (CHoCH)
- Order Blocks (last opposing candle before an impulsive move)
- Fair Value Gaps (3-bar imbalances)
- Liquidity sweeps (stop hunts above/below recent swings)

Real candle data only — feed it the dataframes that ``app.prices.fetch_bars``
returns (UTC index, OHLCV columns).  No simulator paths.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import numpy as np
import pandas as pd


Side = Literal["bull", "bear"]


@dataclass(frozen=True)
class Pivot:
    """A confirmed swing high / swing low pivot."""

    index: int
    price: float
    kind: Side  # "bull" = swing high (LH/HH), "bear" = swing low (LL/HL)


@dataclass(frozen=True)
class OrderBlock:
    high: float
    low: float
    side: Side          # bull OB = last down candle before up move
    index: int          # bar index where the OB candle sits
    impulse_bars: int   # how many strong bars confirmed the OB


@dataclass(frozen=True)
class FairValueGap:
    top: float
    bottom: float
    side: Side          # bull FVG = bullish imbalance (low_3 > high_1)
    index: int          # index of the middle bar


def _pivots(df: pd.DataFrame, left: int = 3, right: int = 3) -> list[Pivot]:
    """Fractal swing pivots: ``left`` bars before and ``right`` bars after.

    A bar is a swing high if its High is strictly greater than every High in
    the ``left+right`` window around it (and similarly for swing lows).  This
    is the same logic Pine's ``ta.pivothigh`` uses on TradingView, so the
    output matches what a trader sees on their chart.
    """
    if df is None or df.empty or len(df) < left + right + 1:
        return []
    highs = df["High"].to_numpy()
    lows = df["Low"].to_numpy()
    out: list[Pivot] = []
    for i in range(left, len(df) - right):
        win_h = highs[i - left : i + right + 1]
        win_l = lows[i - left : i + right + 1]
        if highs[i] == win_h.max() and (win_h == highs[i]).sum() == 1:
            out.append(Pivot(index=i, price=float(highs[i]), kind="bull"))
        elif lows[i] == win_l.min() and (win_l == lows[i]).sum() == 1:
            out.append(Pivot(index=i, price=float(lows[i]), kind="bear"))
    return out


def market_structure(df: pd.DataFrame, left: int = 3, right: int = 3) -> dict:
    """Detect the most recent BOS (Break of Structure) or CHoCH.

    BOS in an uptrend = price breaks the latest swing high → trend continues.
    CHoCH in an uptrend = price breaks the latest swing low → trend reversal.
    Symmetric in a downtrend.
    """
    pivots = _pivots(df, left, right)
    if len(pivots) < 4:
        return {"event": "none", "score": 0, "last_high": None, "last_low": None}

    highs = [p for p in pivots if p.kind == "bull"]
    lows = [p for p in pivots if p.kind == "bear"]
    if len(highs) < 2 or len(lows) < 2:
        return {"event": "none", "score": 0, "last_high": None, "last_low": None}

    close_now = float(df["Close"].iloc[-1])
    last_high = highs[-1]
    prev_high = highs[-2]
    last_low = lows[-1]
    prev_low = lows[-2]

    bull_structure = last_high.price > prev_high.price and last_low.price > prev_low.price
    bear_structure = last_high.price < prev_high.price and last_low.price < prev_low.price

    event = "none"
    score = 0

    if close_now > last_high.price:
        if bull_structure:
            event, score = "bos_up", +3
        else:
            event, score = "choch_up", +2
    elif close_now < last_low.price:
        if bear_structure:
            event, score = "bos_down", -3
        else:
            event, score = "choch_down", -2

    return {
        "event": event,
        "score": score,
        "last_high": last_high.price,
        "last_low": last_low.price,
        "trend": "up" if bull_structure else "down" if bear_structure else "range",
    }


def detect_order_blocks(
    df: pd.DataFrame,
    *,
    impulse_threshold_atr: float = 1.2,
    max_blocks: int = 5,
) -> list[OrderBlock]:
    """Find the most recent valid order blocks on the dataframe.

    Order block = last *opposing* candle before an impulsive move.  We
    measure "impulsive" as a Close-to-Close move bigger than
    ``impulse_threshold_atr`` × ATR(14).  Returns the freshest first.
    """
    if df is None or len(df) < 30:
        return []
    closes = df["Close"].to_numpy()
    opens = df["Open"].to_numpy()
    highs = df["High"].to_numpy()
    lows = df["Low"].to_numpy()

    # ATR(14) reused locally so we don't have to import the indicators module.
    tr = np.maximum.reduce(
        [
            highs[1:] - lows[1:],
            np.abs(highs[1:] - closes[:-1]),
            np.abs(lows[1:] - closes[:-1]),
        ]
    )
    atr14 = pd.Series(tr).rolling(14, min_periods=14).mean().to_numpy()

    blocks: list[OrderBlock] = []
    for i in range(15, len(df) - 1):
        # Look at the 3 bars following candle i for impulse confirmation.
        forward = closes[i + 1 : i + 4]
        if len(forward) < 3 or np.isnan(atr14[i - 1]):
            continue
        move = closes[i + 3 - 1] - closes[i] if len(forward) >= 3 else 0.0
        if abs(move) < impulse_threshold_atr * atr14[i - 1]:
            continue

        is_down_candle = closes[i] < opens[i]
        is_up_candle = closes[i] > opens[i]

        if move > 0 and is_down_candle:
            blocks.append(
                OrderBlock(
                    high=float(highs[i]),
                    low=float(lows[i]),
                    side="bull",
                    index=i,
                    impulse_bars=3,
                )
            )
        elif move < 0 and is_up_candle:
            blocks.append(
                OrderBlock(
                    high=float(highs[i]),
                    low=float(lows[i]),
                    side="bear",
                    index=i,
                    impulse_bars=3,
                )
            )
    blocks.sort(key=lambda b: -b.index)
    return blocks[:max_blocks]


def detect_fvgs(df: pd.DataFrame, *, max_gaps: int = 5) -> list[FairValueGap]:
    """3-bar fair value gaps.

    A bullish FVG forms when the low of bar 3 is strictly above the high of
    bar 1 (price ran without filling the imbalance).  Bearish is symmetric.
    """
    if df is None or len(df) < 4:
        return []
    highs = df["High"].to_numpy()
    lows = df["Low"].to_numpy()
    out: list[FairValueGap] = []
    for i in range(1, len(df) - 1):
        if lows[i + 1] > highs[i - 1]:
            out.append(
                FairValueGap(
                    top=float(lows[i + 1]),
                    bottom=float(highs[i - 1]),
                    side="bull",
                    index=i,
                )
            )
        elif highs[i + 1] < lows[i - 1]:
            out.append(
                FairValueGap(
                    top=float(lows[i - 1]),
                    bottom=float(highs[i + 1]),
                    side="bear",
                    index=i,
                )
            )
    out.sort(key=lambda g: -g.index)
    return out[:max_gaps]


def liquidity_sweep(df: pd.DataFrame, lookback: int = 20) -> dict:
    """Detect a recent stop hunt above/below the prior swing.

    Returns +1 if the last bar wicked above the prior ``lookback`` high then
    closed back below (sell-side liquidity sweep), -1 for the bullish mirror,
    0 otherwise.
    """
    if df is None or len(df) < lookback + 2:
        return {"event": "none", "score": 0}

    last = df.iloc[-1]
    prior = df.iloc[-(lookback + 1):-1]
    prior_high = float(prior["High"].max())
    prior_low = float(prior["Low"].min())

    if float(last["High"]) > prior_high and float(last["Close"]) < prior_high:
        return {"event": "sweep_high", "score": -1, "level": prior_high}
    if float(last["Low"]) < prior_low and float(last["Close"]) > prior_low:
        return {"event": "sweep_low", "score": +1, "level": prior_low}
    return {"event": "none", "score": 0}


def smc_score(df: pd.DataFrame) -> dict:
    """Combined SMC score for one timeframe.

    Positive = bullish institutional bias, negative = bearish.  The
    individual components are also returned so the UI can render an
    explainable breakdown.
    """
    if df is None or df.empty:
        return {"score": 0, "reasons": []}

    structure = market_structure(df)
    sweep = liquidity_sweep(df)
    blocks = detect_order_blocks(df)
    fvgs = detect_fvgs(df)

    score = structure["score"] + sweep["score"]
    reasons: list[str] = []

    if structure["event"].startswith("bos"):
        reasons.append(
            ("Пробой структуры вверх (BOS)" if structure["score"] > 0
             else "Пробой структуры вниз (BOS)")
        )
    elif structure["event"].startswith("choch"):
        reasons.append(
            ("Смена характера вверх (CHoCH)" if structure["score"] > 0
             else "Смена характера вниз (CHoCH)")
        )

    if sweep["event"] == "sweep_high":
        reasons.append("Снятие верхней ликвидности — давление вниз")
    elif sweep["event"] == "sweep_low":
        reasons.append("Снятие нижней ликвидности — давление вверх")

    # Active OB / FVG within ATR distance of price gets a small bias bump.
    if blocks:
        latest = blocks[0]
        score += 1 if latest.side == "bull" else -1
        reasons.append(
            f"Свежий {'бычий' if latest.side == 'bull' else 'медвежий'} order block"
        )
    if fvgs:
        latest_fvg = fvgs[0]
        score += 1 if latest_fvg.side == "bull" else -1
        reasons.append(
            f"Свежий {'бычий' if latest_fvg.side == 'bull' else 'медвежий'} FVG"
        )

    # Clamp so SMC alone never exceeds ±6 — keeps the brain's weighting honest.
    score = max(-6, min(6, score))

    return {
        "score": score,
        "structure": structure,
        "sweep": sweep,
        "order_blocks": [
            {"high": ob.high, "low": ob.low, "side": ob.side, "index": ob.index}
            for ob in blocks
        ],
        "fvgs": [
            {"top": g.top, "bottom": g.bottom, "side": g.side, "index": g.index}
            for g in fvgs
        ],
        "reasons": reasons,
    }
