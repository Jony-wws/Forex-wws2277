"""Full 28-pair backtest of the FOREX 2026 system.

Runs the SAME logic as `app/analyzer.py` and the live dashboard on every
single one of the 28 currency pairs and reports honest win-rates against
the 5-hour binary forecast horizon.

Outputs (under `reports/`):

* ``28pairs_backtest_latest.md``   — full markdown report (all pairs +
                                      daily TOP-3 picks + tier breakdown).
* ``28pairs_backtest_latest.json`` — same data in machine-readable form
                                      (used by future iterations / charts).

What it does
------------
1. Pulls M15 (60 d), H1 (730 d), H4 (730 d) and D1 (5 y) candles from
   Yahoo Finance for each of the 28 majors and crosses.
2. For every M15 bar it recomputes the 12 voting blocks + multi-TF gate +
   `trend_quality` composite + premium-tier gate (this is the SAME logic
   that runs in the live FastAPI dashboard).
3. For each bar it classifies the pair as ★ ПРЕМИУМ / СТРОГИЙ / no-signal
   and measures whether the *binary* close 5 hours later moved in the
   predicted direction.
4. **Daily TOP-3 picker** — at every 5-hour cycle boundary
   (UTC 00, 05, 10, 15, 20) it ranks all 28 pairs by `trend_quality` and
   keeps the top 3 with a directional side. Their realised 5h binary
   outcome is recorded — this mirrors what the dashboard recommends to
   the user every cycle.
5. Aggregates everything: WR per pair, daily TOP-3 WR, premium-only WR,
   strict-only WR, total trades, trades per day, and a final
   target-check (≥ 70 % WR, ≥ 100 trades, ≥ 3 trades / day, PnL > 0).

Run locally
-----------
    python scripts/backtest_28pairs.py

Run on CI
---------
    .github/workflows/backtest.yml triggers this script on every push and
    daily at 07:00 UTC.
"""
from __future__ import annotations

import json
import math
import os
import sys
from datetime import datetime, timedelta, timezone
from typing import Optional

import numpy as np
import pandas as pd

# Reuse the proven single-pair engine from backtest_eurusd.
from scripts.backtest_eurusd import (
    classify_dataframe,
    evaluate_horizon,
    fetch,
    fresh_only,
    HORIZON_M15_BARS,
    HORIZON_HOURS,
    PREMIUM_MIN_MOVE_PIPS_NONJPY,
    PREMIUM_MIN_MOVE_PIPS_JPY,
)


# ── CONFIG ─────────────────────────────────────────────────────────────
PAIRS: list[str] = [
    # Majors
    "EURUSD", "GBPUSD", "USDJPY", "USDCHF", "AUDUSD", "USDCAD", "NZDUSD",
    # EUR crosses
    "EURGBP", "EURJPY", "EURCHF", "EURAUD", "EURCAD", "EURNZD",
    # GBP crosses
    "GBPJPY", "GBPCHF", "GBPAUD", "GBPCAD", "GBPNZD",
    # JPY crosses
    "AUDJPY", "CADJPY", "CHFJPY", "NZDJPY",
    # Other crosses
    "AUDCAD", "AUDCHF", "AUDNZD",
    "CADCHF", "NZDCAD", "NZDCHF",
]

CYCLE_HOURS = 5
TOP_N = 3   # user-requested daily top-3 across all 28 pairs
PAYOUT = 0.80


def yahoo_ticker(pair: str) -> str:
    return f"{pair}=X"


def is_jpy(pair: str) -> bool:
    return "JPY" in pair


def pip_mult(pair: str) -> float:
    return 100.0 if is_jpy(pair) else 10000.0


def min_move_pips(pair: str) -> float:
    return PREMIUM_MIN_MOVE_PIPS_JPY if is_jpy(pair) else PREMIUM_MIN_MOVE_PIPS_NONJPY


# ── DATA ───────────────────────────────────────────────────────────────
def fetch_all_tfs(symbol: str) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Fetch M15 + H1 + H4 + D1 candles for one symbol with sensible periods."""
    m15 = fetch(period="60d", interval="15m", symbol=symbol)
    h1  = fetch(period="2y",  interval="1h",  symbol=symbol)
    # H4 is built by resampling H1 — yfinance has no native 4h.
    h4 = (
        h1.resample("4h")
        .agg({"Open": "first", "High": "max", "Low": "min",
              "Close": "last", "Volume": "sum"})
        .dropna()
    )
    d1  = fetch(period="5y",  interval="1d",  symbol=symbol)
    return m15, h1, h4, d1


# ── REPORT HELPERS ─────────────────────────────────────────────────────
def stats_block(df: pd.DataFrame, mask: pd.Series, label: str) -> dict:
    sub = df[mask & df["win"].notna()]
    n = int(len(sub))
    if n == 0:
        return {
            "label": label, "trades": 0, "wr": float("nan"),
            "avg_move_pp": float("nan"), "trades_per_day": float("nan"),
            "pnl_units": 0.0,
        }
    wr = float(sub["win"].mean()) * 100.0
    avg_move = float(sub["expected_move_pips_5h"].mean())
    span_days = max(1.0, (sub.index[-1] - sub.index[0]).total_seconds() / 86400.0)
    wins = int(sub["win"].sum())
    losses = n - wins
    pnl_units = wins * PAYOUT + losses * (-1.0)
    return {
        "label": label,
        "trades": n,
        "wr": wr,
        "avg_move_pp": avg_move,
        "trades_per_day": n / span_days,
        "pnl_units": pnl_units,
    }


# ── DAILY TOP-3 PICKER ────────────────────────────────────────────────
def cycle_boundaries(start: datetime, end: datetime) -> list[datetime]:
    """Generate UTC 5-hour cycle boundaries between start and end (inclusive)."""
    out = []
    t = start.replace(hour=0, minute=0, second=0, microsecond=0)
    while t <= end:
        for h in range(0, 24, CYCLE_HOURS):
            ts = t.replace(hour=h)
            if start <= ts <= end:
                out.append(ts)
        t += timedelta(days=1)
    return out


def top3_at_cycle(per_pair: dict[str, pd.DataFrame], when: datetime) -> list[dict]:
    """Pick the top 3 pairs by trend_quality at cycle start `when`.

    Returns at most 3 entries with their settled 5h outcome (or None if
    the bar at `when` is not yet in the data / has no future close)."""
    rows: list[tuple[str, pd.Series]] = []
    for pair, df in per_pair.items():
        # Use the first M15 bar at or after `when` (cycle anchor).
        future = df[df.index >= when]
        if future.empty:
            continue
        bar = future.iloc[0]
        if bar.get("side") not in ("BUY", "SELL"):
            continue
        rows.append((pair, bar))
    rows.sort(key=lambda r: float(r[1].get("trend_quality") or 0.0), reverse=True)
    picks = []
    for pair, bar in rows[:TOP_N]:
        win = bar.get("win")
        picks.append({
            "ts": when.isoformat(),
            "pair": pair,
            "side": str(bar.get("side")),
            "confidence": float(bar.get("confidence") or 0.0),
            "trend_quality": float(bar.get("trend_quality") or 0.0),
            "is_premium": bool(bar.get("is_premium") or False),
            "is_strict": bool(bar.get("is_strict") or False),
            "expected_move_pips_5h": float(bar.get("expected_move_pips_5h") or 0.0),
            "win": None if (win is None or (isinstance(win, float) and math.isnan(win))) else bool(win),
        })
    return picks


# ── PIPELINE ───────────────────────────────────────────────────────────
def run_pair(pair: str) -> tuple[Optional[pd.DataFrame], Optional[dict]]:
    """Fetch + classify + evaluate one pair. Returns (df, per-pair stats)."""
    symbol = yahoo_ticker(pair)
    try:
        m15, h1, h4, d1 = fetch_all_tfs(symbol)
    except Exception as exc:  # pragma: no cover
        print(f"[28pairs] {pair}: fetch failed — {exc}", flush=True)
        return None, None

    df = classify_dataframe(
        m15, h1, h4, d1,
        pip_mult=pip_mult(pair),
        min_move_pips=min_move_pips(pair),
    )
    df = evaluate_horizon(df, HORIZON_M15_BARS)

    fresh_strict = fresh_only(df, "is_strict")
    fresh_premium = fresh_only(df, "is_premium")

    pair_stats = {
        "pair": pair,
        "bars": int(len(df)),
        "m15_first": str(df.index[0]),
        "m15_last": str(df.index[-1]),
        "premium": stats_block(df, fresh_premium, "★ ПРЕМИУМ"),
        "strict_no_premium": stats_block(df, fresh_strict & ~df["is_premium"], "СТРОГИЙ"),
        "strict_total": stats_block(df, fresh_strict, "СТРОГИЙ + ПРЕМИУМ"),
    }
    return df, pair_stats


def aggregate_top3(per_pair_dfs: dict[str, pd.DataFrame]) -> dict:
    """Walk every 5h cycle boundary and pick the top-3 across all pairs."""
    if not per_pair_dfs:
        return {"trades": 0, "wr": float("nan"), "trades_per_day": 0.0, "pnl_units": 0.0, "picks": []}

    starts = [df.index[0].to_pydatetime() for df in per_pair_dfs.values()]
    ends   = [df.index[-1].to_pydatetime() for df in per_pair_dfs.values()]
    start = max(starts)
    end   = min(ends) - timedelta(hours=HORIZON_HOURS)  # leave room for outcome
    cycles = cycle_boundaries(start, end)

    all_picks: list[dict] = []
    for c in cycles:
        all_picks.extend(top3_at_cycle(per_pair_dfs, c))

    settled = [p for p in all_picks if p["win"] is not None]
    n = len(settled)
    if n == 0:
        return {"trades": 0, "wr": float("nan"), "trades_per_day": 0.0, "pnl_units": 0.0, "picks": all_picks}

    wins   = sum(1 for p in settled if p["win"])
    losses = n - wins
    wr     = wins / n * 100.0
    pnl    = wins * PAYOUT + losses * (-1.0)
    span_days = max(1.0, (cycles[-1] - cycles[0]).total_seconds() / 86400.0)
    return {
        "trades": n,
        "wr": wr,
        "wins": wins,
        "losses": losses,
        "trades_per_day": n / span_days,
        "pnl_units": pnl,
        "first_cycle": cycles[0].isoformat() if cycles else None,
        "last_cycle":  cycles[-1].isoformat() if cycles else None,
        "picks": all_picks,
    }


def aggregate_premium_strict(per_pair_stats: list[dict]) -> dict:
    """Sum trades / wins across all pairs for premium-only, strict-only and combined."""
    out = {}
    for tier_key, tier_label in (
        ("premium", "★ ПРЕМИУМ"),
        ("strict_no_premium", "СТРОГИЙ (без премиум)"),
        ("strict_total", "СТРОГИЙ + ПРЕМИУМ"),
    ):
        trades = 0
        wins   = 0
        for s in per_pair_stats:
            n  = s[tier_key]["trades"]
            wr = s[tier_key]["wr"]
            if n and not math.isnan(wr):
                trades += n
                wins   += int(round(n * wr / 100.0))
        if trades == 0:
            out[tier_key] = {"label": tier_label, "trades": 0, "wr": float("nan"), "pnl_units": 0.0}
            continue
        losses = trades - wins
        out[tier_key] = {
            "label": tier_label,
            "trades": trades,
            "wins":   wins,
            "losses": losses,
            "wr":     wins / trades * 100.0,
            "pnl_units": wins * PAYOUT + losses * (-1.0),
        }
    return out


# ── REPORT ─────────────────────────────────────────────────────────────
TARGET_WR = 70.0
TARGET_TRADES = 100
TARGET_TRADES_PER_DAY = 3.0


def fmt_wr(s: dict) -> str:
    wr = s.get("wr", float("nan"))
    return "n/a" if (wr is None or math.isnan(wr)) else f"{wr:.2f} %"


def fmt_pnl(s: dict) -> str:
    return f"{s.get('pnl_units', 0.0):+.2f}"


def render_report(per_pair_stats: list[dict], totals: dict, top3: dict, info: dict) -> str:
    lines: list[str] = []
    lines.append(f"# Полный 28-парный бэктест системы — {info['as_of']}")
    lines.append("")
    lines.append(f"**Период M15:** {info['m15_first']} → {info['m15_last']}")
    lines.append(f"**Горизонт:** {HORIZON_HOURS} ч binary  ({HORIZON_M15_BARS} M15 баров)")
    lines.append(f"**Выплата:** {PAYOUT*100:.0f} %  (breakeven WR ≈ 55.56 %)")
    lines.append(f"**Пары:** {len(per_pair_stats)} из {len(PAIRS)}")
    lines.append("")

    # Aggregates first — that's what the user wants.
    lines.append("## Итог по всем 28 парам — финальный контроль")
    lines.append("")
    lines.append("| Уровень | Сделок | Win Rate | PnL (80%) |")
    lines.append("|---|---:|---:|---:|")
    for key in ("premium", "strict_no_premium", "strict_total"):
        s = totals[key]
        lines.append(f"| **{s['label']}** | {s['trades']} | {fmt_wr(s)} | {fmt_pnl(s)} |")
    lines.append("")

    # Daily top-3 picker — the second main aggregate.
    lines.append("## Каждый день — топ-3 из 28 (по trend_quality)")
    lines.append("")
    lines.append("| Метрика | Значение |")
    lines.append("|---|---:|")
    lines.append(f"| Всего отобранных сделок | **{top3['trades']}** |")
    lines.append(f"| Wins / Losses | {top3.get('wins', 0)} / {top3.get('losses', 0)} |")
    lines.append(f"| Win Rate | **{fmt_wr(top3)}** |")
    lines.append(f"| Сделок в день | {top3.get('trades_per_day', 0):.2f} |")
    lines.append(f"| PnL (80% binary) | **{fmt_pnl(top3)}** |")
    if top3.get("first_cycle"):
        lines.append(f"| Первый цикл | {top3['first_cycle']} |")
        lines.append(f"| Последний цикл | {top3['last_cycle']} |")
    lines.append("")

    # Final target check — what the user asked for explicitly.
    lines.append("## Контроль целей пользователя")
    lines.append("")
    target_wr  = top3.get("wr")
    target_trd = top3.get("trades", 0)
    target_tpd = top3.get("trades_per_day", 0.0)
    target_pnl = top3.get("pnl_units", 0.0)
    lines.append(f"- WR ≥ {TARGET_WR:.0f} %        …  {'✓' if target_wr is not None and target_wr >= TARGET_WR else '✗'}  ({fmt_wr(top3)})")
    lines.append(f"- Сделок ≥ {TARGET_TRADES}        …  {'✓' if target_trd >= TARGET_TRADES else '✗'}  ({target_trd})")
    lines.append(f"- Сделок/день ≥ {TARGET_TRADES_PER_DAY:.0f}    …  {'✓' if target_tpd >= TARGET_TRADES_PER_DAY else '✗'}  ({target_tpd:.2f})")
    lines.append(f"- PnL > 0          …  {'✓' if target_pnl > 0 else '✗'}  ({target_pnl:+.2f})")
    lines.append("")

    # Per-pair breakdown.
    lines.append("## Разбивка по 28 парам")
    lines.append("")
    lines.append("| Пара | Premium WR | Premium Trades | Strict WR | Strict Trades |")
    lines.append("|---|---:|---:|---:|---:|")
    for s in per_pair_stats:
        lines.append(
            f"| {s['pair']} | "
            f"{fmt_wr(s['premium'])} | {s['premium']['trades']} | "
            f"{fmt_wr(s['strict_total'])} | {s['strict_total']['trades']} |"
        )
    lines.append("")
    lines.append("Сгенерировано `scripts/backtest_28pairs.py` — Yahoo Finance, M15 60 дней + H1/H4 2 года + D1 5 лет.")
    return "\n".join(lines)


# ── MAIN ───────────────────────────────────────────────────────────────
def main() -> int:
    per_pair_dfs:   dict[str, pd.DataFrame] = {}
    per_pair_stats: list[dict] = []

    print(f"[28pairs] processing {len(PAIRS)} pairs (this takes ~2-4 min) …", flush=True)
    for pair in PAIRS:
        print(f"[28pairs]   {pair} …", flush=True)
        df, stats = run_pair(pair)
        if df is None or stats is None:
            continue
        per_pair_dfs[pair] = df
        per_pair_stats.append(stats)
        prem = stats["premium"]
        strict = stats["strict_total"]
        print(
            f"[28pairs]   {pair} done: "
            f"premium={prem['trades']}@{fmt_wr(prem)}, "
            f"strict={strict['trades']}@{fmt_wr(strict)}",
            flush=True,
        )

    if not per_pair_dfs:
        print("[28pairs] no pair data downloaded — aborting", flush=True)
        return 2

    totals = aggregate_premium_strict(per_pair_stats)
    top3   = aggregate_top3(per_pair_dfs)
    info = {
        "as_of": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "m15_first": str(min(df.index[0] for df in per_pair_dfs.values())),
        "m15_last":  str(max(df.index[-1] for df in per_pair_dfs.values())),
    }

    report = render_report(per_pair_stats, totals, top3, info)

    os.makedirs("reports", exist_ok=True)
    with open("reports/28pairs_backtest_latest.md", "w") as f:
        f.write(report)

    # Also write a JSON sidecar (no DataFrames — just summary data) for future tooling.
    json_payload = {
        "as_of": info["as_of"],
        "m15_first": info["m15_first"],
        "m15_last":  info["m15_last"],
        "horizon_hours": HORIZON_HOURS,
        "payout": PAYOUT,
        "totals": totals,
        "top3": {k: v for k, v in top3.items() if k != "picks"},
        "per_pair": [{
            "pair": s["pair"],
            "bars": s["bars"],
            "premium": s["premium"],
            "strict_no_premium": s["strict_no_premium"],
            "strict_total": s["strict_total"],
        } for s in per_pair_stats],
    }
    with open("reports/28pairs_backtest_latest.json", "w") as f:
        json.dump(json_payload, f, indent=2, ensure_ascii=False, default=str)

    print("\n" + report, flush=True)
    print("\n[28pairs] reports/28pairs_backtest_latest.{md,json} written", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
