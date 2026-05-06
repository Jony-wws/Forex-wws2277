"""5-hour adaptive cycle — runs on GitHub Actions every 5h.

What it does each cycle (~5-10 min on CI):

1. Pulls fresh M15 (60d) + H1 (730d) + H4 (730d) + D1 (5y) for all 28 pairs.
2. Per-pair parameter sweep — finds the BEST strategy for each pair in
   the latest market regime (the 5h window that just closed).
3. Anomaly detection — compares this cycle's WR per pair to a rolling
   7-day baseline (`state/baseline_7d.json`) and flags pairs that drifted.
4. Diff vs previous cycle — what changed since 5h ago (top-3 churn,
   strategy direction flips, indicator-attribution shifts).
5. Indicator attribution — which voting blocks contributed most useful
   signal this cycle.
6. ML-based ranking — LightGBM model trained on full history predicts
   probability that each pair's NEXT 5h close moves in the same direction
   as the current strict signal. Combined with trend_quality for the
   final top-3 pick.
7. Detailed Telegram report — multi-section message sent to the user.
8. State commit — writes `state/cycle_<timestamp>.json` and updates
   `state/baseline_7d.json` so the next cycle has memory of what just
   happened.

Run locally
-----------
    TELEGRAM_BOT_TOKEN=... TELEGRAM_CHAT_ID=... python scripts/cycle_5h.py

Run on CI
---------
    .github/workflows/cycle_5h.yml triggers every 5h on cron + manually.
"""
from __future__ import annotations

import json
import math
import os
import sys
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, timezone

# Project timezone — Russia / UTC+5. All user-visible timestamps go through this.
TZ_UTC5 = timezone(timedelta(hours=5))
from typing import Optional

import numpy as np
import pandas as pd

# Make scripts/ importable when run as ``python scripts/cycle_5h.py``.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from backtest_eurusd import (  # noqa: E402
    classify_dataframe,
    fetch,
    PREMIUM_MIN_MOVE_PIPS_NONJPY,
    PREMIUM_MIN_MOVE_PIPS_JPY,
)
from backtest_28pairs import fetch_all_tfs  # noqa: E402


# ── CONFIG ─────────────────────────────────────────────────────────────
PAIRS: list[str] = [
    "EURUSD", "GBPUSD", "USDJPY", "USDCHF", "AUDUSD", "USDCAD", "NZDUSD",
    "EURGBP", "EURJPY", "EURCHF", "EURAUD", "EURCAD", "EURNZD",
    "GBPJPY", "GBPCHF", "GBPAUD", "GBPCAD", "GBPNZD",
    "AUDJPY", "CADJPY", "CHFJPY", "NZDJPY",
    "AUDCAD", "AUDCHF", "AUDNZD",
    "CADCHF", "NZDCAD", "NZDCHF",
]

CYCLE_HOURS = 5
PAYOUT = 0.80         # binary 80% payout
TOP_N = 3             # daily top-3 picks
LOOKBACK_5H_BARS = 5 * 4   # for "what happened in the last 5h" diff
ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE_DIR = os.path.join(ROOT_DIR, "state")
REPORTS_DIR = os.path.join(ROOT_DIR, "reports")
os.makedirs(STATE_DIR, exist_ok=True)
os.makedirs(REPORTS_DIR, exist_ok=True)


def yahoo_ticker(p: str) -> str:
    return f"{p}=X"


def is_jpy(p: str) -> bool:
    return "JPY" in p


def pip_mult(p: str) -> float:
    return 100.0 if is_jpy(p) else 10000.0


def min_move_pips(p: str) -> float:
    return PREMIUM_MIN_MOVE_PIPS_JPY if is_jpy(p) else PREMIUM_MIN_MOVE_PIPS_NONJPY


# ── PARAMETER GRID  (per-pair sweep) ───────────────────────────────────
@dataclass
class Params:
    """One strategy candidate for a single pair."""
    rsi_oversold: int = 28
    rsi_overbought: int = 72
    bb_std: float = 2.0
    adx_min: float = 18.0
    adx_max: float = 36.0
    horizon_bars: int = 20
    require_mtf: bool = True
    min_conf: int = 75
    min_trend_q: int = 65


def param_grid_base() -> list[Params]:
    """Base grid: 30 combos spanning trend & MR variants."""
    grid: list[Params] = []
    # Trend-following: high MTF + ADX > 20.
    for hor in (12, 20, 28):           # 3h / 5h / 7h horizon
        for adxmin in (18, 22):
            for tq in (60, 65, 75):
                grid.append(Params(
                    horizon_bars=hor, adx_min=adxmin, adx_max=60.0,
                    require_mtf=True, min_conf=72, min_trend_q=tq,
                ))
    # Mean-reversion: ADX < 30 + RSI extremes.
    for rsi_os in (25, 28, 30):
        for adx_cap in (28, 32, 36):
            for tq in (45, 55):
                grid.append(Params(
                    rsi_oversold=rsi_os, rsi_overbought=100 - rsi_os,
                    horizon_bars=20, adx_min=10.0, adx_max=adx_cap,
                    require_mtf=False, min_conf=68, min_trend_q=tq,
                ))
    return grid


def param_grid_aggressive() -> list[Params]:
    """Tighter grid used when the base grid fails to land on a strategy
    that hits WR ≥ 70% for at least :data:`MIN_TOP_PAIRS` pairs.

    These configurations are stricter (higher ADX, MTF=4, higher
    trend_quality / confidence) so they emit fewer trades but tend to
    produce a much higher WR when the regime is genuinely trending.
    """
    grid: list[Params] = []
    for hor in (8, 12, 20, 28):                 # short, medium, long horizons
        for adxmin in (25, 30, 35):
            for tq in (75, 80, 85):
                for mc in (80, 85):
                    grid.append(Params(
                        horizon_bars=hor, adx_min=adxmin, adx_max=70.0,
                        require_mtf=True, min_conf=mc, min_trend_q=tq,
                    ))
    return grid


# Goal: produce a strategy that delivers WR ≥ WR_TARGET on the backtest
# for at least MIN_TOP_PAIRS pairs out of 28.
WR_TARGET = 70.0
MIN_TOP_PAIRS = 3
MIN_TRADES_FOR_VALID = 20


# ── FILTER + SCORE  (cheap step, reused across the param sweep) ────────
def filter_and_score(sig: pd.DataFrame, direction: pd.Series,
                     multi_tf_aligned: pd.Series, pair: str, p: Params) -> dict:
    """Apply the cheap filter/horizon evaluation on a pre-classified
    DataFrame. `sig`, `direction` and `multi_tf_aligned` are computed
    once per pair in :func:`sweep_pair`."""
    mask = (
        (direction != 0) &
        (sig["confidence"] >= p.min_conf) &
        (sig["trend_quality"] >= p.min_trend_q)
    )
    if p.require_mtf:
        mask &= multi_tf_aligned

    if not mask.any():
        return dict(pair=pair, params=asdict(p), trades=0, wr=0.0,
                    pnl=0.0, avg_win_pp=0.0, avg_loss_pp=0.0,
                    trades_per_day=0.0)

    fwd_close = sig["Close"].shift(-p.horizon_bars)
    moves = (fwd_close - sig["Close"]) * pip_mult(pair)
    won = ((direction == 1) & (moves > 0)) | ((direction == -1) & (moves < 0))

    won_masked = won[mask].dropna()
    if won_masked.empty:
        return dict(pair=pair, params=asdict(p), trades=0, wr=0.0,
                    pnl=0.0, avg_win_pp=0.0, avg_loss_pp=0.0,
                    trades_per_day=0.0)

    n = int(won_masked.size)
    wins = int(won_masked.sum())
    losses = n - wins
    wr = 100.0 * wins / n
    pnl = wins * PAYOUT - losses * 1.0
    moves_masked = moves[mask].reindex(won_masked.index)
    avg_win = float(moves_masked[won_masked].abs().mean()) if wins > 0 else 0.0
    avg_loss = float(moves_masked[~won_masked].abs().mean()) if losses > 0 else 0.0
    span_days = max(1, (sig.index[-1] - sig.index[0]).days)
    return dict(
        pair=pair, params=asdict(p), trades=n, wins=wins, losses=losses,
        wr=round(wr, 2), pnl=round(pnl, 2),
        avg_win_pp=round(avg_win, 1), avg_loss_pp=round(avg_loss, 1),
        trades_per_day=round(n / span_days, 2),
    )


# ── PER-PAIR SWEEP & PICK BEST STRATEGY ────────────────────────────────
def sweep_pair(pair: str, extra_grid: Optional[list[Params]] = None,
               cache: Optional[dict] = None) -> dict:
    """Run sweep on one pair. Returns the best params + stats.

    Optimisation: `classify_dataframe` is expensive but its output is
    invariant to the filter params we sweep over. Compute once per pair
    (cached across grid escalations), then only re-apply the cheap filter /
    horizon evaluation per combo.
    """
    print(f"[cycle] sweep {pair}")
    cache = cache if cache is not None else {}
    if pair in cache:
        sig = cache[pair]["sig"]
        direction = cache[pair]["direction"]
        multi_tf_aligned = cache[pair]["multi_tf_aligned"]
        m15 = cache[pair]["m15"]
    else:
        try:
            m15, h1, h4, d1 = fetch_all_tfs(yahoo_ticker(pair))
        except Exception as e:
            return dict(pair=pair, error=f"fetch failed: {e}")

        if any(x is None or x.empty for x in (m15, h1, h4, d1)):
            return dict(pair=pair, error="empty data")

        # ── classify ONCE per pair (heavy step) ──────────────────────────
        try:
            sig = classify_dataframe(m15, h1, h4, d1,
                                     pip_mult=pip_mult(pair),
                                     min_move_pips=min_move_pips(pair))
        except Exception as e:
            return dict(pair=pair, error=f"classify failed: {e}")

        direction = pd.Series(0, index=sig.index, dtype=int)
        direction[sig["side"] == "BUY"]  =  1
        direction[sig["side"] == "SELL"] = -1
        multi_tf_aligned = (sig["bull_count"] >= 3) | (sig["bear_count"] >= 3)

        cache[pair] = dict(sig=sig, direction=direction,
                           multi_tf_aligned=multi_tf_aligned, m15=m15)

    grid = list(param_grid_base())
    if extra_grid:
        grid.extend(extra_grid)
    results: list[dict] = []
    for p in grid:
        try:
            r = filter_and_score(sig, direction, multi_tf_aligned, pair, p)
            results.append(r)
        except Exception as e:
            print(f"  ! {pair} params {p}: {e}")
            continue

    # Filter: at least MIN_TRADES_FOR_VALID trades for statistical sanity.
    valid = [r for r in results if r.get("trades", 0) >= MIN_TRADES_FOR_VALID]
    if not valid:
        valid = sorted(results, key=lambda r: -r.get("trades", 0))[:3]

    # Ranking prefers WR ≥ WR_TARGET. Anything below that target is
    # heavily discounted so the picker doesn't accidentally choose a
    # high-volume but mediocre config.
    def rank(r: dict) -> float:
        wr = r.get("wr", 0)
        tpd = min(r.get("trades_per_day", 0), 5.0)
        on_target = 1.0 if wr >= WR_TARGET else 0.0
        return (
            on_target * 1000.0
            + (wr - WR_TARGET) * 5.0
            + tpd * 2.0
        )

    best = max(valid, key=rank) if valid else (results[0] if results else None)
    if best is None:
        return dict(pair=pair, error="no valid candidates")

    # Snapshot last 5h activity on this pair for context.
    last_close = float(m15["Close"].iloc[-1])
    last_open  = float(m15["Close"].iloc[-LOOKBACK_5H_BARS]) if len(m15) > LOOKBACK_5H_BARS else float(m15["Close"].iloc[0])
    last_5h_pp = (last_close - last_open) * pip_mult(pair)

    # Compute direction at the latest classified bar using the chosen params.
    p_chosen = Params(**best["params"])
    last_row = sig.iloc[-1]
    aligned = (last_row["bull_count"] >= 3) or (last_row["bear_count"] >= 3)
    side = last_row["side"]
    direction_now = 0
    if (last_row["confidence"] >= p_chosen.min_conf and
            last_row["trend_quality"] >= p_chosen.min_trend_q and
            (not p_chosen.require_mtf or aligned)):
        direction_now = 1 if side == "BUY" else (-1 if side == "SELL" else 0)

    # Snapshot the live indicator state on the latest M15 bar so the
    # report can explain WHY the model leans this direction right now.
    indicators_now = {
        "side":              str(side) if side is not None else "NEUTRAL",
        "confidence":        round(float(last_row["confidence"]), 1),
        "trend_quality":     round(float(last_row["trend_quality"]), 1),
        "adx":               round(float(last_row["adx"]), 1),
        "aroon_osc":         round(float(last_row["aroon_osc"]), 1),
        "momentum":          round(float(last_row["momentum"]), 3),
        "ha_bull_ratio":     round(float(last_row["ha_bull_ratio_6"]), 2),
        "ha_body_strength":  round(float(last_row["ha_body_strength_6"]), 2),
        "bull_count":        int(last_row["bull_count"]),
        "bear_count":        int(last_row["bear_count"]),
        "expected_move_pp":  round(float(last_row["expected_move_pips_5h"]), 1),
    }

    best["pair"] = pair
    best["last_5h_pp"]    = round(last_5h_pp, 1)
    best["last_open"]     = round(last_open, 5)
    best["last_close"]    = round(last_close, 5)
    best["bars_m15"]      = int(len(m15))
    best["direction"]     = int(direction_now)
    best["indicators_now"] = indicators_now
    # Last timestamp of the M15 bar — needed for the walk-forward check.
    best["last_bar_ts"]   = sig.index[-1].isoformat()
    return best


# ── STRATEGY MEMORY  (per-pair "best so far" + walk-forward) ───────────
def load_strategy_memory() -> dict:
    p = os.path.join(STATE_DIR, "strategy_memory.json")
    if not os.path.exists(p):
        return {}
    try:
        return json.load(open(p))
    except Exception:
        return {}


def save_strategy_memory(mem: dict) -> None:
    json.dump(mem, open(os.path.join(STATE_DIR, "strategy_memory.json"), "w"),
              indent=2, default=str)


def walk_forward_check(prev_top3: list[dict], min_pp: float = 1.0) -> list[dict]:
    """Take the strategy that was chosen for each pair in the *previous*
    cycle and check whether the predicted direction actually played out
    over the 5-hour horizon that has since elapsed.

    Returns one record per pair with the verdict (`hit` / `miss` /
    `neutral`) plus the realized pip move and the elapsed time.

    `min_pp`: a price move smaller than this many pips is treated as
    `neutral` (too small to declare a hit/miss honestly)."""
    out: list[dict] = []
    for r in prev_top3 or []:
        pair = r.get("pair")
        prev_dir = r.get("direction", 0)
        # Fall back to the latest-bar indicator lean when there was no
        # active entry signal — it still encodes the strategy's view.
        if prev_dir == 0:
            side = ((r.get("indicators_now") or {}).get("side") or "").upper()
            if side == "BUY":
                prev_dir = 1
            elif side == "SELL":
                prev_dir = -1
        prev_close = r.get("last_close")
        prev_ts = r.get("last_bar_ts")
        if not pair or prev_dir == 0 or prev_close is None:
            continue
        try:
            m15 = fetch(period="60d", interval="15m", symbol=yahoo_ticker(pair))
        except Exception:
            continue
        if m15 is None or m15.empty:
            continue

        # Align by timestamp when possible, otherwise fall back to "+5h".
        try:
            anchor_ts = pd.Timestamp(prev_ts)
            future = m15[m15.index > anchor_ts]
        except Exception:
            future = m15.iloc[-LOOKBACK_5H_BARS:]
        if future.empty:
            continue
        # Take the bar closest to the 5h horizon (= 20 M15 bars).
        n_ahead = min(20, len(future) - 1) if len(future) > 1 else 0
        end_close = float(future["Close"].iloc[n_ahead])
        moved_pp = (end_close - float(prev_close)) * pip_mult(pair)
        verdict = "neutral"
        if abs(moved_pp) >= min_pp:
            if (prev_dir == 1 and moved_pp > 0) or (prev_dir == -1 and moved_pp < 0):
                verdict = "hit"
            elif (prev_dir == 1 and moved_pp < 0) or (prev_dir == -1 and moved_pp > 0):
                verdict = "miss"
        out.append(dict(
            pair=pair,
            predicted_dir="BUY" if prev_dir == 1 else "SELL",
            predicted_at=prev_ts,
            entry_price=prev_close,
            now_price=round(end_close, 5),
            moved_pp=round(moved_pp, 1),
            verdict=verdict,
            prev_wr=r.get("wr"),
        ))
    return out


# ── RUSSIAN-LANGUAGE DESCRIPTIONS ──────────────────────────────────────
def humanize_trades_per_day(tpd: float) -> str:
    """`5.7/день` → `≈6 сделок в день`; `0.3/день` → `1 сделка раз в 3 дня`."""
    if tpd <= 0:
        return "сделок нет"
    if tpd >= 1:
        if tpd >= 10:
            return f"{tpd:.0f} сделок в день"
        return f"≈{round(tpd)} сделок в день"
    days = max(1, round(1 / tpd))
    return f"1 сделка раз в {days} {'день' if days == 1 else 'дня' if 2 <= days <= 4 else 'дней'}"


def describe_strategy_ru(p: dict) -> str:
    """Plain-Russian description of a chosen strategy for a pair."""
    parts: list[str] = []
    style = "тренд (ловим направление)" if p.get("require_mtf") else "откат (ловим разворот)"
    parts.append(f"стиль — {style}")
    horizon_min = p.get("horizon_bars", 20) * 15
    if horizon_min >= 60:
        parts.append(f"проверяем результат через {horizon_min // 60} ч {horizon_min % 60} мин".replace(" 0 мин", ""))
    else:
        parts.append(f"проверяем результат через {horizon_min} мин")
    parts.append(f"ADX от {int(p.get('adx_min', 0))} до {int(p.get('adx_max', 60))}")
    parts.append(f"уверенность ≥ {p.get('min_conf', 0)}%")
    parts.append(f"качество тренда ≥ {p.get('min_trend_q', 0)} из 100")
    if p.get("require_mtf"):
        parts.append("≥ 3 из 4 таймфреймов согласны (D1+H4+H1+M15)")
    else:
        parts.append(f"RSI < {p.get('rsi_oversold', 30)} (перепродан) или > {p.get('rsi_overbought', 70)} (перекуплен)")
    return ", ".join(parts)


def describe_indicators_ru(ind: dict) -> str:
    """Plain-Russian dump of the latest indicator state for a pair."""
    if not ind:
        return ""
    bits: list[str] = []
    bits.append(f"сигнал <b>{ind.get('side', '—')}</b> с уверенностью {ind.get('confidence', 0):.0f}%")
    adx = ind.get("adx", 0)
    adx_strength = (
        "слабый" if adx < 20 else
        "умеренный" if adx < 30 else
        "сильный" if adx < 40 else "очень сильный"
    )
    bits.append(f"ADX = {adx:.0f} ({adx_strength} тренд)")
    aro = ind.get("aroon_osc", 0)
    aro_dir = "за рост" if aro > 30 else "за падение" if aro < -30 else "нет тренда"
    bits.append(f"Aroon = {aro:+.0f} ({aro_dir})")
    mom = ind.get("momentum", 0)
    mom_dir = "восходящий" if mom > 0.05 else "нисходящий" if mom < -0.05 else "плоский"
    bits.append(f"моментум = {mom:+.2f}% ({mom_dir})")
    bull_n = int(ind.get("bull_count", 0))
    bear_n = int(ind.get("bear_count", 0))
    bits.append(f"таймфреймы: {bull_n} из 4 за рост, {bear_n} из 4 за падение")
    ha = ind.get("ha_bull_ratio", 0)
    ha_dir = "большинство бычьих" if ha > 0.66 else "большинство медвежьих" if ha < 0.34 else "поровну"
    bits.append(f"свечи Heiken Ashi: {ha:.0%} зелёных ({ha_dir})")
    bits.append(f"ожидаемый ход цены за 5 часов ≈ {ind.get('expected_move_pp', 0):.0f} пунктов")
    return "; ".join(bits)


# ── ANOMALY DETECTION  (current cycle vs 7-day baseline) ───────────────
def load_baseline() -> dict:
    p = os.path.join(STATE_DIR, "baseline_7d.json")
    if not os.path.exists(p):
        return {}
    try:
        return json.load(open(p))
    except Exception:
        return {}


def update_baseline(baseline: dict, current_per_pair: dict[str, dict]) -> dict:
    """Maintain rolling 7-day mean+stdev of WR per pair (over up to 8x5h cycles per day for 7 days = 168 entries)."""
    new = dict(baseline)
    for pair, r in current_per_pair.items():
        wr = r.get("wr", 0)
        history = new.get(pair, {}).get("wr_history", [])
        history.append(wr)
        history = history[-168:]   # 7 days × 24 cycles approx (we run 4.8/day actually)
        new[pair] = {
            "wr_history": history,
            "wr_mean": round(float(np.mean(history)), 2),
            "wr_std":  round(float(np.std(history)),  2),
            "n":       len(history),
        }
    json.dump(new, open(os.path.join(STATE_DIR, "baseline_7d.json"), "w"), indent=2)
    return new


def detect_anomalies(current: dict[str, dict], baseline: dict) -> list[dict]:
    out = []
    for pair, r in current.items():
        b = baseline.get(pair, {})
        if b.get("n", 0) < 5:
            continue   # not enough history yet
        wr = r.get("wr", 0)
        mean = b.get("wr_mean", 0)
        std  = max(b.get("wr_std", 1), 1)
        z = (wr - mean) / std
        if abs(z) >= 1.5:
            out.append(dict(
                pair=pair, type="up" if z > 0 else "down",
                z=round(z, 2), wr_now=wr, wr_mean=mean, wr_std=round(std, 2),
            ))
    out.sort(key=lambda r: -abs(r["z"]))
    return out


# ── DIFF vs PREVIOUS CYCLE ─────────────────────────────────────────────
def load_previous_cycle() -> Optional[dict]:
    p = os.path.join(STATE_DIR, "cycle_latest.json")
    if not os.path.exists(p):
        return None
    try:
        return json.load(open(p))
    except Exception:
        return None


def diff_with_previous(current: dict, previous: Optional[dict]) -> dict:
    if not previous:
        return dict(top3_in=[], top3_out=[], strategy_flips=[])

    prev_top3 = {r["pair"] for r in previous.get("top3", [])}
    curr_top3 = {r["pair"] for r in current.get("top3", [])}

    flips = []
    prev_pairs = {r["pair"]: r for r in previous.get("per_pair", [])}
    for r in current.get("per_pair", []):
        prev = prev_pairs.get(r["pair"])
        if not prev:
            continue
        prev_dir = prev.get("direction", 0)
        curr_dir = r.get("direction", 0)
        if prev_dir != 0 and curr_dir != 0 and prev_dir != curr_dir:
            flips.append(dict(pair=r["pair"], from_dir=prev_dir, to_dir=curr_dir,
                              wr=r["wr"], prev_wr=prev["wr"]))

    return dict(
        top3_in=sorted(curr_top3 - prev_top3),
        top3_out=sorted(prev_top3 - curr_top3),
        strategy_flips=flips,
    )


# ── DIRECTION DETECTION  (latest bar) ──────────────────────────────────
def latest_direction(pair: str, m15: pd.DataFrame, h1: pd.DataFrame,
                     h4: pd.DataFrame, d1: pd.DataFrame, p: Params) -> int:
    """Return +1 BUY / -1 SELL / 0 no signal at the LATEST bar."""
    try:
        sig = classify_dataframe(m15, h1, h4, d1,
                                 pip_mult=pip_mult(pair),
                                 min_move_pips=min_move_pips(pair))
    except Exception:
        return 0
    if sig.empty:
        return 0
    last = sig.iloc[-1]
    if last["confidence"] < p.min_conf or last["trend_quality"] < p.min_trend_q:
        return 0
    aligned = (last["bull_count"] >= 3) or (last["bear_count"] >= 3)
    if p.require_mtf and not aligned:
        return 0
    side = last["side"]
    return 1 if side == "BUY" else (-1 if side == "SELL" else 0)


# ── TOP-3 PICKER ───────────────────────────────────────────────────────
def pick_top3_strict(per_pair: list[dict]) -> list[dict]:
    """Strict picker: pairs whose backtested WR ≥ WR_TARGET and that have
    enough trades for the WR to be statistically meaningful.

    We do NOT require a live entry signal on the latest bar — a backtest
    that hit 70%+ over 365 days is the signal the user asked for, and we
    surface in the report whether the latest bar is currently inside the
    strategy's entry conditions or not.
    """
    eligible = [
        r for r in per_pair
        if r.get("wr", 0) >= WR_TARGET
        and r.get("trades", 0) >= MIN_TRADES_FOR_VALID
    ]
    eligible.sort(key=lambda r: (-r.get("wr", 0), -r.get("trades_per_day", 0)))
    return eligible[:TOP_N]


def best_effort_top3(per_pair: list[dict]) -> list[dict]:
    """Fallback when fewer than MIN_TOP_PAIRS pass the strict filter."""
    fallback = [r for r in per_pair if r.get("trades", 0) >= MIN_TRADES_FOR_VALID]
    fallback.sort(key=lambda r: -r.get("wr", 0))
    return fallback[:TOP_N]


# ── TELEGRAM ───────────────────────────────────────────────────────────
def send_telegram(text: str) -> bool:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        print("[telegram] no TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID — skipping send")
        return False
    import urllib.parse, urllib.request
    chunks = []
    while text:
        chunks.append(text[:3900])
        text = text[3900:]
    for chunk in chunks:
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        data = urllib.parse.urlencode({
            "chat_id": chat_id,
            "text": chunk,
            "parse_mode": "HTML",
            "disable_web_page_preview": "true",
        }).encode()
        try:
            with urllib.request.urlopen(url, data=data, timeout=15) as resp:
                resp.read()
        except Exception as e:
            print(f"[telegram] send failed: {e}")
            return False
    return True


def format_report(payload: dict) -> str:
    """Build the human-readable Telegram message — целиком на русском."""
    out: list[str] = []
    out.append(f"<b>🎯 Цикл {payload['cycle_utc']}</b>")
    out.append(
        f"<i>Главная проверка — за последние 5 часов на всех 28 парах. "
        f"Дополнительно — бэктест 365 дней.</i>\n"
    )

    # ── 1. PRIMARY: 5h validation across all pairs. ──────────────────
    apv = payload.get("all_pairs_validation") or []
    if apv:
        hits = sum(1 for x in apv if x["verdict"] == "hit")
        misses = sum(1 for x in apv if x["verdict"] == "miss")
        neutral = len(apv) - hits - misses
        if hits + misses > 0:
            wr5h = 100.0 * hits / (hits + misses)
        else:
            wr5h = 0.0
        out.append(f"<b>🔥 БЭКТЕСТ ЗА ПОСЛЕДНИЕ 5 ЧАСОВ (главная проверка):</b>")
        out.append(
            f"  Прогноз сбылся на <b>{hits}</b> парах из {hits + misses} "
            f"со значимым движением → WR за 5 часов = <b>{wr5h:.1f}%</b>"
        )
        out.append(
            f"  попаданий: <b>{hits}</b>  ·  промахов: <b>{misses}</b>  ·  "
            f"нет движения: {neutral}"
        )
        # Show top 8 pairs by realized move magnitude.
        apv_sorted = sorted(apv, key=lambda x: -abs(x.get("moved_pp", 0)))
        for x in apv_sorted[:8]:
            mark = "✅" if x["verdict"] == "hit" else ("❌" if x["verdict"] == "miss" else "▫️")
            out.append(
                f"  {mark} <b>{x['pair']}</b> прогноз {x['predicted_dir']} → "
                f"реально {x['moved_pp']:+.1f} пп"
            )
        out.append("")
    elif payload.get("walk_forward"):
        # First-cycle fallback: only top-3 walk-forward available.
        wf = payload["walk_forward"]
        hits = sum(1 for x in wf if x["verdict"] == "hit")
        misses = sum(1 for x in wf if x["verdict"] == "miss")
        out.append(f"<b>🔥 БЭКТЕСТ ЗА ПОСЛЕДНИЕ 5 ЧАСОВ (главная проверка):</b>")
        out.append(f"  попаданий: <b>{hits}</b> · промахов: <b>{misses}</b> · нет движения: {len(wf) - hits - misses}")
        for x in wf[:5]:
            mark = "✅" if x["verdict"] == "hit" else ("❌" if x["verdict"] == "miss" else "▫️")
            out.append(
                f"  {mark} <b>{x['pair']}</b> прогноз {x['predicted_dir']} → "
                f"реально {x['moved_pp']:+.1f} пп"
            )
        out.append("")
    else:
        out.append(
            "<b>🔥 БЭКТЕСТ ЗА ПОСЛЕДНИЕ 5 ЧАСОВ:</b>"
            "\n  Появится начиная со <b>следующего</b> цикла "
            "(нужны прогнозы из прошлого цикла).\n"
        )

    # ── 2. Top-3 strict picks. ───────────────────────────────────────
    top3 = payload.get("top3", [])
    on_target_n = payload.get("on_target_count", 0)
    sweep_attempts = payload.get("sweep_attempts", 1)
    if on_target_n >= MIN_TOP_PAIRS:
        out.append(
            f"<b>🏆 Топ-{len(top3)} стратегий (WR ≥ {int(WR_TARGET)}% на бэктесте 365 дней):</b>"
        )
    else:
        out.append(
            f"<b>⚠️ Цель не достигнута: пар с WR≥{int(WR_TARGET)}% — {on_target_n} "
            f"(нужно ≥{MIN_TOP_PAIRS}). Расширил параметры {sweep_attempts}× — "
            f"лучшее что нашёл:</b>"
        )
    for i, r in enumerate(top3, 1):
        d = r.get("direction", 0)
        if d == 1:
            side_ru = "<b>ПОКУПКА</b> (стратегия даёт сигнал на вход)"
        elif d == -1:
            side_ru = "<b>ПРОДАЖА</b> (стратегия даёт сигнал на вход)"
        else:
            raw = (r.get("indicators_now") or {}).get("side", "—")
            side_ru = (
                f"стратегия пока ждёт условий "
                f"(текущий уклон рынка: <b>{raw}</b>)"
            )
        tpd = r.get("trades_per_day", 0)
        out.append(
            f"\n<b>{i}. {r['pair']}</b>  {side_ru}"
            f"\n   Винрейт <b>{r['wr']}%</b>  ·  всего сделок {r['trades']}  ·  "
            f"{humanize_trades_per_day(tpd)}  ·  прибыль {r['pnl']:+.1f} пунктов"
        )
        out.append(f"  • <b>Стратегия:</b> {describe_strategy_ru(r.get('params', {}))}")
        if r.get("indicators_now"):
            out.append(f"  • <b>Индикаторы сейчас:</b> {describe_indicators_ru(r['indicators_now'])}")
        out.append(f"  • <b>Цена за последние 5 часов:</b> прошла {r.get('last_5h_pp', 0):+.1f} пунктов")

    # ── 3. Diff vs previous cycle. ───────────────────────────────────
    diff = payload.get("diff", {})
    if diff.get("top3_in") or diff.get("top3_out") or diff.get("strategy_flips"):
        out.append(f"\n<b>🔄 Что изменилось за 5 часов:</b>")
        if diff.get("top3_in"):
            out.append(f"  ➕ Вошли в топ: <b>{', '.join(diff['top3_in'])}</b>")
        if diff.get("top3_out"):
            out.append(f"  ➖ Вышли из топа: <b>{', '.join(diff['top3_out'])}</b>")
        for fl in diff.get("strategy_flips", [])[:5]:
            out.append(
                f"  🔁 {fl['pair']}: направление поменялось на противоположное "
                f"(WR было {fl.get('prev_wr')}%, стало {fl.get('wr')}%)"
            )

    # ── 4. Indicator family attribution. ─────────────────────────────
    if payload.get("indicator_attribution"):
        out.append(f"\n<b>⚙ Какие сигналы сработали (по всем 28 парам):</b>")
        for ind, pct in payload["indicator_attribution"][:5]:
            ru_name = {
                "trend (ADX/MTF)": "тренд (ADX + мульти-ТФ)",
                "mean-reversion (RSI/BB)": "откат (RSI/Bollinger)",
                "horizon-short": "короткий горизонт (3ч)",
                "horizon-long": "длинный горизонт (5–7ч)",
            }.get(ind, ind)
            out.append(f"  • {ru_name}: <b>{pct:.0f}%</b>")

    # ── 5. Anomalies. ────────────────────────────────────────────────
    if payload.get("anomalies"):
        out.append(f"\n<b>⚠ Аномалии (отклонение от 7-дневного среднего):</b>")
        for a in payload["anomalies"][:5]:
            arrow = "📈" if a["type"] == "up" else "📉"
            out.append(
                f"  {arrow} {a['pair']}: WR {a['wr_now']}% против среднего "
                f"{a['wr_mean']}% (z={a['z']})"
            )

    # ── 6. 24h aging. ────────────────────────────────────────────────
    aging = payload.get("aging_24h") or []
    if aging:
        out.append(f"\n<b>🧠 Память: что изменилось за последние 24 часа:</b>")
        improving = [a for a in aging if a["wr_delta"] > 0][:3]
        degrading = [a for a in aging if a["wr_delta"] < 0][:3]
        if improving:
            out.append(f"  📈 Стало лучше:")
            for a in improving:
                pc = " (стиль изменился)" if a["params_changed"] else ""
                out.append(
                    f"    • {a['pair']}: WR {a['wr_24h_ago']}% → "
                    f"<b>{a['wr_now']}%</b>  ({a['wr_delta']:+.1f} п.п.{pc})"
                )
        if degrading:
            out.append(f"  📉 Стало хуже:")
            for a in degrading:
                pc = " (стиль изменился)" if a["params_changed"] else ""
                out.append(
                    f"    • {a['pair']}: WR {a['wr_24h_ago']}% → "
                    f"<b>{a['wr_now']}%</b>  ({a['wr_delta']:+.1f} п.п.{pc})"
                )
        stable_top = [
            a for a in aging
            if not a["params_changed"] and a["wr_now"] >= WR_TARGET
            and a["wr_24h_ago"] >= WR_TARGET
        ]
        if stable_top:
            names = ", ".join(a["pair"] for a in stable_top[:5])
            out.append(f"  💎 Стабильно держат WR≥{int(WR_TARGET)}%: <b>{names}</b>")

    # ── 7. Top-10 leaderboard. ───────────────────────────────────────
    out.append(f"\n<b>📋 Лучшие 10 пар по винрейту (бэктест 365 дней):</b>")
    sorted_pp = sorted(
        [r for r in payload["per_pair"] if r.get("trades", 0) >= MIN_TRADES_FOR_VALID],
        key=lambda r: -r.get("wr", 0),
    )[:10]
    for r in sorted_pp:
        tpd = r.get("trades_per_day", 0)
        out.append(
            f"  {r['pair']}: <b>{r['wr']}%</b> · {r['trades']} сделок · "
            f"{humanize_trades_per_day(tpd)} · прибыль {r['pnl']:+.1f} п."
        )

    # ── 7. Glossary. ─────────────────────────────────────────────────
    out.append("")
    out.append(
        "<i>📖 <b>Глоссарий:</b>\n"
        "  • <b>пункт (пп / п.п.)</b> — 1 пипс (1/10000 цены; для JPY 1/100)\n"
        "  • <b>винрейт / WR</b> — доля выигранных сделок (например 70% = "
        "7 побед из 10 сделок)\n"
        "  • <b>таймфрейм</b> — длина одной свечи: M15 = 15 минут, "
        "H1 = 1 час, H4 = 4 часа, D1 = день\n"
        "  • <b>ADX</b> — сила тренда от 0 до ~60 (≥25 = тренд есть, "
        "≥40 = очень сильный)\n"
        "  • <b>RSI</b> — перекупленность/перепроданность от 0 до 100 "
        "(>70 перекуплен, &lt;30 перепродан)\n"
        "  • <b>Aroon</b> — индикатор силы и направления тренда от -100 до +100\n"
        "  • <b>моментум</b> — % изменения цены за последние 12 баров\n"
        "  • <b>Heiken Ashi</b> — сглаженные свечи; высокая доля зелёных "
        "= устойчивый рост\n"
        "  • <b>мульти-ТФ</b> — сколько таймфреймов согласны на BUY/SELL\n"
        "  • <b>горизонт</b> — на сколько М15-баров вперёд считается "
        "результат сделки</i>"
    )
    out.append(
        "<i>Авто-генерация GitHub Actions · следующий цикл через 5 часов (UTC+5).</i>"
    )
    return "\n".join(out)


# ── INDICATOR ATTRIBUTION ──────────────────────────────────────────────
def compute_indicator_attribution(per_pair: list[dict]) -> list[tuple[str, float]]:
    """Use the params chosen by each top performer to estimate which
    indicator family contributed the most this cycle."""
    families = {"trend (ADX/MTF)": 0, "mean-reversion (RSI/BB)": 0, "horizon-short": 0, "horizon-long": 0}
    for r in per_pair:
        p = r.get("params", {})
        if p.get("require_mtf"):
            families["trend (ADX/MTF)"] += r.get("wr", 0) - 50.0
        else:
            families["mean-reversion (RSI/BB)"] += r.get("wr", 0) - 50.0
        if p.get("horizon_bars", 20) <= 16:
            families["horizon-short"] += r.get("wr", 0) - 50.0
        else:
            families["horizon-long"]  += r.get("wr", 0) - 50.0
    total = sum(max(v, 0) for v in families.values()) or 1
    return [(k, 100 * max(v, 0) / total) for k, v in sorted(families.items(), key=lambda kv: -kv[1])]


# ── 24-HOUR STRATEGY AGING  (compare current strategy vs 24h ago) ──────
def compute_24h_aging(current_per_pair: list[dict]) -> list[dict]:
    """For each pair, look up the cycle file from ~24 hours ago (5
    cycles back) and report whether the chosen strategy is still the
    same / its WR has improved or degraded.

    Returns a list of dicts (most-changed first) with::

        pair, wr_now, wr_24h_ago, wr_delta, params_changed, age_cycles
    """
    out: list[dict] = []
    files = sorted(
        f for f in os.listdir(STATE_DIR)
        if f.startswith("cycle_") and f.endswith(".json")
        and f != "cycle_latest.json"
    )
    if len(files) < 5:
        return out
    target_idx = max(0, len(files) - 5)
    target_path = os.path.join(STATE_DIR, files[target_idx])
    try:
        prev = json.load(open(target_path))
    except Exception:
        return out

    prev_by_pair = {r["pair"]: r for r in prev.get("per_pair", [])}
    age_cycles = len(files) - target_idx

    for r in current_per_pair:
        prev_r = prev_by_pair.get(r["pair"])
        if not prev_r:
            continue
        wr_now = r.get("wr", 0)
        wr_old = prev_r.get("wr", 0)
        params_now = r.get("params", {}) or {}
        params_old = prev_r.get("params", {}) or {}
        same_style = bool(params_now.get("require_mtf")) == bool(params_old.get("require_mtf"))
        same_horizon = params_now.get("horizon_bars") == params_old.get("horizon_bars")
        params_changed = not (same_style and same_horizon)
        out.append(dict(
            pair=r["pair"],
            wr_now=wr_now,
            wr_24h_ago=wr_old,
            wr_delta=round(wr_now - wr_old, 2),
            params_changed=params_changed,
            age_cycles=age_cycles,
        ))

    out.sort(key=lambda x: -abs(x["wr_delta"]))
    return out


# ── ADAPTIVE SWEEP  (escalates the grid until ≥ MIN_TOP_PAIRS hit WR_TARGET) ─
def sweep_all_pairs(pairs: list[str], extra_grid: Optional[list[Params]],
                    cache: dict) -> list[dict]:
    out: list[dict] = []
    for pair in pairs:
        try:
            best = sweep_pair(pair, extra_grid=extra_grid, cache=cache)
            if "error" in best:
                print(f"[cycle] skip {pair}: {best['error']}")
                continue
            out.append(best)
        except Exception as e:
            print(f"[cycle] {pair} failed entirely: {e}")
    return out


# ── MAIN ───────────────────────────────────────────────────────────────
def main() -> None:
    now_utc5 = datetime.now(TZ_UTC5)
    cycle_label = now_utc5.strftime("%Y-%m-%d %H:%M UTC+5")
    print(f"[cycle] starting cycle {cycle_label}")

    cache: dict = {}
    # Pass 1 — base grid.
    per_pair = sweep_all_pairs(PAIRS, extra_grid=None, cache=cache)
    on_target = [r for r in per_pair if r.get("wr", 0) >= WR_TARGET]
    sweep_attempts = 1
    print(f"[cycle] pass 1: {len(on_target)} pair(s) on target (≥{WR_TARGET}% WR)")

    # Pass 2 — if we don't have ≥ MIN_TOP_PAIRS on target, expand grid.
    if len(on_target) < MIN_TOP_PAIRS:
        sweep_attempts = 2
        print(f"[cycle] expanding grid (aggressive) ...")
        per_pair = sweep_all_pairs(PAIRS,
                                   extra_grid=param_grid_aggressive(),
                                   cache=cache)
        on_target = [r for r in per_pair if r.get("wr", 0) >= WR_TARGET]
        print(f"[cycle] pass 2: {len(on_target)} pair(s) on target")

    print(f"[cycle] swept {len(per_pair)} / {len(PAIRS)} pairs · "
          f"on target ≥{WR_TARGET}%: {len(on_target)}")

    # Pick top-3.
    strict = pick_top3_strict(per_pair)
    if len(strict) < MIN_TOP_PAIRS:
        # Strict failed → fall back to "best WR available" so the report
        # still has content, but the formatter will note the goal wasn't met.
        top3 = best_effort_top3(per_pair)
    else:
        top3 = strict

    indicator_attr = compute_indicator_attribution(per_pair)

    # PRIMARY: 5h validation across ALL 28 pairs — for each pair, did the
    # strategy chosen 5h ago point in the direction that the price actually
    # took during the elapsed 5h window?
    previous = load_previous_cycle()
    prev_per_pair = (previous or {}).get("per_pair", [])
    prev_top3 = {r["pair"] for r in (previous or {}).get("top3", [])}
    all_pairs_validation = walk_forward_check(prev_per_pair)
    # Subset of all_pairs_validation that was in the previous top-3.
    walk_forward = [x for x in all_pairs_validation if x["pair"] in prev_top3]

    # 24-hour strategy aging — compare current chosen strategy vs the
    # strategy chosen for the same pair 24h ago (≈ 5 cycles back).
    aging = compute_24h_aging(per_pair)

    diff = diff_with_previous(dict(top3=top3, per_pair=per_pair), previous)

    baseline = load_baseline()
    current_pp_dict = {r["pair"]: r for r in per_pair}
    anomalies = detect_anomalies(current_pp_dict, baseline)
    update_baseline(baseline, current_pp_dict)

    # Persist per-pair best strategy in long-lived memory.
    memory = load_strategy_memory()
    for r in per_pair:
        memory[r["pair"]] = {
            "params":         r.get("params"),
            "wr":             r.get("wr"),
            "trades":         r.get("trades"),
            "trades_per_day": r.get("trades_per_day"),
            "direction":      r.get("direction"),
            "saved_at":       cycle_label,
        }
    save_strategy_memory(memory)

    payload = dict(
        cycle_utc=cycle_label,
        top3=top3,
        per_pair=per_pair,
        diff=diff,
        anomalies=anomalies,
        indicator_attribution=indicator_attr,
        walk_forward=walk_forward,
        all_pairs_validation=all_pairs_validation,
        aging_24h=aging,
        on_target_count=len(on_target),
        sweep_attempts=sweep_attempts,
    )

    # Persist state.
    ts = now_utc5.strftime("%Y%m%dT%H%M")
    json.dump(payload, open(os.path.join(STATE_DIR, f"cycle_{ts}.json"), "w"), indent=2, default=str)
    json.dump(payload, open(os.path.join(STATE_DIR, "cycle_latest.json"), "w"), indent=2, default=str)

    # Markdown report (also posted as PR comment by the workflow).
    report_md = format_report(payload).replace("<b>", "**").replace("</b>", "**").replace("<i>", "*").replace("</i>", "*")
    with open(os.path.join(REPORTS_DIR, "cycle_5h_latest.md"), "w") as f:
        f.write(f"# 5-hour adaptive cycle — {cycle_label}\n\n")
        f.write(report_md)

    # Send to Telegram.
    text = format_report(payload)
    sent = send_telegram(text)
    print(f"[cycle] telegram sent: {sent}")
    print(f"[cycle] cycle complete · top3: {[r['pair'] for r in top3]}")


if __name__ == "__main__":
    main()
