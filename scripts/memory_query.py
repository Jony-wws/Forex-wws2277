"""Supabase pgvector memory query — find K nearest historical setups.

Companion to ``scripts/memory_index.py``.  After every 5h cycle (or on
its cron fallback, see ``.github/workflows/memory_query.yml``) this
script:

1. Reads the **current** cycle from ``state/forecasts.json`` (the
   ``current.selected`` list written by ``app/cycle.py``).
2. For each currently-selected pair, builds the same 9-D deterministic
   feature vector used by ``memory_index.py`` (with the win/loss slot
   set to 1.0 — i.e. "what would a perfect winner look like?") and asks
   Supabase ``pgvector`` for the **K = 10 nearest historical setups by
   cosine distance**.
3. Counts how many of those neighbours actually won (``result_5h ==
   'win'``) and writes a markdown summary to
   ``reports/memory_neighbors_latest.md`` — for example::

       ## EURUSD BUY  →  7 / 10 historical analogs were winners

Both ``scripts/ai_review.py`` and ``scripts/ai_patcher.py`` then inject
that markdown into their LLM prompt, so the model sees concrete
historical analogs of the live trade.

The script is **deliberately defensive** — it skips silently with a
printed warning rather than crashing whenever:

* ``supabase-py`` is not installed,
* ``SUPABASE_URL`` / ``SUPABASE_KEY`` env vars are missing,
* ``state/forecasts.json`` does not exist or has no current cycle yet.

In every "skip" path we still write a non-empty
``reports/memory_neighbors_latest.md`` explaining *why* there are no
neighbours, so downstream readers can rely on the file existing.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
STATE_FILE = ROOT / "state" / "forecasts.json"
REPORTS = ROOT / "reports"
REPORTS.mkdir(exist_ok=True)
OUTPUT = REPORTS / "memory_neighbors_latest.md"

K = 10
TABLE = "trade_memory"
RPC_NAME = "match_trade_memory"

# 9-D vector identical to memory_index.py.  We deliberately set the
# win-flag slot to 1.0 — we are asking "find historical winners that
# look like the current setup", not "find any historical match".
FEATURE_DIM = 9


def feature_vector_for_current(forecast: dict) -> list[float]:
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
    return [confidence, score_abs, ratio, adx_h1, adx_h4, persistence, multi_tf, side, 1.0]


# ── supabase client ────────────────────────────────────────────────────


def _load_client():
    url = os.getenv("SUPABASE_URL")
    key = os.getenv("SUPABASE_KEY") or os.getenv("SUPABASE_SERVICE_ROLE_KEY")
    if not url or not key:
        return None, "SUPABASE_URL / SUPABASE_KEY are not set"
    try:
        from supabase import create_client  # type: ignore
    except ImportError:
        return None, "supabase-py is not installed (pip install supabase)"
    try:
        return create_client(url, key), None
    except Exception as exc:  # pragma: no cover
        return None, f"could not create supabase client: {exc}"


def _query_neighbours(client, vector: list[float], k: int) -> list[dict[str, Any]]:
    """Return the K nearest historical rows by cosine distance.

    We try a server-side RPC first (fastest, lets pgvector do the work)
    and fall back to a SELECT over the whole table when the RPC is not
    deployed yet — fine for the free-tier table sizes we expect.
    """
    # 1. RPC path — requires the user has deployed the SQL function in
    #    docs (see RPC_SQL below).
    try:
        res = client.rpc(RPC_NAME, {"query": vector, "match_count": k}).execute()
        rows = getattr(res, "data", None) or []
        if rows:
            return list(rows)
    except Exception as exc:
        print(f"[memory_query] RPC `{RPC_NAME}` not available ({exc}); falling back to SELECT.", file=sys.stderr)

    # 2. Fallback path — pull the whole table and rank in Python.  This
    #    is fine while the memory is small (free tier, 60 cycles ≈ 300
    #    rows).  The RPC path takes over once the user runs the SQL.
    try:
        res = client.table(TABLE).select(
            "id,pair,side,cycle_start,confidence,result_5h,move_pct_5h,features"
        ).execute()
        rows = getattr(res, "data", None) or []
    except Exception as exc:
        print(f"[memory_query] fallback select failed: {exc}", file=sys.stderr)
        return []

    scored: list[tuple[float, dict[str, Any]]] = []
    for row in rows:
        feat = row.get("features")
        if isinstance(feat, str):
            # supabase returns vector as a "[1,2,3]" string
            try:
                feat = json.loads(feat)
            except json.JSONDecodeError:
                continue
        if not isinstance(feat, list) or len(feat) != FEATURE_DIM:
            continue
        scored.append((_cosine_distance(vector, feat), row))
    scored.sort(key=lambda t: t[0])
    return [r for _, r in scored[:k]]


def _cosine_distance(a: list[float], b: list[float]) -> float:
    import math
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 1.0
    return 1.0 - dot / (na * nb)


# SQL for the optional server-side RPC — printed in the markdown report
# so the user can paste it into the Supabase SQL editor for faster KNN.
RPC_SQL = """\
-- Optional: deploy this function in the Supabase SQL editor to make
-- KNN run server-side (faster than the Python fallback).
create or replace function match_trade_memory(query vector(9), match_count int)
returns table (
    id            text,
    pair          text,
    side          text,
    cycle_start   timestamptz,
    confidence    int,
    result_5h     text,
    move_pct_5h   double precision,
    features      vector(9),
    distance      double precision
) language sql stable as $$
    select id, pair, side, cycle_start, confidence, result_5h, move_pct_5h,
           features,
           features <=> query as distance
    from trade_memory
    order by features <=> query
    limit match_count;
$$;
"""


# ── markdown rendering ─────────────────────────────────────────────────


def _summarise_neighbours(rows: list[dict[str, Any]]) -> tuple[int, int, list[str]]:
    """Return (wins, total, bullet_lines)."""
    bullets: list[str] = []
    wins = 0
    for r in rows:
        if r.get("result_5h") == "win":
            wins += 1
        bullets.append(
            "- `{pair}` {side} · {ts} · conf {conf} · {result} ({move:+.2f}%)".format(
                pair=r.get("pair") or "?",
                side=r.get("side") or "?",
                ts=(r.get("cycle_start") or "?")[:16],
                conf=r.get("confidence") or 0,
                result=r.get("result_5h") or "?",
                move=float(r.get("move_pct_5h") or 0.0),
            )
        )
    return wins, len(rows), bullets


def render_markdown(
    sections: list[str],
    skipped_reason: str | None = None,
) -> str:
    parts: list[str] = []
    parts.append("# 🧠 Память аналогов из Supabase pgvector")
    parts.append(
        "_Авто-генерируется `scripts/memory_query.py` — каждое 5-часовое "
        "окно ищет K=10 ближайших исторических сетапов по косинусному "
        "расстоянию, и считает сколько из них завершились победой._",
    )
    if skipped_reason:
        parts.append("")
        parts.append(f"> ⚠ Память пропущена: {skipped_reason}")
        parts.append("")
        parts.append(
            "Скрипт продолжит работу как только переменные `SUPABASE_URL` и "
            "`SUPABASE_KEY` будут настроены в репо-секретах и таблица "
            "`trade_memory` создана. Бесплатный тариф на 500 МБ Postgres: "
            "<https://supabase.com/dashboard/projects>."
        )
    if sections:
        parts.append("")
        parts.extend(sections)
    parts.append("")
    parts.append("<details><summary>SQL для серверного KNN (опционально)</summary>\n\n```sql\n"
                 + RPC_SQL + "```\n</details>")
    return "\n".join(parts) + "\n"


# ── main ───────────────────────────────────────────────────────────────


def _load_state() -> dict | None:
    if not STATE_FILE.exists():
        return None
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8") or "{}")
    except json.JSONDecodeError:
        return None


def main() -> int:
    state = _load_state()
    if not state or not state.get("current"):
        OUTPUT.write_text(
            render_markdown([], skipped_reason="нет текущего цикла в `state/forecasts.json`."),
            encoding="utf-8",
        )
        print("[memory_query] no current cycle — wrote placeholder report.")
        return 0

    current = state["current"]
    selected: list[dict] = current.get("selected", []) or []
    if not selected:
        OUTPUT.write_text(
            render_markdown([], skipped_reason="в текущем цикле нет выбранных пар."),
            encoding="utf-8",
        )
        print("[memory_query] current cycle has no picks — wrote placeholder report.")
        return 0

    client, err = _load_client()
    if client is None:
        OUTPUT.write_text(
            render_markdown([], skipped_reason=err or "Supabase недоступна."),
            encoding="utf-8",
        )
        print(f"[memory_query] supabase unavailable: {err} — wrote placeholder report.")
        return 0

    sections: list[str] = []
    cycle_start = current.get("cycle_start_utc") or "?"
    sections.append(f"## Текущий цикл `{cycle_start}`")
    sections.append("")

    for f in selected:
        pair = f.get("pair") or "?"
        side = f.get("side") or "?"
        vec = feature_vector_for_current(f)
        rows = _query_neighbours(client, vec, K)
        wins, total, bullets = _summarise_neighbours(rows)
        if total == 0:
            sections.append(
                f"### {pair} {side}\n\nНет исторических аналогов в `trade_memory` пока.\n"
            )
            continue
        sections.append(
            f"### {pair} {side}  →  **{wins}/{total}** исторических аналогов оказались победителями"
        )
        sections.extend(bullets)
        sections.append("")

    OUTPUT.write_text(render_markdown(sections), encoding="utf-8")
    print(f"[memory_query] wrote {OUTPUT.relative_to(ROOT)} for {len(selected)} pairs.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
