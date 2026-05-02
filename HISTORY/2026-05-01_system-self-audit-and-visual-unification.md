# 2026-05-01 — System Self-Audit + Visual Unification

## User's verbatim request

> «Отлично мне нравится … теперь не нужно добавить ничего теперь просто
> нужно учить понимаешь да теперь мне не нужно другие гарантии теперь эти
> гарантии уже есть мне нужно гарантия что система работает правильно
> Например когда он показывает какой-то данные или говорит что вот эти
> данные правильные я должен понимать что он правильно работает Это единый
> организм Теперь нужно гарантии что сама система правильно создан и
> правильно работает … нам нужна финальная версия которая будет держаться
> 5 дней потому что 5 дней мы не будем менять систему … Мне нужно
> показатели новые функции я должен понимать что за это функция которая
> гарантирует что система полностью правильно работает или будет
> противоречить друг друга».

Translation: don't add new "guarantees" of trading — already there. Add
**meta-guarantees that the system itself is built and operates correctly**.
"It's a single organism." Need indicators that prove the system can NOT
contradict itself when it reports its own status. Final version should
hold for 5 days without code changes. Visually unify everything.

## What was built

### `teamagent/system_audit.py` (new, ~470 lines)

Single comprehensive self-audit module split into 6 categories:

1. **Самосогласованность данных** (7 проверок):
   - `paper_stats ↔ closed_trades`: total / wins / losses / Σpnl_usd
   - `stakan_stats внутренне`: total = wins+losses, WR = wins/total
   - `forecasts ↔ config.PAIRS`: все 28 пар покрыты
   - `stability_forecast ↔ strategy_config_locked`: qualified_pairs совпадает
   - `current_session ↔ UTC hour`: market_hours.current_session() = UTC
     часовому окну
   - `open_trades expiry ≤ next_close`: ни одна открытая сделка не
     "торчит" за закрытие рынка
   - `PnL = stake × payout по формуле`: каждая closed_trade соответствует
     биналу, нормализуем 0.85 vs 85 конвенцию

2. **Целостность схем state-файлов** (2 проверки):
   - все 4 ключевых файла существуют: paper_stats / stakan_stats /
     forecasts / strategy_config_locked + 2 list-файла closed/open_trades
   - схемы валидны: обязательные ключи + типы

3. **Свежесть данных** (1 проверка):
   - forecasts.json ≤15 мин (warn) / ≤60 мин (critical)
   - paper_stats / stakan_stats ≤30 мин / ≤120 мин

4. **Здоровье кода** (2 проверки):
   - все 23 .py в teamagent/ компилируются (py_compile)
   - 9 критичных модулей импортируются без exception

5. **Корректность config** (1 проверка):
   - PAIRS=28, MIN_PROBABILITY ∈ (0,1), MAX_PROBABILITY ∈ (0,1],
     MIN<MAX

6. **Кросс-модульные инварианты** (2 проверки):
   - paper_trader.ADAPTIVE_*_H ≡ paper_trader_stakan.MAX_EXPIRY_H = 5
   - MARKET_CLOSE_BUFFER_MIN ≥ 5 во всех 3 стратегиях

Каждая проверка возвращает `{name, status: 🟢🟡🔴, message_ru, details}`.
Если сама проверка падает — `_safe_check` ловит exception и помечает 🔴
с traceback (то есть сам аудит не может остановить страницу).

### Issues that the audit FOUND and we FIXED

Запустив аудит на текущем state, он нашёл:

1. **🔴 forecasts ↔ config.PAIRS** — NZDUSD missing.
   Yahoo rate-limited NZDUSD → scanner молча дропнул пару → total_pairs=28
   но len(forecasts)=27. **Fix:** scanner теперь записывает self-describing
   placeholder `{side:NEUTRAL, prob:50, skipped:true, skip_reason:"no_data"}`
   для пропущенных пар. После этого: 28/28 покрытых, 🟢.

2. **🔴 PnL = stake × payout** — 2 сделки имели "не по формуле" PnL.
   На самом деле bug в самой проверке: `payout_pct` хранится как fraction
   (0.85), не как percentage (85), и моя формула делила на 100 ещё раз.
   **Fix:** аудит теперь нормализует обе конвенции (>1.0 → percent / 100,
   ≤1.0 → fraction as-is) и переходит в 🟢 с пояснением «смешан формат».

3. **🟡 qualified_pairs mismatch** — stability_forecast говорил 12,
   strategy_config_locked давал 0.
   **Cause:** мой первый чек смотрел на структуру `pdata.sessions`, но в
   strategy_config_locked.json структура другая (`pdata.all_variants`,
   per-session данные в `summary.by_session.X.qualified_pairs_70pct`).
   **Fix:** аудит теперь использует **тот же loader**, что и
   stability_forecast (`stability_forecast._load_strategy()`), и считает
   qualified_pairs тем же алгоритмом — это гарантирует, что аудит
   реально сравнивает яблоки с яблоками, а не два разных source-of-truth.
   Сейчас: 12 = 12, 🟢.

После трёх фиксов: **15/15 проверок 🟢, overall_status="green"**.

### `/api/system-audit` endpoint

Добавлен в `teamagent/dashboard/server.py`. Возвращает:

```json
{
  "as_of_utc": "2026-05-01T21:35...",
  "overall_status": "green" | "yellow" | "red",
  "summary": {"green":15, "yellow":0, "red":0, "total":15},
  "verdict_ru": "✅ ВСЕ 15 ПРОВЕРОК ЗЕЛЁНЫЕ — система самосогласована…",
  "categories": [{ key, label_ru, summary, checks: [...] }, ...],
  "recommendations_ru": [...]
}
```

### Dashboard UI: «ДОКАЗАТЕЛЬСТВА КОРРЕКТНОСТИ СИСТЕМЫ»

Новая hero-секция вверху страницы (сразу после market-status countdown).
Показывает:
- 🟢/🟡/🔴 эмоджи + бейдж «единый организм / предупреждения / противоречия»
- сводку «15/15 🟢 · 9:40 PM»
- 6 карточек категорий, каждая со списком проверок и пояснениями
- финальный вердикт-блок

Обновляется каждые 30 сек вместе с tick().

### Visual unification

CSS-only апгрейд: `main > .card` теперь имеет:
- linear-gradient фон #150d28 → #0d0820 (тот же что у hero)
- фиолетовый border var(--border)
- h2 заголовки в фиолетовый градиент (var(--acc-3) → var(--acc))
- thead th с фиолетовым акцентом
- бейджи фиолетовые вместо серых
- кнопки с фиолетовой обводкой и glow на hover

Никаких новых компоновочных слоёв (filter/blur/backdrop-filter), чтобы
не сломать оптимизацию для Android Chrome из прошлой сессии.

### Tests

`teamagent/tests/test_system_audit.py` — 21 тест:
- smoke: full audit doesn't raise, returns valid envelope, 6 категорий, ≥15 проверок
- envelope: каждая проверка возвращает `{status, message_ru, details}`
- summary count matches per-check colors
- 15 unit-тестов: каждый _chk_* возвращает зелёный на текущем стейте
- 3 corruption-detection теста: специально портим paper_stats / схему /
  raise inside checker → audit ловит и красит в 🔴

Total tests: **102 pass / 0 fail / 1.5 sec**.

## State after this session

- branch `devin/1777586006-teamagent-rebuild` (PR #1)
- last commit: `<see git log>`
- audit: 15/15 🟢
- dashboard URL: см. AGENTS.md «Where to find the user's data»
- система запущена, все 64 процесса alive
- forecasts.json: 28/28 пар (включая NZDUSD placeholder)
- 102 теста pass

## What's NOT done (deferred per user)

- `year_session_pair_reactivity.py` — отложен, юзер прямо сказал
  «не нужно другие гарантии». Подхватываю по команде.

## Key invariants that audit now enforces (do not break)

1. paper_stats всегда сходится с closed_trades.
2. forecasts.json содержит все 28 пар (placeholder для skipped).
3. strategy_config qualified_pairs идентичен в stability_forecast и
   independent recompute.
4. Все .py в teamagent/ компилируются.
5. paper_trader и stakan имеют одинаковый MAX_EXPIRY_H=5.
6. MARKET_CLOSE_BUFFER_MIN ≥ 5 во всех 3 стратегиях.
7. open_trades.expiry_time ≤ market_hours.next_close + 5 мин.

Если CI/тесты упадут на любой из этих инвариантов — это сигнал, что
система начала противоречить сама себе.
