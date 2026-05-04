"""Per-pair behavior profile.

For each of 28 pairs, build a session-level profile (Asia / London / Overlap / NY):
- top event drivers (by frequency, with hit-rate / persistence / trap-rate)
- typical reaction (mean signed move pips)
- traps to avoid (high trap-rate event patterns)
- session liquidity-trap risk (% of significant moves that are traps)

Output:
- HISTORY/event_attribution_365d/per_pair_behavior.csv (one row per pair × session)
- HISTORY/event_attribution_365d/persistent_drivers.csv (event-types qualifying as
  "persistent" globally: count >=10, persistence >=60, hit-rate >=60 OR concord >=80)
- HISTORY/event_attribution_365d/pair_<PAIR>.md (28 markdown files with verdict)
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

import pandas as pd

from .. import config

log = logging.getLogger("events.profile")

ROOT = Path(__file__).resolve().parents[2]
OUT_DIR = ROOT / "HISTORY" / "event_attribution_365d"

PER_EVENT_FILE = OUT_DIR / "per_event_response.csv"
PER_EVENT_PS_FILE = OUT_DIR / "per_event_pair_session.csv"
TRAP_PAIR_FILE = OUT_DIR / "trap_pair_session_summary.csv"
TRAP_EVENT_FILE = OUT_DIR / "trap_event_patterns.csv"


# Persistent driver thresholds (used in the global table)
MIN_TOTAL_MATCHES = 10
MIN_PERSISTENCE = 60.0
MIN_CONCORDANCE = 70.0


def session_label(s: str) -> str:
    return {"Asia": "Asia (00-06 UTC)", "London": "London (07-12 UTC)",
            "Overlap": "Overlap (13-16 UTC)", "NY": "NY (17-21 UTC)"}.get(s, s)


def main() -> None:
    df_event = pd.read_csv(PER_EVENT_FILE)
    df_evt_ps = pd.read_csv(PER_EVENT_PS_FILE)
    df_traps_ps = pd.read_csv(TRAP_PAIR_FILE)
    df_traps_evt = pd.read_csv(TRAP_EVENT_FILE)

    # 1. Persistent drivers (global, across all pairs/sessions)
    persistent = df_event[
        (df_event["total_matches"] >= MIN_TOTAL_MATCHES)
        & ((df_event["persistence_24h_avg_pct"].fillna(0) >= MIN_PERSISTENCE)
           | (df_event["direction_concordance_pct"].fillna(0) >= MIN_CONCORDANCE))
    ].copy()
    persistent = persistent.sort_values(["persistence_24h_avg_pct", "total_matches"], ascending=[False, False])
    persistent.to_csv(OUT_DIR / "persistent_drivers.csv", index=False)
    log.info(f"persistent_drivers: {len(persistent)} event-types qualified")

    # 2. Per-pair × session behavior summary
    rows = []
    for pair in config.PAIRS:
        for sess in ["Asia", "London", "Overlap", "NY"]:
            cell_evt = df_evt_ps[(df_evt_ps["pair"] == pair) & (df_evt_ps["session"] == sess)]
            trap_row = df_traps_ps[(df_traps_ps["pair"] == pair) & (df_traps_ps["session"] == sess)]
            n_significant = int(trap_row["n_significant"].iloc[0]) if len(trap_row) else 0
            n_traps = int(trap_row["n_traps"].iloc[0]) if len(trap_row) else 0
            trap_pct = float(trap_row["trap_pct_of_significant"].iloc[0]) if len(trap_row) else 0
            # Top 3 event drivers
            top = cell_evt.sort_values("frequency", ascending=False).head(3)
            top_drivers_str = " | ".join(
                f"{r['event_type']} (n={r['frequency']}, dir={r['dominant_direction_event_ccy']}, conc={r['direction_concordance_pct']:.0f}%, persist={(r['persistence_24h_avg_pct'] if pd.notna(r['persistence_24h_avg_pct']) else 0):.0f}%)"
                for _, r in top.iterrows()
            )
            rows.append({
                "pair": pair,
                "session": sess,
                "n_significant_moves_365d": n_significant,
                "n_traps": n_traps,
                "trap_pct_of_significant": trap_pct,
                "top_event_drivers": top_drivers_str,
            })
    df_summary = pd.DataFrame(rows)
    df_summary.to_csv(OUT_DIR / "per_pair_behavior.csv", index=False)
    log.info(f"per_pair_behavior: {len(df_summary)} rows")

    # 3. Per-pair markdown files (28 files)
    for pair in config.PAIRS:
        md_path = OUT_DIR / f"pair_{pair}.md"
        lines = []
        lines.append(f"# {pair} — 365-day event-attribution profile")
        lines.append("")
        lines.append(f"_(period: 365 days, Yahoo 1H bars, real events from FRED+CB+COT+geo archive)_")
        lines.append("")
        for sess in ["Asia", "London", "Overlap", "NY"]:
            lines.append(f"## {session_label(sess)}")
            tps = df_traps_ps[(df_traps_ps["pair"] == pair) & (df_traps_ps["session"] == sess)]
            if len(tps) == 0:
                lines.append("No data.")
                lines.append("")
                continue
            r = tps.iloc[0]
            lines.append(f"- significant moves (>1.5σ): **{int(r['n_significant'])}** of {int(r['n_total_cells'])} cells ({r['significant_pct_of_total']:.1f}%)")
            lines.append(f"- traps (significant move reversed ≥80% within 6h): **{int(r['n_traps'])}** ({r['trap_pct_of_significant']:.1f}% of significant moves)")
            lines.append("")

            # Top event drivers
            cell_evt = df_evt_ps[(df_evt_ps["pair"] == pair) & (df_evt_ps["session"] == sess)].sort_values("frequency", ascending=False)
            if len(cell_evt) > 0:
                lines.append("**Top event drivers:**")
                lines.append("")
                lines.append("| event_type | n | mean_signed_move_pips | direction (event ccy) | concordance | persistence_24h | trap_rate |")
                lines.append("|---|---:|---:|---|---:|---:|---:|")
                for _, e in cell_evt.head(8).iterrows():
                    persist = f"{e['persistence_24h_avg_pct']:.0f}%" if pd.notna(e["persistence_24h_avg_pct"]) else "n/a"
                    lines.append(
                        f"| {e['event_type']} | {int(e['frequency'])} | "
                        f"{e['mean_signed_move_pips']:+.1f} | "
                        f"{e['dominant_direction_event_ccy']} | "
                        f"{e['direction_concordance_pct']:.0f}% | "
                        f"{persist} | "
                        f"{e['trap_rate_pct']:.0f}% |"
                    )
                lines.append("")

            # Trap-event patterns specific to this cell
            cell_traps = df_traps_evt[(df_traps_evt["pair"] == pair) & (df_traps_evt["session"] == sess) & (df_traps_evt["trap_rate_pct"] >= 50.0)]
            if len(cell_traps) > 0:
                lines.append("**Frequent trap setups (≥50% reversal):**")
                lines.append("")
                for _, t in cell_traps.iterrows():
                    lines.append(f"- {t['event_type']}: trap rate **{t['trap_rate_pct']:.0f}%** ({int(t['n_traps'])}/{int(t['n_event_significant_matches'])} significant matches)")
                lines.append("")

        md_path.write_text("\n".join(lines))
    log.info(f"wrote 28 per-pair markdown files to {OUT_DIR}")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    main()
