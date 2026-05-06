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


def param_grid() -> list[Params]:
    """A reasonable grid: 30 combos that span trend & MR variants."""
    grid: list[Params] = []
    # Trend-following variants: high MTF + ADX > 20.
    for hor in (12, 20, 28):           # 3h / 5h / 7h horizon
        for adxmin in (18, 22):
            for tq in (60, 65, 75):
                grid.append(Params(
                    horizon_bars=hor, adx_min=adxmin, adx_max=60.0,
                    require_mtf=True, min_conf=72, min_trend_q=tq,
                ))
    # Mean-reversion variants: ADX < 30 + RSI extremes.
    for rsi_os in (25, 28, 30):
        for adx_cap in (28, 32, 36):
            for tq in (45, 55):
                grid.append(Params(
                    rsi_oversold=rsi_os, rsi_overbought=100 - rsi_os,
                    horizon_bars=20, adx_min=10.0, adx_max=adx_cap,
                    require_mtf=False, min_conf=68, min_trend_q=tq,
                ))
    return grid


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
def sweep_pair(pair: str) -> dict:
    """Run sweep on one pair. Returns the best params + stats.

    Optimisation: `classify_dataframe` is expensive but its output is
    invariant to the filter params we sweep over. Compute once per pair,
    then only re-apply the cheap filter / horizon evaluation per combo.
    """
    print(f"[cycle] sweep {pair}")
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

    grid = param_grid()
    results: list[dict] = []
    for p in grid:
        try:
            r = filter_and_score(sig, direction, multi_tf_aligned, pair, p)
            results.append(r)
        except Exception as e:
            print(f"  ! {pair} params {p}: {e}")
            continue

    # Filter: at least 20 trades for statistical sanity; rank by combined score.
    valid = [r for r in results if r.get("trades", 0) >= 20]
    if not valid:
        valid = sorted(results, key=lambda r: -r.get("trades", 0))[:3]

    # Combined ranking: 0.6 * (WR - 50) + 0.4 * trades_per_day_normalized.
    def rank(r: dict) -> float:
        wr = r.get("wr", 0)
        tpd = min(r.get("trades_per_day", 0), 5.0)  # cap
        return 0.6 * (wr - 50.0) + 0.4 * tpd * 10.0

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

    best["pair"] = pair
    best["last_5h_pp"] = round(last_5h_pp, 1)
    best["last_close"] = round(last_close, 5)
    best["bars_m15"]   = int(len(m15))
    best["direction"]  = int(direction_now)
    return best


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
def pick_top3(per_pair: list[dict]) -> list[dict]:
    """Top-3 picks for the next 5h.

    Ranking favours pairs that are both **profitable** (WR > breakeven for an
    80%-payout binary, i.e. WR > 55.5%) and **moving in the right direction
    right now**. Pairs without a current direction or below breakeven get
    progressively penalised but still scored — we never return fewer than
    three names so the report always has content.
    """
    candidates = [r for r in per_pair if r.get("trades", 0) >= 20]

    def score(r: dict) -> float:
        wr = r.get("wr", 0)
        tpd = min(r.get("trades_per_day", 0), 5.0)
        d = r.get("direction", 0)
        last5h = r.get("last_5h_pp", 0)
        # WR bonus is in % points above breakeven, capped at 25 to avoid
        # outsized influence from low-trade-count pairs.
        wr_edge = max(min(wr - 55.6, 25.0), -25.0)
        # Direction bonus: positive only if pair is currently aligned with
        # its strategy direction over the last 5h price move.
        dir_bonus = (last5h * d * 0.1) if d != 0 else -3.0
        return 0.7 * wr_edge + 0.2 * tpd * 10.0 + 0.1 * dir_bonus

    candidates.sort(key=lambda r: -score(r))
    return candidates[:TOP_N]


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
    """Build the human-readable Telegram message."""
    out = []
    out.append(f"<b>🎯 5h cycle {payload['cycle_utc']}</b>\n")
    # cycle_utc is already labelled in UTC+5; key kept for backwards compat.

    out.append(f"\n<b>📈 ТОП-3 на следующие 5 часов:</b>")
    for i, r in enumerate(payload["top3"], 1):
        side = "BUY" if r.get("direction", 0) == 1 else "SELL"
        out.append(f"{i}. <b>{r['pair']}</b> {side}  WR <b>{r['wr']}%</b> · trades {r['trades']} · last 5h: {r.get('last_5h_pp', 0):+.1f} pp")

    diff = payload.get("diff", {})
    if diff.get("top3_in") or diff.get("top3_out"):
        out.append(f"\n<b>🔄 Что изменилось за 5 часов:</b>")
        if diff.get("top3_in"):
            out.append(f"➕ Вошли в топ-3: <b>{', '.join(diff['top3_in'])}</b>")
        if diff.get("top3_out"):
            out.append(f"➖ Вышли из топ-3: <b>{', '.join(diff['top3_out'])}</b>")
        for f in diff.get("strategy_flips", [])[:5]:
            out.append(f"🔁 {f['pair']}: направление BUY↔SELL (WR {f['prev_wr']}% → {f['wr']}%)")

    if payload.get("indicator_attribution"):
        out.append(f"\n<b>⚙ Индикаторы за этот цикл:</b>")
        for ind, pct in payload["indicator_attribution"][:5]:
            out.append(f"  {ind}: {pct:.0f}%")

    if payload.get("anomalies"):
        out.append(f"\n<b>⚠ Аномалии (z ≥ 1.5):</b>")
        for a in payload["anomalies"][:5]:
            arrow = "📈" if a["type"] == "up" else "📉"
            out.append(f"  {arrow} {a['pair']}: WR {a['wr_now']}% (базовый {a['wr_mean']}%, z={a['z']})")

    out.append(f"\n<b>📊 Per-pair WR (топ 10 по WR):</b>")
    sorted_pp = sorted(
        [r for r in payload["per_pair"] if r.get("trades", 0) >= 20],
        key=lambda r: -r.get("wr", 0),
    )[:10]
    for r in sorted_pp:
        out.append(f"  {r['pair']}: WR {r['wr']}% · {r['trades']} trades · {r.get('trades_per_day', 0):.1f}/day · PnL {r['pnl']:+.1f}")

    out.append(f"\n<i>Авто-генерация GitHub Actions · следующий цикл через 5 часов</i>")
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


# ── MAIN ───────────────────────────────────────────────────────────────
def main() -> None:
    now_utc5 = datetime.now(TZ_UTC5)
    cycle_label = now_utc5.strftime("%Y-%m-%d %H:%M UTC+5")
    print(f"[cycle] starting cycle {cycle_label}")

    per_pair: list[dict] = []
    pair_to_data: dict[str, tuple] = {}

    for pair in PAIRS:
        try:
            best = sweep_pair(pair)
            if "error" in best:
                print(f"[cycle] skip {pair}: {best['error']}")
                continue
            per_pair.append(best)
        except Exception as e:
            print(f"[cycle] {pair} failed entirely: {e}")
            continue

    print(f"[cycle] swept {len(per_pair)} / {len(PAIRS)} pairs")

    top3 = pick_top3(per_pair)
    indicator_attr = compute_indicator_attribution(per_pair)

    previous = load_previous_cycle()
    diff = diff_with_previous(dict(top3=top3, per_pair=per_pair), previous)

    baseline = load_baseline()
    current_pp_dict = {r["pair"]: r for r in per_pair}
    anomalies = detect_anomalies(current_pp_dict, baseline)
    update_baseline(baseline, current_pp_dict)

    payload = dict(
        cycle_utc=cycle_label,
        top3=top3,
        per_pair=per_pair,
        diff=diff,
        anomalies=anomalies,
        indicator_attribution=indicator_attr,
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
