"""generate_backtest_report — собирает CSV-сводку из реальных 365-day
бэктестов (strategy_search per-(pair×session) + backtester per-pair).

Источники (всё реальные данные Yahoo OHLCV, никаких симуляторов):
  - teamagent/state/strategy_config.json        (per-(pair, session) × 250 variants)
  - teamagent/state/backtest_30d.json           (per-pair overall, 365 дней)
  - teamagent/state/strategy_config_locked.json (baseline-snapshot)

Выход (теперь в HISTORY/backtest_365d_csv/):
  - per_pair_overall.csv          — overall WR на 365д на парy (backtester)
  - per_pair_session.csv          — WR per (pair, session) (strategy_search)
  - distribution.csv              — сколько ячеек/пар попадает в каждое WR-ведро
  - top10_live_candidates.csv     — top-10 (pair × session) с WR ≥ 65 и trades ≥ 30
  - bottom10_avoid.csv            — bottom-10 (pair × session) c наименьшим WR

Вызов:
  python scripts/generate_backtest_report.py
"""
from __future__ import annotations
import csv
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATE = ROOT / "teamagent" / "state"
OUT_DIR = ROOT / "HISTORY" / "backtest_365d_csv"
OUT_DIR.mkdir(parents=True, exist_ok=True)


def _load(name: str) -> dict:
    p = STATE / name
    if not p.exists():
        print(f"!! missing {p}", file=sys.stderr)
        return {}
    return json.loads(p.read_text())


def write_per_pair_overall(bt: dict) -> Path:
    """Per-pair overall WR на 365д (из backtester.run_full_backtest)."""
    rows = []
    pairs = bt.get("pairs") or {}
    for pair, r in pairs.items():
        rows.append({
            "pair": pair,
            "trades": r.get("trades") or 0,
            "wins": r.get("wins") or 0,
            "losses": r.get("losses") or 0,
            "win_rate_pct": r.get("win_rate_pct"),
            "total_pnl_usd": r.get("total_pnl_usd"),
            "avg_score": r.get("avg_score"),
            "note": r.get("note") or "",
        })
    # сортируем по WR desc, потом trades desc
    rows.sort(key=lambda x: (-(x["win_rate_pct"] or 0), -x["trades"]))

    out = OUT_DIR / "per_pair_overall.csv"
    with out.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()) if rows else
                           ["pair","trades","wins","losses","win_rate_pct","total_pnl_usd","avg_score","note"])
        w.writeheader()
        w.writerows(rows)
    print(f"wrote {out} ({len(rows)} rows)")
    return out


def _all_cells(strat: dict) -> list[dict]:
    """Все ячейки (pair × session) из strategy_search.json."""
    cells = []
    pairs = strat.get("pairs") or {}
    for pair, r in pairs.items():
        by_session = r.get("by_session") or {}
        for sess_name, sd in by_session.items():
            cells.append({
                "pair": pair,
                "session": sess_name,
                "window_utc_start": (sd.get("window_utc") or [None, None])[0],
                "window_utc_end": (sd.get("window_utc") or [None, None])[1],
                "best_variant": sd.get("best_variant"),
                "best_label": sd.get("best_label") or "",
                "trades": sd.get("trades") or 0,
                "wins": sd.get("wins") or 0,
                "losses": sd.get("losses") or 0,
                "win_rate_pct": sd.get("win_rate_pct"),
                "pnl_usd": sd.get("pnl_usd"),
                "qualifies_70pct": bool(sd.get("qualifies_70pct")),
            })
    return cells


def write_per_pair_session(strat: dict) -> Path:
    cells = _all_cells(strat)
    cells.sort(key=lambda x: (x["pair"], x["session"]))
    out = OUT_DIR / "per_pair_session.csv"
    with out.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(cells[0].keys()) if cells else
                           ["pair","session","window_utc_start","window_utc_end","best_variant","best_label",
                            "trades","wins","losses","win_rate_pct","pnl_usd","qualifies_70pct"])
        w.writeheader()
        w.writerows(cells)
    print(f"wrote {out} ({len(cells)} rows)")
    return out


def write_distribution(bt: dict, strat: dict) -> Path:
    """Сколько пар/ячеек попадает в каждое WR-ведро."""
    buckets = [
        ("≥70%", lambda wr: wr is not None and wr >= 70),
        ("65-70%", lambda wr: wr is not None and 65 <= wr < 70),
        ("60-65%", lambda wr: wr is not None and 60 <= wr < 65),
        ("55-60%", lambda wr: wr is not None and 55 <= wr < 60),
        ("50-55%", lambda wr: wr is not None and 50 <= wr < 55),
        ("<50%",  lambda wr: wr is not None and wr < 50),
        ("n/a",   lambda wr: wr is None),
    ]

    pairs = (bt.get("pairs") or {}).values()
    pair_wrs = [r.get("win_rate_pct") for r in pairs]

    cells = _all_cells(strat)
    cell_wrs_by_session: dict[str, list] = {}
    for c in cells:
        cell_wrs_by_session.setdefault(c["session"], []).append(c["win_rate_pct"])
    cell_wrs_all = [c["win_rate_pct"] for c in cells]

    rows = []
    for label, pred in buckets:
        rows.append({
            "bucket": label,
            "pairs_overall": sum(1 for w in pair_wrs if pred(w)),
            "cells_all": sum(1 for w in cell_wrs_all if pred(w)),
            "cells_asia":    sum(1 for w in cell_wrs_by_session.get("Asia", []) if pred(w)),
            "cells_london":  sum(1 for w in cell_wrs_by_session.get("London", []) if pred(w)),
            "cells_overlap": sum(1 for w in cell_wrs_by_session.get("Overlap", []) if pred(w)),
            "cells_ny":      sum(1 for w in cell_wrs_by_session.get("NY", []) if pred(w)),
        })

    out = OUT_DIR / "distribution.csv"
    with out.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    print(f"wrote {out} ({len(rows)} rows)")
    return out


def write_top10_live(strat: dict) -> Path:
    """Top-10 (pair × session) с WR ≥ 65 и trades ≥ 30 — реальные live-кандидаты."""
    cells = _all_cells(strat)
    qualified = [
        c for c in cells
        if c["win_rate_pct"] is not None and c["win_rate_pct"] >= 65 and c["trades"] >= 30
    ]
    qualified.sort(key=lambda x: (-(x["win_rate_pct"] or 0), -x["trades"]))
    top10 = qualified[:10]
    out = OUT_DIR / "top10_live_candidates.csv"
    fieldnames = ["rank","pair","session","trades","wins","win_rate_pct","pnl_usd","best_variant","best_label"]
    with out.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for i, c in enumerate(top10, 1):
            w.writerow({
                "rank": i,
                "pair": c["pair"],
                "session": c["session"],
                "trades": c["trades"],
                "wins": c["wins"],
                "win_rate_pct": c["win_rate_pct"],
                "pnl_usd": c["pnl_usd"],
                "best_variant": c["best_variant"] or "",
                "best_label": c["best_label"] or "",
            })
    print(f"wrote {out} ({len(top10)} rows out of {len(qualified)} qualified)")
    return out


def write_bottom10(strat: dict) -> Path:
    """Bottom-10 (pair × session) с наименьшим WR (>= 30 trades для статзначимости).
    Это «не торговать» список."""
    cells = _all_cells(strat)
    eligible = [c for c in cells if c["win_rate_pct"] is not None and c["trades"] >= 30]
    eligible.sort(key=lambda x: (x["win_rate_pct"] or 0, -x["trades"]))
    bot = eligible[:10]
    out = OUT_DIR / "bottom10_avoid.csv"
    fieldnames = ["rank","pair","session","trades","wins","win_rate_pct","pnl_usd","best_variant","best_label"]
    with out.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for i, c in enumerate(bot, 1):
            w.writerow({
                "rank": i,
                "pair": c["pair"],
                "session": c["session"],
                "trades": c["trades"],
                "wins": c["wins"],
                "win_rate_pct": c["win_rate_pct"],
                "pnl_usd": c["pnl_usd"],
                "best_variant": c["best_variant"] or "",
                "best_label": c["best_label"] or "",
            })
    print(f"wrote {out} ({len(bot)} rows out of {len(eligible)} eligible)")
    return out


def write_session_summary(strat: dict) -> Path:
    """Aggregated WR per session."""
    by_session = (strat.get("summary") or {}).get("by_session") or {}
    rows = []
    for sess_name, s in by_session.items():
        rows.append({
            "session": sess_name,
            "window_utc": "-".join(str(x) for x in (s.get("window_utc") or [])),
            "qualified_count_70pct": s.get("qualified_count"),
            "total_pairs_with_data": s.get("total_pairs_with_data"),
            "mean_wr_pct": s.get("mean_wr_pct"),
            "aggregated_wr_pct": s.get("aggregated_wr_pct"),
            "trades_total": s.get("trades_total"),
            "wins_total": s.get("wins_total"),
            "qualified_pairs_70pct": ",".join(s.get("qualified_pairs_70pct") or []),
        })
    out = OUT_DIR / "session_summary.csv"
    if not rows:
        rows.append({k: None for k in ["session","window_utc","qualified_count_70pct",
                                        "total_pairs_with_data","mean_wr_pct","aggregated_wr_pct",
                                        "trades_total","wins_total","qualified_pairs_70pct"]})
    with out.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    print(f"wrote {out} ({len(rows)} rows)")
    return out


def main() -> int:
    bt = _load("backtest_30d.json")
    strat = _load("strategy_config.json")

    if not bt:
        print("!! backtest_30d.json missing — run `python -m teamagent.backtester once` first", file=sys.stderr)
    if not strat or not strat.get("pairs"):
        print("!! strategy_config.json missing or empty — run `python -m teamagent.strategy_search` first", file=sys.stderr)
        return 1

    write_per_pair_overall(bt)
    write_per_pair_session(strat)
    write_distribution(bt, strat)
    write_top10_live(strat)
    write_bottom10(strat)
    write_session_summary(strat)

    # meta
    meta = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "lookback_days": 365,
        "data_source": "Yahoo Finance 1H OHLCV (real, no simulator)",
        "strategy_search_as_of": strat.get("as_of"),
        "backtester_as_of": bt.get("as_of"),
        "total_pairs": (strat.get("summary") or {}).get("total_pairs"),
        "qualified_pairs_70pct_count": (strat.get("summary") or {}).get("qualified_count"),
    }
    (OUT_DIR / "meta.json").write_text(json.dumps(meta, indent=2))
    print(f"wrote {OUT_DIR}/meta.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
