# 2026-05-01 — market_hours + pre-emptive stability forecast

## Дословная цитата пользователя

> на основном стратегии должен быть так что система должна найти на стакане
> уровень так как же и в 2 стратегию тут такая же будет логика но тут будет
> так от 1 часа до 5 часов будет потому что 2 старания лучше работает раз
> за полуллос теперь и это будет на оба стратегии только теперь на основном
> стратегии будет такая же логика как и на втором стратегии с дыркой будет
> от 1 часа до 5 часов ещё система должен понимать когда рынок Форекс будет
> открыт и когда будет закрыться у меня время utc 5+ и когда рынок закрыт
> система больше не должна открывать сделки Например если он открывает
> Зеркальное 2 часа если через 1 час рынок будет закрыта То он должен
> понимать что на 2 часа открыть невозможно … Я хочу чтобы стабильность
> была очень хорошим Я понимаю что у нас сейчас мало сделок но я хочу
> чтобы ты прямо сейчас проверил все валюты все сессии за год …
> Я хочу чтобы ты добавил такую систему которая не зависит от сколько мы
> открыли сделок он заранее анализировал Как работает вообще система
> И заранее сказал что нам стоит ожидать честно

## Что сделано

### 1. `teamagent/market_hours.py` (новый, 195 строк)

Полностью dependency-free модуль (только `datetime`):

- `is_market_open(at)` — Forex Sun 22:00 UTC → Fri 22:00 UTC
- `next_close(at)` / `next_open(at)` — следующий момент закрытия/открытия
- `seconds_until_close(at)` / `seconds_until_open(at)`
- `current_session(at)` — Asia / London / Overlap / NY / Closed (UTC часы)
- `max_safe_expiry_hours(at, min_buffer_minutes=15)` — макс. expiry в
  целых часах что ещё успеет settle до закрытия рынка с буфером
- `clip_expiry_hours(desired, at, buffer_min=15)` — обрезает желаемую
  экспирацию (или возвращает 0 если "слишком близко к закрытию")
- `market_status(at)` — снапшот для дашборда (emoji, status_text,
  countdown, max_safe_expiry_h)

### 2. Интеграция в обе основные стратегии + daily

**`paper_trader.py`** (главная стратегия):
- импорт `market_hours as mh`
- Adaptive expiry клампится в `[1..5h]` (было `[1..4h]` фиксировано
  через config). Конкретное значение всё ещё выбирается стратегией
  (best_variant.fixed_expiry_h или forecast.recommended_hours).
- Перед открытием: `if not mh.is_market_open(now): return 0`
- Перед открытием: если `max_safe_expiry_hours < 1h` — пропуск
- Каждый трейд: `recommended = mh.clip_expiry_hours(recommended, now, 15)`
  — если 0, пропускаем (рынок закрывается)

**`paper_trader_stakan.py`**:
- `MAX_EXPIRY_H` снижен с **20 → 5** (единый диапазон с основной стратегией)
- Market gate перед сканом 28 пар
- Per-trade clip до закрытия рынка

**`paper_trader_daily.py`**:
- Market gate (буфер 30 мин т.к. daily может идти 18ч)
- Per-trade clip; если 0 — `skip_reason: "market_closing"`

### 3. `teamagent/stability_forecast.py` (новый, 295 строк)

**Главная фича по запросу пользователя.** Pre-emptive прогноз стабильности
**не зависит от количества закрытых сделок**. Ответ есть, даже если
сделок вообще нет.

Формула:
```
expected_wr_window = Σ over (session ∈ window):
    qualified_cells_wr_in_session × hours_in_window_in_session
                       / total_active_hours
```

Источники данных (всё реальное):
- `state/strategy_config_locked.json` — 365-day Yahoo backtest по каждой
  ячейке (pair × session)
- `market_hours` — какие часы окна реально активны
- Wilson 95% CI считается по суммарному `backtest_trades_weight`,
  даёт честные нижние/верхние границы

Дополнительные сигналы:
- `forecasts_eligible_now` — сколько прогнозов проходят 70%-гейт прямо сейчас
- `active_qualified_pairs_count` — пары прошедшие 70% WR на 365д
- `readiness_score_0_100` — взвешенная оценка готовности системы
  (0.30 × Wilson lower + 0.25 × WR + 0.20 × qualified + 0.10 × eligible
  + 0.10 × active_ratio + 0.05 × market_open)
- Русский диагноз и рекомендации

### 4. API endpoints в `teamagent/dashboard/server.py`

- `GET /api/market-status` — снапшот market_hours
- `GET /api/stability-forecast?hours_ahead=N` — N от 1 до 168 (7 дней)

### 5. UI на дашборде (`index.html` + `app.js` + `style.css`)

Две новые hero-секции **в самом верху страницы**:

**FOREX ОТКРЫТ/ЗАКРЫТ** — большая карточка с тремя ячейками:
- Обратный отсчёт `00:47:04` (тикает каждую секунду через `setInterval(tickClock, 1000)`)
- Время пользователя в UTC+5 + UTC
- Безопасный max expiry прямо сейчас

**ПРЕДВАРИТЕЛЬНЫЙ ПРОГНОЗ СТАБИЛЬНОСТИ** — три карточки 1ч / 6ч / 24ч:
- Ожидаемый WR + 95% CI + verdict emoji
- Progress bar готовности 0..100
- Метрики qualified / eligible / активных часов в окне
- Русский диагноз и рекомендации внизу

Палитра остаётся фиолетовой SEO-AI (`#a78bfa` + `#0a0612`),
mobile-perf overrides сохранены — никаких новых лагов.

Все fetch'и идут через хелпер `api()` который использует `location.origin`
вместо relative URL — иначе credentials в URL ломают fetch (Chrome 100+).

### 6. Тесты

- `tests/test_market_hours.py` — **44 теста**, проверяют все границы
  Sun 22:00 / Fri 22:00 / Saturday closed / clip_expiry / sessions
- `tests/test_stability_forecast.py` — **9 тестов**, проверяют структуру,
  CI sanity, идемпотентность, независимость от # сделок

Полный прогон: `81 passed in 1.26s`.

## Текущее состояние системы

- **Время теста:** Fri 2026-05-01 21:12 UTC
- **Forex статус:** ОТКРЫТ, до закрытия 47 мин, сессия NY
- **Безопасный max expiry прямо сейчас:** 0ч (<1h + 15min buffer) →
  все три стратегии **не открывают новые сделки** до Sun 22:00 UTC
- **Прогноз на 24ч:** 75.0% expected WR, 95% CI [64.8%; 83.0%],
  СРЕДНЯЯ ГОТОВНОСТЬ 69/100 — потому что 23/24ч выходные
- **Активных qualified пар:** 12/28 (на 365д back-test)
- **Forecasts проходящих 70%-гейт прямо сейчас:** 4
- Все 81 теста pass

## Открытые TODO (P1, на следующую сессию)

1. **`year_session_pair_reactivity.py`** — для каждой пары × сессии × 365д
   Yahoo посчитать реакцию на:
   - ForexFactory high-impact news (есть/нет → разница в WR)
   - COT positioning extremes (CFTC weekly z-score)
   - FRED macro releases (CPI / NFP / FOMC через `fundamentals.py`)
   - Best-entry UTC hour для этой пары в этой сессии
   - Output: `state/year_reactivity.json`

2. **Интеграция year_reactivity в `forecast_scanner.evaluate_pair()`** —
   тилты по best_entry_hour, news_correlation, COT contrarian.

## Что НЕ менялось (важно для пользователя)

- 70%-гейт сохранён (probability_pct ≥ 70 → trade)
- Stake $1, payout 85% — без изменений
- Martingale 1→2→4 — без изменений
- Палитра + мобильная оптимизация (Phase 1) — без изменений
- Никаких симуляторов / случайных данных
- PROGNOZY-28 остаётся единым source of truth

## Дашборд

URL без изменений (тот же туннель этой сессии):
https://user:887aa255f3ab82c2c39d73f3e1702037@082b75f2888a-tunnel-v9e3vq29.devinapps.com/

Скриншот UI с обратным отсчётом + прогнозом — приложен к PR #1.
