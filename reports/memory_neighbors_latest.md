# 🧠 Память аналогов из Supabase pgvector
_Авто-генерируется `scripts/memory_query.py` — каждое 5-часовое окно ищет K=10 ближайших исторических сетапов по косинусному расстоянию, и считает сколько из них завершились победой._

> ⚠ Память пропущена: нет текущего цикла в `state/forecasts.json`.

Скрипт продолжит работу как только переменные `SUPABASE_URL` и `SUPABASE_KEY` будут настроены в репо-секретах и таблица `trade_memory` создана. Бесплатный тариф на 500 МБ Postgres: <https://supabase.com/dashboard/projects>.

<details><summary>SQL для серверного KNN (опционально)</summary>

```sql
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
```
</details>
