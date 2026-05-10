"""SNIPER V1.0 — top-1 5-hour binary-options brain.

Implements SYSTEM_PROTOCOL: FOREX_SNIPER_V1.0 (5 rules):

  1. ZERO-OLD-INFO. Every signal is built from bars fetched in this
     process; no baked-in caches beyond the low-level fetch layer.
  2. ANTI-TRAP. Detects false breakouts, crowded retail positioning,
     and unconfirmed spikes. Rejects candidates that fail any filter.
  3. 5H CYCLE MECHANICS. Signals only fire on 5h boundaries
     (00, 05, 10, 15, 20 UTC); an ATR-based "safety cushion" blocks
     entries too close to swing S/R levels.
  4. TABLE REPORT. Every signal exports three tables — A (technical),
     B (trade params), C (risk map) — ready to render verbatim in UI.
  5. CONTINUOUS LEARNING. After every 5h expiry the result (win/loss
     + root-cause tag) is appended to state/sniper_learning.json; the
     next run reads that history and *subtracts* a penalty from
     candidates that match a known trap pattern.

The module is deliberately *separate* from `app.cycle` (the top-5
module).  This brain picks exactly ONE best pair per 5h slot, which is
the core rule of the binary-options cockpit.
"""
from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

import pandas as pd

from . import indicators
from .analyzer import analyze_pair
from .config import PAIRS, PAIR_NAMES_RU, detect_session
from .prices import fetch_bars

log = logging.getLogger("sniper")

# --------------------------------------------------------------------------
# Configuration — tuned for "institutional" 5h binary options.  All values
# are plain constants so the backtester can import them verbatim.
# --------------------------------------------------------------------------

SLOT_HOURS = 5                 # binary option expiry
SLOT_ANCHOR_UTC_HOUR = 0       # cycle starts at 00 UTC every day
MIN_CONFIDENCE = 80            # never fire below this
CUSHION_MULTIPLIER = 3.0       # distance to nearest S/R must be >= 3 x ATR
ADX_H1_MIN = 22.0              # real trend requirement
ADX_H4_MIN = 18.0
PERSISTENCE_MIN = 80.0         # at least 4 of last 5 H1 bars agreeing
TRAP_PENALTY_PER_HIT = 0.08    # multiplicative confidence haircut

# High-impact news blackout window — hardcoded because we have no live
# econ-calendar feed in the sandbox.  First Friday of each month NFP at
# 12:30 UTC, and every Wednesday 18:00 UTC for FOMC dot-plot press
# conferences. Entries within ±30 min are blocked per Rule 1.
NFP_BLACKOUT_WINDOW_MIN = 30


# --------------------------------------------------------------------------
# Public dataclasses
# --------------------------------------------------------------------------

@dataclass
class TableRow:
    """One {metric, value, note} row — renders as a table row in UI."""
    metric: str
    value: str
    note: str = ""


@dataclass
class SniperSignal:
    """The ONE top-1 pick for a 5h slot.

    ``tables`` is a dict with keys 'A' (technical slice), 'B' (trade
    params) and 'C' (risk map) per Rule 4.  Each table is a list of
    TableRow which the frontend renders as `<tr>` rows verbatim.
    """
    slot_start_utc: str
    slot_end_utc: str
    pair: str
    name_ru: str
    side: str                  # "BUY" or "SELL"
    entry_price: float
    confidence: int            # 0..100
    atr_h1: float
    strength: str
    verdict: str               # "FIRE" or "NO-TRADE"
    traps_detected: list[str]
    tables: dict = field(default_factory=dict)
    # Rule 5 — known-trap penalty already applied; surfaced for debug
    learning_adjust: float = 1.0


@dataclass
class SniperCandidate:
    """Intermediate per-pair scoring, ranked to pick top-1."""
    pair: str
    name_ru: str
    side: str | None
    raw_confidence: int
    adjusted_confidence: int
    entry_price: float
    atr_h1: float
    adx_h1: float
    adx_h4: float
    persistence_pct: float
    multi_tf_aligned: bool
    nearest_support: float | None
    nearest_resistance: float | None
    cushion_ratio: float       # (distance to opposite side) / ATR
    traps_detected: list[str]
    score: int
    max_score: int
    strength: str
    analyzer: dict             # raw analyzer output (for tables)


# --------------------------------------------------------------------------
# Rule 3 — 5h slot helpers
# --------------------------------------------------------------------------

def slot_bounds(now_utc: datetime | None = None) -> tuple[datetime, datetime]:
    """Return (slot_start, slot_end) for the 5h slot containing *now*.

    Slots are anchored at 00 UTC and run 5h wide, i.e. for any UTC time
    they are one of [00,05), [05,10), [10,15), [15,20), [20,01+1d).
    The 20→01 slot intentionally crosses midnight so we always hit a
    clean 5h width.
    """
    if now_utc is None:
        now_utc = datetime.now(timezone.utc)
    elif now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)
    else:
        now_utc = now_utc.astimezone(timezone.utc)

    midnight = now_utc.replace(hour=0, minute=0, second=0, microsecond=0)
    hour_into_day = (now_utc - midnight).total_seconds() / 3600.0
    slot_index = int(hour_into_day // SLOT_HOURS)
    slot_start = midnight + timedelta(hours=slot_index * SLOT_HOURS)
    slot_end = slot_start + timedelta(hours=SLOT_HOURS)
    return slot_start, slot_end


def seconds_to_next_slot(now_utc: datetime | None = None) -> int:
    _, end = slot_bounds(now_utc)
    if now_utc is None:
        now_utc = datetime.now(timezone.utc)
    elif now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)
    return max(0, int((end - now_utc).total_seconds()))


# --------------------------------------------------------------------------
# Rule 1 + Rule 3 — news blackout
# --------------------------------------------------------------------------

def _is_first_friday_of_month(dt: datetime) -> bool:
    if dt.weekday() != 4:  # 4 == Friday
        return False
    return dt.day <= 7


def in_news_blackout(now_utc: datetime | None = None) -> tuple[bool, str]:
    """True if we are within ±30 min of a known high-impact release.

    Currently covers: monthly US NFP (first Friday, 12:30 UTC) and the
    weekly Wednesday 18:00 UTC FOMC presser. We cannot pull a live
    calendar without network access, so this is intentionally coarse —
    add entries here as you find false positives in the trade log.
    """
    if now_utc is None:
        now_utc = datetime.now(timezone.utc)
    elif now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)

    candidates: list[tuple[datetime, str]] = []
    if _is_first_friday_of_month(now_utc):
        candidates.append((
            now_utc.replace(hour=12, minute=30, second=0, microsecond=0),
            "NFP (US Non-Farm Payrolls)",
        ))
    if now_utc.weekday() == 2:  # Wednesday
        candidates.append((
            now_utc.replace(hour=18, minute=0, second=0, microsecond=0),
            "FOMC (Fed Rate / Press Conference)",
        ))

    window = timedelta(minutes=NFP_BLACKOUT_WINDOW_MIN)
    for ts, label in candidates:
        if ts - window <= now_utc <= ts + window:
            return True, label
    return False, ""


# --------------------------------------------------------------------------
# Rule 2 — ANTI-TRAP detectors
# --------------------------------------------------------------------------

def detect_traps(
    bars_1h: pd.DataFrame,
    bars_15m: pd.DataFrame,
    ind_1h: dict,
    direction: str | None,
) -> list[str]:
    """Return human-readable labels of every trap pattern found.

    Each label maps to an ~8% confidence haircut; a candidate with many
    active traps is typically knocked below the 80% fire threshold.
    """
    traps: list[str] = []
    if direction is None:
        return traps

    # --- FALSE BREAKOUT ---
    # Last 1h bar made a new 20-bar high/low but closed back inside the
    # range AND volume (tick volume proxy) was below the 20-bar median.
    if len(bars_1h) >= 21:
        window = bars_1h.iloc[-21:-1]
        prev_high = float(window["High"].max())
        prev_low = float(window["Low"].min())
        last = bars_1h.iloc[-1]
        median_vol = float(window["Volume"].median() or 0)
        last_vol = float(last["Volume"] or 0)
        if direction == "BUY" and last["High"] > prev_high and last["Close"] <= prev_high \
                and last_vol < median_vol * 0.8:
            traps.append("Ложный пробой вверх без объёма (high > 20-bar max, close < уровня)")
        if direction == "SELL" and last["Low"] < prev_low and last["Close"] >= prev_low \
                and last_vol < median_vol * 0.8:
            traps.append("Ложный пробой вниз без объёма (low < 20-bar min, close > уровня)")

    # --- UNCONFIRMED SPIKE on 15M ---
    # Any 15m candle in the last hour with body > 2*ATR_15m and reverse
    # follow-through is classified as a liquidity sweep.
    if len(bars_15m) >= 20:
        last4 = bars_15m.tail(4)
        body = (last4["Close"] - last4["Open"]).abs()
        atr15 = float((bars_15m["High"] - bars_15m["Low"]).tail(20).mean())
        big_body_mask = body > 2 * atr15
        if big_body_mask.any():
            for ts, row in last4[big_body_mask].iterrows():
                if direction == "BUY" and row["Close"] < row["Open"]:
                    traps.append(f"Крупная медвежья свеча на 15М (body>2×ATR) в часе входа")
                    break
                if direction == "SELL" and row["Close"] > row["Open"]:
                    traps.append(f"Крупная бычья свеча на 15М (body>2×ATR) в часе входа")
                    break

    # --- EXTREME SENTIMENT PROXY ---
    # Retail sentiment feeds require a paid API we don't have.  Proxy:
    # if the last 5 hourly closes are >90% in one direction *and* RSI is
    # also extreme in that direction, retail is likely crowded.  We
    # invert per Rule 2.1 by flagging the *agreeing* side as suspect.
    if len(bars_1h) >= 5:
        last5 = bars_1h.tail(5)
        ups = int((last5["Close"] > last5["Open"]).sum())
        downs = int((last5["Close"] < last5["Open"]).sum())
        rsi = ind_1h.get("rsi14", 50)
        if direction == "BUY" and ups >= 5 and rsi >= 75:
            traps.append(f"Розница перегружена в лонг (5/5 бычьих + RSI {rsi:.0f})")
        if direction == "SELL" and downs >= 5 and rsi <= 25:
            traps.append(f"Розница перегружена в шорт (5/5 медвежьих + RSI {rsi:.0f})")

    # --- LATE-ENTRY / OVER-EXTENDED ---
    # ATR-extended: current price is > 3 ATR away from EMA50.  That is
    # mean-reversion territory, not trend continuation.  indicators.compute_all
    # does not expose ATR directly, so we compute it here from the same bars.
    ema50 = float(ind_1h.get("ema50", 0) or 0)
    close_px = float(ind_1h.get("close", 0) or 0)
    try:
        atr_series = indicators.atr(bars_1h, period=14)
        atr_val = float(atr_series.iloc[-1]) if len(atr_series) else 0.0
    except Exception:
        atr_val = 0.0
    if atr_val > 0 and ema50 > 0:
        if direction == "BUY" and (close_px - ema50) > 3.0 * atr_val:
            traps.append("Перегрев вверх: цена > EMA50 + 3·ATR (поздний вход)")
        if direction == "SELL" and (ema50 - close_px) > 3.0 * atr_val:
            traps.append("Перегрев вниз: цена < EMA50 − 3·ATR (поздний вход)")

    return traps


# --------------------------------------------------------------------------
# Support / resistance + safety cushion
# --------------------------------------------------------------------------

def find_sr_levels(bars_4h: pd.DataFrame, price: float) -> tuple[list[float], list[float]]:
    """Return (supports, resistances) from the last 60 4h bars.

    Uses the fractal-style "local extreme of 5 bars" definition, which
    is the same one our orderbook module uses for visual levels.  We
    return sorted lists, closest first.
    """
    if bars_4h is None or len(bars_4h) < 20:
        return [], []
    df = bars_4h.tail(60)
    highs = df["High"].to_numpy()
    lows = df["Low"].to_numpy()
    supports: list[float] = []
    resistances: list[float] = []
    for i in range(2, len(df) - 2):
        if lows[i] < lows[i - 1] and lows[i] < lows[i - 2] \
                and lows[i] < lows[i + 1] and lows[i] < lows[i + 2]:
            supports.append(float(lows[i]))
        if highs[i] > highs[i - 1] and highs[i] > highs[i - 2] \
                and highs[i] > highs[i + 1] and highs[i] > highs[i + 2]:
            resistances.append(float(highs[i]))

    supports = sorted({round(s, 5) for s in supports if s < price}, reverse=True)
    resistances = sorted({round(r, 5) for r in resistances if r > price})
    return supports[:5], resistances[:5]


def safety_cushion(
    price: float,
    atr: float,
    supports: list[float],
    resistances: list[float],
    direction: str,
) -> tuple[float, bool]:
    """Rule 3 — cushion must be ≥ CUSHION_MULTIPLIER × ATR.

    Returns (cushion_ratio, is_ok). For BUY the nearest obstacle is the
    closest resistance *above* price; for SELL it is the closest
    support *below*.  Missing level ⇒ cushion is treated as infinite.
    """
    if atr <= 0:
        return 0.0, False
    if direction == "BUY":
        if not resistances:
            return 99.0, True
        dist = resistances[0] - price
    else:
        if not supports:
            return 99.0, True
        dist = price - supports[0]
    if dist <= 0:
        return 0.0, False
    ratio = dist / atr
    return round(ratio, 2), ratio >= CUSHION_MULTIPLIER


# --------------------------------------------------------------------------
# Rule 5 — Learning store
# --------------------------------------------------------------------------

_LEARNING_PATH = Path(__file__).resolve().parent.parent / "state" / "sniper_learning.json"


def _load_learning() -> dict:
    try:
        return json.loads(_LEARNING_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {"trap_stats": {}, "pair_stats": {}, "history": []}


def _save_learning(data: dict) -> None:
    _LEARNING_PATH.parent.mkdir(parents=True, exist_ok=True)
    _LEARNING_PATH.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def record_result(signal_dict: dict, price_at_expiry: float) -> dict:
    """Called once a 5h expiry passes.  Appends to history + updates
    per-trap and per-pair WR. Returns the updated learning state.
    """
    data = _load_learning()
    pair = signal_dict["pair"]
    side = signal_dict["side"]
    entry = signal_dict["entry_price"]
    win = (side == "BUY" and price_at_expiry > entry) or \
          (side == "SELL" and price_at_expiry < entry)
    traps = signal_dict.get("traps_detected") or []

    hist = data.setdefault("history", [])
    hist.append({
        "slot": signal_dict.get("slot_start_utc"),
        "pair": pair, "side": side,
        "entry": entry, "exit": price_at_expiry,
        "win": win, "traps": traps,
    })
    data["history"] = hist[-2000:]

    pstats = data.setdefault("pair_stats", {}).setdefault(
        pair, {"wins": 0, "losses": 0, "last": []})
    pstats["wins" if win else "losses"] += 1
    pstats["last"] = (pstats["last"] + [1 if win else 0])[-20:]

    tstats = data.setdefault("trap_stats", {})
    for trap in traps:
        s = tstats.setdefault(trap, {"wins": 0, "losses": 0})
        s["wins" if win else "losses"] += 1

    _save_learning(data)
    return data


def _pair_learning_multiplier(pair: str, learning: dict) -> float:
    """Per-pair historical WR → confidence multiplier in [0.85, 1.10]."""
    s = learning.get("pair_stats", {}).get(pair)
    if not s:
        return 1.0
    total = s["wins"] + s["losses"]
    if total < 5:
        return 1.0
    wr = s["wins"] / total
    # WR 0.5 → 1.00x; WR 0.70 → 1.10x; WR 0.30 → 0.85x
    return max(0.85, min(1.10, 1.0 + (wr - 0.5) * 0.5))


def _trap_learning_multiplier(traps: list[str], learning: dict) -> float:
    """Each historically loss-heavy trap adds an extra haircut."""
    mult = 1.0
    tstats = learning.get("trap_stats", {})
    for trap in traps:
        s = tstats.get(trap)
        if not s:
            continue
        total = s["wins"] + s["losses"]
        if total < 3:
            continue
        wr = s["wins"] / total
        if wr < 0.45:
            mult *= 0.90
    return max(0.70, mult)


# --------------------------------------------------------------------------
# MAIN — per-pair candidate + top-1 picker
# --------------------------------------------------------------------------

def _candidate_for(pair: str) -> SniperCandidate | None:
    """Build a SniperCandidate from live bars + the shared analyser.

    We reuse analyze_pair() (a verified 15-block voter) and then layer
    on the SNIPER-specific filters: strict ADX/persistence floors, S/R
    cushion, trap detection.
    """
    analysis = analyze_pair(pair)
    if not analysis:
        return None
    side = analysis["side"]
    conf = analysis["confidence"]
    if side is None:
        return None

    bars_1h = fetch_bars(pair, "1h", "1mo")
    bars_15m = fetch_bars(pair, "15m", "5d")
    bars_4h = fetch_bars(pair, "4h", "3mo")
    if bars_1h.empty or bars_4h.empty:
        return None
    ind_1h = indicators.compute_all(bars_1h)
    if not ind_1h:
        return None

    price = float(bars_1h["Close"].iloc[-1])
    # Compute ATR(14) directly — indicators.compute_all doesn't expose it.
    try:
        atr_series = indicators.atr(bars_1h, period=14)
        atr = float(atr_series.iloc[-1]) if len(atr_series) else 0.0
    except Exception:
        atr = 0.0

    supports, resistances = find_sr_levels(bars_4h, price)
    cushion_ratio, _ = safety_cushion(price, atr, supports, resistances, side)
    traps = detect_traps(bars_1h, bars_15m, ind_1h, side)

    return SniperCandidate(
        pair=pair,
        name_ru=PAIR_NAMES_RU.get(pair, pair),
        side=side,
        raw_confidence=conf,
        adjusted_confidence=conf,
        entry_price=round(price, 5),
        atr_h1=round(atr, 5),
        adx_h1=float(analysis.get("adx_h1", 0)),
        adx_h4=float(analysis.get("adx_h4", 0)),
        persistence_pct=float(analysis.get("trend_persistence_5h", 0)),
        multi_tf_aligned=bool(analysis.get("multi_tf_aligned")),
        nearest_support=supports[0] if supports else None,
        nearest_resistance=resistances[0] if resistances else None,
        cushion_ratio=cushion_ratio,
        traps_detected=traps,
        score=int(analysis.get("score", 0)),
        max_score=int(analysis.get("max_score", 0)),
        strength=str(analysis.get("strength", "")),
        analyzer=analysis,
    )


def _rank_candidate(c: SniperCandidate, learning: dict) -> float:
    """Composite ranking score used to pick top-1.

    We do NOT simply pick the highest confidence — a 95% candidate
    with a failed cushion or multiple active traps is worse than an
    88% clean one.  The composite combines all hard filters.
    """
    # Hard-floor filters first
    if c.side is None:
        return -1.0
    if c.raw_confidence < MIN_CONFIDENCE:
        return -1.0
    if c.adx_h1 < ADX_H1_MIN or c.adx_h4 < ADX_H4_MIN:
        return -0.5
    if c.persistence_pct < PERSISTENCE_MIN:
        return -0.5
    if c.cushion_ratio < CUSHION_MULTIPLIER:
        return -0.3

    base = c.raw_confidence / 100.0
    trap_haircut = max(0.4, 1.0 - TRAP_PENALTY_PER_HIT * len(c.traps_detected))
    cushion_bonus = min(1.5, c.cushion_ratio / CUSHION_MULTIPLIER)
    tf_bonus = 1.15 if c.multi_tf_aligned else 1.0

    learning_mult = _pair_learning_multiplier(c.pair, learning) \
        * _trap_learning_multiplier(c.traps_detected, learning)

    return base * trap_haircut * cushion_bonus * tf_bonus * learning_mult


def _build_tables(c: SniperCandidate, verdict: str, reason: str) -> dict:
    """Rule 4 — three labelled tables.  All fields are already scalars
    so the frontend just renders rows without re-formatting.
    """
    a = [
        TableRow("RSI H1", f"{c.analyzer['indicators']['RSI']:.1f}",
                 "Моментум H1").__dict__,
        TableRow("MACD гистограмма",
                 f"{c.analyzer['indicators']['MACD']:.5f}").__dict__,
        TableRow("ADX H1",
                 f"{c.adx_h1:.1f}",
                 ("OK" if c.adx_h1 >= ADX_H1_MIN
                  else f"ниже порога {ADX_H1_MIN}")).__dict__,
        TableRow("ADX H4",
                 f"{c.adx_h4:.1f}",
                 ("OK" if c.adx_h4 >= ADX_H4_MIN
                  else f"ниже порога {ADX_H4_MIN}")).__dict__,
        TableRow("Тренд 5ч (H1 бары)",
                 f"{int(c.persistence_pct)}%",
                 ("устойчивый" if c.persistence_pct >= PERSISTENCE_MIN
                  else "нестабильный")).__dict__,
        TableRow("Мульти-ТФ",
                 "D1+H4+H1+M15" if c.multi_tf_aligned else "частично",
                 ("все ТФ в направлении" if c.multi_tf_aligned
                  else "разнонаправленные")).__dict__,
        TableRow("Ближ. поддержка",
                 f"{c.nearest_support:.5f}" if c.nearest_support else "—").__dict__,
        TableRow("Ближ. сопротивление",
                 f"{c.nearest_resistance:.5f}" if c.nearest_resistance else "—").__dict__,
        TableRow("ATR H1", f"{c.atr_h1:.5f}").__dict__,
    ]
    b = [
        TableRow("Пара", c.pair, c.name_ru).__dict__,
        TableRow("Направление", c.side,
                 "BUY = цена выше текущей через 5ч" if c.side == "BUY"
                 else "SELL = цена ниже текущей через 5ч").__dict__,
        TableRow("Точка входа (текущая цена)", f"{c.entry_price:.5f}").__dict__,
        TableRow("Экспирация", "5 часов",
                 "бинарный опцион, строго 5h").__dict__,
        TableRow("Уверенность сигнала", f"{c.adjusted_confidence}%",
                 c.strength).__dict__,
        TableRow("Score", f"{c.score}/{c.max_score}",
                 "внутренняя метрика голосования").__dict__,
        TableRow("Подушка безопасности",
                 f"{c.cushion_ratio:.2f}× ATR",
                 ("OK (≥3)" if c.cushion_ratio >= CUSHION_MULTIPLIER
                  else "НЕ ПРОЙДЕНА")).__dict__,
        TableRow("Вердикт", verdict, reason).__dict__,
    ]
    c_rows = [
        TableRow("Ловушки обнаружены",
                 str(len(c.traps_detected)),
                 "применён haircut уверенности" if c.traps_detected else "ни одной").__dict__,
    ]
    for i, trap in enumerate(c.traps_detected, 1):
        c_rows.append(TableRow(f"Ловушка #{i}", trap,
                               "штраф −8% уверенности").__dict__)
    return {"A": a, "B": b, "C": c_rows}


def pick_top1(
    pairs: Iterable[str] = PAIRS,
    now_utc: datetime | None = None,
    learning: dict | None = None,
) -> SniperSignal:
    """Run the full 28-pair sweep and return the ONE top pick.

    When no pair passes the hard floors, we still return a signal with
    verdict = "NO-TRADE" + the best rejected candidate, so the UI can
    explain *why* we skipped this 5h window.
    """
    slot_start, slot_end = slot_bounds(now_utc)
    blackout, news = in_news_blackout(now_utc)

    if learning is None:
        learning = _load_learning()

    # ----- news blackout short-circuit -----
    if blackout:
        return SniperSignal(
            slot_start_utc=slot_start.strftime("%Y-%m-%d %H:%M UTC"),
            slot_end_utc=slot_end.strftime("%Y-%m-%d %H:%M UTC"),
            pair="—", name_ru="—", side="—", entry_price=0.0,
            confidence=0, atr_h1=0.0, strength="",
            verdict="NO-TRADE",
            traps_detected=[f"Новостная блокировка: {news}"],
            tables={"A": [], "B": [], "C": [
                TableRow("Причина", "Новости High Impact",
                         "вход заблокирован ±30 мин").__dict__,
                TableRow("Событие", news).__dict__,
            ]},
        )

    candidates: list[SniperCandidate] = []
    for p in pairs:
        try:
            c = _candidate_for(p)
            if c is not None:
                candidates.append(c)
        except Exception as e:
            log.warning("SNIPER candidate %s failed: %s", p, e)

    # ----- rank -----
    ranked = sorted(
        candidates,
        key=lambda c: _rank_candidate(c, learning),
        reverse=True,
    )
    if not ranked:
        return SniperSignal(
            slot_start_utc=slot_start.strftime("%Y-%m-%d %H:%M UTC"),
            slot_end_utc=slot_end.strftime("%Y-%m-%d %H:%M UTC"),
            pair="—", name_ru="—", side="—", entry_price=0.0,
            confidence=0, atr_h1=0.0, strength="",
            verdict="NO-TRADE",
            traps_detected=["Нет валидных данных по 28 парам"],
            tables={"A": [], "B": [], "C": []},
        )

    best = ranked[0]

    # Apply learning multipliers to the final confidence shown.
    adj = best.raw_confidence \
        * _pair_learning_multiplier(best.pair, learning) \
        * _trap_learning_multiplier(best.traps_detected, learning) \
        * max(0.6, 1.0 - TRAP_PENALTY_PER_HIT * len(best.traps_detected))
    best.adjusted_confidence = int(round(max(0, min(100, adj))))

    # Hard-floor check — if the winner still fails any floor, call it
    # NO-TRADE and explain why.
    fail_reason: list[str] = []
    if best.adjusted_confidence < MIN_CONFIDENCE:
        fail_reason.append(
            f"Уверенность после штрафов {best.adjusted_confidence}% "
            f"< порога {MIN_CONFIDENCE}%")
    if best.adx_h1 < ADX_H1_MIN:
        fail_reason.append(f"ADX H1 {best.adx_h1:.0f} < {ADX_H1_MIN:.0f}")
    if best.adx_h4 < ADX_H4_MIN:
        fail_reason.append(f"ADX H4 {best.adx_h4:.0f} < {ADX_H4_MIN:.0f}")
    if best.persistence_pct < PERSISTENCE_MIN:
        fail_reason.append(
            f"Тренд 5ч {best.persistence_pct:.0f}% < {PERSISTENCE_MIN:.0f}%")
    if best.cushion_ratio < CUSHION_MULTIPLIER:
        fail_reason.append(
            f"Подушка {best.cushion_ratio:.2f}× < требуемые {CUSHION_MULTIPLIER}×")

    verdict = "FIRE" if not fail_reason else "NO-TRADE"
    reason = "Все фильтры пройдены" if not fail_reason else "; ".join(fail_reason)

    tables = _build_tables(best, verdict, reason)

    return SniperSignal(
        slot_start_utc=slot_start.strftime("%Y-%m-%d %H:%M UTC"),
        slot_end_utc=slot_end.strftime("%Y-%m-%d %H:%M UTC"),
        pair=best.pair,
        name_ru=best.name_ru,
        side=best.side if verdict == "FIRE" else "—",
        entry_price=best.entry_price,
        confidence=best.adjusted_confidence,
        atr_h1=best.atr_h1,
        strength=best.strength,
        verdict=verdict,
        traps_detected=best.traps_detected,
        tables=tables,
        learning_adjust=round(adj / max(1, best.raw_confidence), 3),
    )


def signal_to_dict(sig: SniperSignal) -> dict:
    """Serialise for JSON output (with session label and slot hours)."""
    d = asdict(sig)
    d["session"] = detect_session()
    d["slot_hours"] = SLOT_HOURS
    return d
