"""Supabase pgvector memory indexer — embeds historical winning 5h forecasts.

This is a *retrieval memory* for the AI patcher / AI reviewer.  Every
day (cron, see ``.github/workflows/memory_index.yml``) it:

1. Reads ``state/forecasts.json`` (the strict-cycle history written by
   ``app/cycle.py``) and walks all evaluated forecasts.
2. Builds a **deterministic 9-D feature vector** for each forecast — no
   LLM is called for embedding so the memory stays free and local:

       [confidence, score, score/max_score, adx_h1, adx_h4,
        trend_persistence_5h, multi_tf_aligned (0/1),
        side (BUY=+1, SELL=-1), win (1) / loss (0)]

   These are the exact features the strict-cycle gate already cares
   about, and they make cosine similarity meaningful for "is the
   current setup analogous to past winners?".
3. Upserts the rows into the Supabase Postgres table ``trade_memory``
   via the official ``supabase-py`` client.

The script is **deliberately defensive** — it skips silently with a
printed warning rather than crashing whenever:

* ``supabase-py`` is not installed (``pip install supabase``),
* ``SUPABASE_URL`` / ``SUPABASE_KEY`` env vars are missing,
* ``state/forecasts.json`` does not exist or has no history yet.

This way the daily workflow on a freshly-cloned org keeps working even
before the user has provisioned a Supabase project.

Run locally::

    SUPABASE_URL=https://xxx.supabase.co \
    SUPABASE_KEY=eyJ... \
    python scripts/memory_index.py

Free tier setup (≤ 500 MB Postgres, plenty for thousands of forecasts):
https://supabase.com/dashboard/projects → New project → free tier.
SQL to bootstrap the table is printed by the script the first time it
runs (see ``BOOTSTRAP_SQL``).
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parent.parent
STATE_FILE = ROOT / "state" / "forecasts.json"

# 9-D vector — order matters and must match memory_query.py exactly.
FEATURE_DIM = 9

TABLE = "trade_memory"

# SQL the user runs once in the Supabase SQL editor to create the table.
# pgvector is included for free on every Supabase Postgres instance.
BOOTSTRAP_SQL = """\
-- Run once in Supabase SQL editor (https://supabase.com/dashboard/projects)
create extension if not exists vector;

create table if not exists trade_memory (
    id            text primary key,
    pair          text not null,
    side          text not null,
    cycle_start   timestamptz not null,
    confidence    int,
    score         int,
    max_score     int,
    adx_h1        double precision,
    adx_h4        double precision,
    persistence   double precision,
    multi_tf      boolean,
    result_5h     text,
    move_pct_5h   double precision,
    features      vector(9) not null
);

create index if not exists trade_memory_features_ivf
    on trade_memory using ivfflat (features vector_cosine_ops)
    with (lists = 100);
"""


# ── feature engineering ────────────────────────────────────────────────


def feature_vector(forecast: dict, cycle_start: str) -> list[float]:
    """Deterministic 9-D feature vector for a single historical forecast."""
    confidence = float(forecast.get("confidence") or 0.0)
    score_abs = abs(float(forecast.get("score") or 0.0))
    max_score = float(forecast.get("max_score") or 0.0) or 1.0
    ratio = score_abs / max_score
    adx_h1 = float(forecast.get("adx_h1") or forecast.get("adx") or 0.0)
    adx_h4 = float(forecast.get("adx_h4") or 0.0)
    persistence = float(forecast.get("trend_persistence_5h") or 0.0)
    multi_tf = 1.0 if forecast.get("multi_tf_aligned") else 0.0
    side_raw = forecast.get("side")
    side = 1.0 if side_raw == "BUY" else (-1.0 if side_raw == "SELL" else 0.0)
    result = forecast.get("result_5h")
    win = 1.0 if result == "win" else 0.0
    return [
        confidence,
        score_abs,
        ratio,
        adx_h1,
        adx_h4,
        persistence,
        multi_tf,
        side,
        win,
    ]


def row_id(pair: str, cycle_start: str) -> str:
    """Stable id so re-runs upsert instead of inserting duplicates."""
    return f"{cycle_start}|{pair}"


def iter_evaluated_forecasts(state: dict) -> Iterable[tuple[str, dict]]:
    """Yield (cycle_start_utc, forecast_dict) for every evaluated forecast.

    A forecast is considered "evaluated" only when ``result_5h`` is set
    (so we always know whether it was a win or a loss).  Both finished
    history cycles and the current cycle (if some of its picks have
    already crossed the 5h horizon) are walked.
    """
    cycles: list[dict] = []
    cur = state.get("current")
    if isinstance(cur, dict):
        cycles.append(cur)
    cycles.extend(state.get("history", []) or [])
    for cyc in cycles:
        cycle_start = cyc.get("cycle_start_utc") or ""
        if not cycle_start:
            continue
        for f in cyc.get("selected", []) or []:
            if f.get("result_5h") in ("win", "loss"):
                yield cycle_start, f


def build_rows(state: dict) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for cycle_start, f in iter_evaluated_forecasts(state):
        pair = f.get("pair")
        if not pair:
            continue
        vec = feature_vector(f, cycle_start)
        if len(vec) != FEATURE_DIM:
            continue
        rows.append({
            "id": row_id(pair, cycle_start),
            "pair": pair,
            "side": f.get("side") or "",
            "cycle_start": cycle_start,
            "confidence": int(f.get("confidence") or 0),
            "score": int(f.get("score") or 0),
            "max_score": int(f.get("max_score") or 0),
            "adx_h1": float(f.get("adx_h1") or f.get("adx") or 0.0),
            "adx_h4": float(f.get("adx_h4") or 0.0),
            "persistence": float(f.get("trend_persistence_5h") or 0.0),
            "multi_tf": bool(f.get("multi_tf_aligned")),
            "result_5h": f.get("result_5h") or "",
            "move_pct_5h": float(f.get("move_pct_5h") or 0.0),
            "features": vec,
        })
    return rows


# ── supabase client (optional dependency) ──────────────────────────────


def _load_supabase_client():
    """Return a configured supabase client, or None if anything is missing."""
    url = os.getenv("SUPABASE_URL")
    key = os.getenv("SUPABASE_KEY") or os.getenv("SUPABASE_SERVICE_ROLE_KEY")
    if not url or not key:
        print(
            "[memory_index] SUPABASE_URL / SUPABASE_KEY are not set — skipping.",
            file=sys.stderr,
        )
        return None
    try:
        from supabase import create_client  # type: ignore
    except ImportError:
        print(
            "[memory_index] supabase-py is not installed — "
            "`pip install supabase` to enable. Skipping.",
            file=sys.stderr,
        )
        return None
    try:
        return create_client(url, key)
    except Exception as exc:  # pragma: no cover - network/SDK errors
        print(f"[memory_index] could not create supabase client: {exc}", file=sys.stderr)
        return None


def upsert_rows(client, rows: list[dict[str, Any]]) -> int:
    """Upsert ``rows`` in batches of 500. Returns number of rows pushed."""
    pushed = 0
    batch_size = 500
    for i in range(0, len(rows), batch_size):
        batch = rows[i:i + batch_size]
        try:
            client.table(TABLE).upsert(batch, on_conflict="id").execute()
            pushed += len(batch)
        except Exception as exc:  # pragma: no cover - network errors
            print(
                f"[memory_index] upsert batch {i}-{i + len(batch)} failed: {exc}",
                file=sys.stderr,
            )
    return pushed


# ── main ───────────────────────────────────────────────────────────────


def main() -> int:
    if not STATE_FILE.exists():
        print(
            f"[memory_index] {STATE_FILE.relative_to(ROOT)} not found — "
            "no history yet, skipping.",
        )
        return 0
    try:
        state = json.loads(STATE_FILE.read_text(encoding="utf-8") or "{}")
    except json.JSONDecodeError as exc:
        print(f"[memory_index] could not parse forecasts.json: {exc}", file=sys.stderr)
        return 0

    rows = build_rows(state)
    if not rows:
        print("[memory_index] no evaluated forecasts found — nothing to index.")
        return 0

    client = _load_supabase_client()
    if client is None:
        print(
            f"[memory_index] would have indexed {len(rows)} historical forecasts "
            "(supabase client not configured).",
        )
        print("\n--- One-time table bootstrap SQL ---")
        print(BOOTSTRAP_SQL)
        return 0

    pushed = upsert_rows(client, rows)
    print(
        f"[memory_index] upserted {pushed}/{len(rows)} historical forecasts into "
        f"`{TABLE}`.",
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
