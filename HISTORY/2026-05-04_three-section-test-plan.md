# Test Plan — PR #12 (3-section reorg + live-price + news-watch + system health)

URL: https://static-build-qumqktab.devinapps.com/
Backend (used by static-shim fallback): https://fxinvestment-nbmuknwe.fly.dev/

## What changed (user-visible)
1. Главная /intent теперь показывает **ровно 3 раздела**: СТАКАН (наверху) → СДЕЛКИ + Авто-сделки от СТАКАНА → СОСТОЯНИЕ СИСТЕМЫ. Старые разделы (final-signals, ai-narrative, live-analyst, daily-target, fx-controls, fx-grid, fx-deep) скрыты через `<div hidden style="display:none !important">`.
2. В шапке СТАКАНА появился pulse-индикатор `sk-pair-pulse` (зелёная мигающая точка) и live-обновление цены каждые 5 сек через новый endpoint `/api/live-price/{pair}`.
3. Под шапкой — баннер `sk-news-warning` (красный), показывается только когда `/api/news-watch/{pair}` возвращает `count > 0`.
4. Раздел СОСТОЯНИЕ СИСТЕМЫ показывает grid из 11 компонентов с зелёными/красными точками + age + PID.

## Primary flow (single end-to-end test)

### Step 1 — Page loads с правильной структурой
1. Открыть https://static-build-qumqktab.devinapps.com/ в Chrome (browser maximized).
2. Прокрутить страницу сверху вниз.
3. **Pass criteria:**
   - Видны ровно три заголовка `<h2>`: «📊 ОСНОВНОЙ ПРОГНОЗ — СТАКАН · 28 валют», «💼 СДЕЛКИ — открытые + история», «🩺 СОСТОЯНИЕ СИСТЕМЫ — все компоненты live»
   - НЕ видны: «28 ВАЛЮТ ФОРЕКС — ФИНАЛЬНЫЕ ПРОГНОЗЫ», «AI-АНАЛИТИК», «ЖИВОЙ АНАЛИТИК», «ДНЕВНОЙ ТАРГЕТ»
   - Нет 28 grid-карточек со страновыми флагами (старая `fx-grid`)
4. **Fail criteria:** если виден хотя бы один из старых заголовков → реорг сломан.
5. **Why this would fail if change broke:** until this PR, `final-signals-section` etc. рендерились видимо. Видимость — единственный наблюдаемый признак реорга.

### Step 2 — Pair selector + live-price update (5 сек cadence)
1. В разделе СТАКАН найти селектор пар (пилюли с тикерами). Кликнуть на **EURUSD**.
2. Дождаться появления цены в `#sk-pair-price` (формат `1.17XXX`).
3. Запомнить значение цены и timestamp в `sk-pair-pulse-txt` (формат `live HH:MM:SS`).
4. Подождать ~12 секунд (за это время должно произойти ≥2 обновления при cadence 5 сек).
5. **Pass criteria:**
   - Цена EUR/USD в диапазоне 1.16–1.18 (sanity check для интервала ноября 2025/мая 2026)
   - Timestamp в `sk-pair-pulse-txt` обновился (HH:MM:SS отличается от изначального)
   - В `sk-pair-pulse-delta` видны числа с подписями `1м` и `5м` (могут быть 0)
   - Pulse-точка зелёная и мигает (CSS animation `skPulse`)
6. **Fail criteria:** если цена `—` или timestamp не обновляется — live-loop не работает.
7. **Why this would fail if change broke:** до PR не было endpoint `/api/live-price/{pair}` и не было `sk-pair-pulse` элемента. Если loop не зарегистрирован, timestamp заморозится.

### Step 3 — Cross-check цены с внешним источником
Параллельно с шагом 2, в shell:
```
curl -sm 10 'https://fxinvestment-nbmuknwe.fly.dev/api/live-price/EURUSD' | python3 -m json.tool
```
**Pass criteria:**
- Возвращает `price` в диапазоне 1.16–1.18
- `source = "yahoo_1m"`
- разница с тем, что показано в UI, ≤ 0.0005 (5 пипсов; обычно ±2)
- Сравнить с независимым источником `yfinance` напрямую (Python). Разница ≤ 0.0003.

**Fail criteria:** если значение API ≠ значение в UI или > 5 пипсов от yfinance → проблема с pipe.

### Step 4 — System Health grid рендерит компоненты
1. Прокрутить вниз до раздела СОСТОЯНИЕ СИСТЕМЫ.
2. **Pass criteria:**
   - Виден grid из ≥6 карточек `.sh-card` (forecast_scanner, paper_trader, daily_planner, market_intel, etc.)
   - Каждая карточка содержит зелёную точку 🟢 либо красную 🔴, age (например `35s` / `2m`), PID (число)
   - В summary над grid видна строка вида `live · alive: N · 🔴 dead: M`
3. **Fail criteria:** grid пустой, либо `не удалось получить /api/health`.
4. **Why this would fail if broke:** функция `refreshSystemHealth` — новая, до PR не было `sh-grid` элемента. Если функция не вызывается, остаётся placeholder.

### Step 5 — News-warning conditional rendering
1. В Chrome DevTools Network → filter `news-watch` → найти запрос `/api/news-watch/EURUSD?hours_ahead=5`.
2. Открыть response, посмотреть `count`.
3. **Pass criteria:**
   - Если `count == 0` — баннер `#sk-news-warning` имеет атрибут `hidden` (не виден на странице)
   - Если `count > 0` — баннер виден, текст начинается с `⚠️ через ≤5ч выходит N high-impact событий`
4. **Fail criteria:** баннер виден когда `count == 0`, или скрыт когда `count > 0`.
5. **Why this would fail if broke:** до PR не было `sk-news-warning` элемента. Если refreshNewsWatch не учитывает count, баннер либо всегда виден, либо никогда.

### Step 6 — Авто-сделки от СТАКАНА (под-секция в СДЕЛКИ)
1. В разделе СДЕЛКИ прокрутить до подзаголовка «🤖 Авто-сделки от СТАКАНА».
2. **Pass criteria:**
   - Виден pill `mt-stk-pill` со статистикой (например `WR · X% · trades · N`) ИЛИ `…` если данных нет
   - Видны две таблицы: «открытые» и «закрытые» (могут быть пустыми с placeholder `—`)
3. **Fail criteria:** под-секция отсутствует.

## Out of scope (НЕ тестируем в этом раунде)
- 70% WR на всех 28×4 ячейках — это open follow-up из PR description, требует отдельного forecast redesign
- Auto-close trade при появлении high-impact news — UI warning есть, авто-закрытие не реализовано
- TradingView native integration — используем Yahoo как surrogate (≤3 пипса разница)
- Backtest accuracy — out of scope

## Recording plan
Один непрерывный recording (~2 мин):
1. annotate setup: «Opening static mirror dashboard»
2. test_start: «It should show exactly 3 sections (Стакан, Сделки, Состояние)»
3. assertion: result of Step 1
4. test_start: «It should update EUR/USD price every 5 sec via live-pulse»
5. assertion: precondition (selected EURUSD shows price + pulse), then assertion after 10 sec wait
6. test_start: «It should match external Yahoo source within 5 pips»
7. assertion: result of Step 3 (with terminal evidence)
8. test_start: «It should render system health grid with component status»
9. assertion: result of Step 4
10. test_start: «It should hide news-warning when count is 0»
11. assertion: result of Step 5
