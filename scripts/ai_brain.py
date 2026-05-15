"""Run the new AI brain and publish data/top1.json + data/brain_full.json.

Designed to run on GitHub Actions (``.github/workflows/ai_brain.yml``)
every 5 minutes for fresh layer scores + every 5 hours for the canonical
Top-1 forecast that the UI shows.

Real-money safety:
- Refuses to publish a forecast if the veto layer eliminates EVERY pair
  (returns ``"top1": null`` so the site shows "ждём цикла" instead of
  forcing a low-conviction trade).
- Pins the ``next_cycle_utc`` boundary so the UI countdown stays in sync
  with the GitHub-Actions cron schedule.
- Writes a *separate* ``brain_full.json`` with the per-pair breakdown
  so journals/audits can replay any decision later.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.brain import select_top1  # noqa: E402

log = logging.getLogger("ai_brain")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

DATA_DIR = ROOT / "data"


def _write(payload: dict, file: Path) -> None:
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    log.info(f"wrote {file.relative_to(ROOT)} ({file.stat().st_size} bytes)")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--quick",
        action="store_true",
        help="Skip the per-pair full breakdown (top1 only) — for 5-min refresh",
    )
    args = parser.parse_args()

    started = datetime.now(timezone.utc)
    log.info("AI brain start — running select_top1() across 28 pairs…")
    payload = select_top1()

    # `top1` is the user-facing canonical card — kept tiny so the SPA
    # loads it instantly on slow mobile networks.  We also expose the
    # big-player / clear-favorite layers so the UI can display "почему
    # фаворит" and "куда стоит smart money" without re-fetching.
    top1_payload = {
        "generated_at_utc": payload["generated_at_utc"],
        "next_cycle_utc": payload["next_cycle_utc"],
        "top1": payload["top1"],
        "top5": payload["top5"],
        "macro_currency_strength": payload["macro"]["currency_strength"],
        "sentiment": payload["sentiment"],
        "political_risk": payload["political_risk"],
        "big_players": payload.get("big_players"),
        "favorite_check": payload.get("favorite_check"),
    }
    _write(top1_payload, DATA_DIR / "top1.json")

    if not args.quick:
        _write(payload, DATA_DIR / "brain_full.json")

    finished = datetime.now(timezone.utc)
    log.info(f"AI brain done in {(finished-started).total_seconds():.1f}s")
    if payload["top1"] is None:
        log.warning(
            "No pair survived the veto filter — published top1=null on purpose."
        )
    else:
        t = payload["top1"]
        log.info(
            f"Top-1: {t['pair']} {t['side']} conf={t['confidence']}% — "
            f"reason via {len(t['layers']['technical']['details'])} TA votes"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
