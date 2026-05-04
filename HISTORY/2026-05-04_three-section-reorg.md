# 2026-05-04 — 3-section reorg + 5-sec live price + news-watch + system health

## What the user asked (verbatim, abridged)

> "Отлично прямо сейчас я хочу чтобы ты убрал все ненужные разделы и оставил
> только историю и стакан ордеров... только три раздела основной прогнозы где
> будет стакан и второй состояние системы... система сам автоматически должен
> открывать на своём систему когда он показывает какой-то прогноз... текущая
> цена обновился каждые пять секунд... реальные данные из tradingwiew...
> предупреждение если выходит какая-то новость или данные он должен
> понимать что это данные мне будет мешать... 70% win rate и 3 прогноз каждый
> день."

## What was done

### 1. Reorg /intent landing → ONLY 3 sections
- **Раздел 1 (top):** ОСНОВНОЙ ПРОГНОЗ — СТАКАН (was Раздел 2 in PR #12, now first)
  - 28-pair selector
  - 24h-bias hero (8-source ensemble)
  - 1–5h main forecast hero (with no-return level)
  - Buyers vs Sellers hero (volume-profile + big-players)
  - Order book + big players grid
  - Per-session strategy table
  - **NEW:** sk-pair-pulse — анимированная индикация live-price (зелёная на росте, красная на падении), показывает 1м/5м pip-delta
  - **NEW:** sk-news-warning — красный banner если в горизонте ≤5ч есть high-impact событие (ForexFactory)
- **Раздел 2:** СДЕЛКИ — открытые + история + WR (existing main-trades-section)
  - **NEW sub-section:** Авто-сделки от СТАКАНА (paper_trader_stakan) — отдельная статистика, открытые/закрытые с live-PnL
- **Раздел 3 (NEW):** СОСТОЯНИЕ СИСТЕМЫ — health/heartbeat dashboard
  - 11 компонентов (forecast_scanner, paper_trader, paper_trader_stakan, market_radar, paper_trader_daily, orchestrator, watchdog, backtester, state_committer, strategy_search, strategy_meta_agent)
  - Зелёный/красный статус, age в секундах/минутах, PID
  - Сводка: всего/живы/мёртвы/open trades (paper)/open trades (стакан)/закрытые
  - Warnings card если есть мёртвые компоненты

### 2. Hidden (not deleted) legacy sections
- `final-signals-section`, `ai-narrative-section`, `live-analyst-section`,
  `daily-target-section`, `fx-controls`, `fx-grid`, `fx-deep` — обёрнуты в
  `<div id="legacy-hidden-sections" hidden style="display:none !important">`.
  DOM intact, JS-инициализация не падает, пользователь не видит. Easy revert.

### 3. New endpoints
- **`GET /api/live-price/{pair}`** — лёгкий ticker для 5-сек refresh.
  Возвращает `{pair, price, change_1m_pips, change_5m_pips, change_1h_pips, ts, source, bar_time}`.
  In-process TTL cache 3 сек защищает Yahoo от спама при N клиентах × 5-сек polling.
- **`GET /api/news-watch/{pair}?hours_ahead=5`** — predicted high-impact события.
  Возвращает `{pair, hours_ahead, count, events[], warning}`. Использует
  существующий `data.news.upcoming_high_impact()` (ForexFactory RSS).

### 4. Frontend refresh cadence
- **Live price (current pair):** каждые 5 сек ← новое
- **News-watch (current pair):** каждые 60 сек (RSS-кэш 15 мин anyway)
- **Stakan view (полный):** каждые 10 сек ← unchanged
- **System health:** каждые 5 сек ← новое
- **Авто-сделки от стакана:** каждые 10 сек ← новое

### 5. Cross-check (per user request)
EUR/USD проверка:
- `/api/live-price/EURUSD` → 1.17041 (Yahoo 1m)
- `yfinance` напрямую (`EURUSD=X`) → 1.17041 ✅
- `api.exchangerate-api.com` (free, no key) → EUR→USD 1.17 ✅
- Bar time: `2026-05-04 11:18:00+00:00` (latest 1-минута UTC)

### 6. Static mirror + Fly redeploy
- Static mirror: https://static-build-qumqktab.devinapps.com (rebuilt with new endpoints baked)
- Fly URL ROTATED: old `lbtxlhtb` → new `nbmuknwe` (Fly free-tier rotates on every machine recreate)
  - New canonical: https://fxinvestment-nbmuknwe.fly.dev/
- Updated AGENTS.md + .agents/skills/fly-deploy/SKILL.md + static-shim.js to point to new URL.

## Files changed

- `teamagent/dashboard/static/intent.html` — reorder + 3 sections + hidden wrapper
- `teamagent/dashboard/static/intent.js` — +5-sec live-price loop, +5-sec health loop, +10-sec stakan auto-trades loop, +news-watch
- `teamagent/dashboard/static/intent.css` — sk-pair-pulse, sk-news-warning, sh-grid, mt-stakan styles
- `teamagent/dashboard/static/static-shim.js` — new Fly URL
- `teamagent/dashboard/server.py` — `/api/live-price/{pair}`, `/api/news-watch/{pair}`
- `scripts/build_static_mirror.sh` — bake live-price/news-watch
- `AGENTS.md`, `.agents/skills/fly-deploy/SKILL.md` — new Fly URL

## NOT done (yet — open follow-ups)

User asked for several things that are bigger scope:

1. **70% WR на ВСЕХ 28 парах × 4 сессиях** — текущий потолок ~36/112 cells на
   реальном 365-дневном Yahoo. Без снижения порога / без симулятора это потолок.
   PR #13 **не меняет** forecast_scanner/paper_trader логику; стакан остаётся
   secondary signal. Если user хочет — следующий шаг: order-book-centric
   forecast (buyers % > 60% → BUY, < 40% → SELL, else neutral) + intersection
   gate с 24h-bias.
2. **3+ прогноза/день/пара** — paper_trader открывает по probability ≥ 70%
   независимо от частоты; в этой сессии не трогали. Если нужно ровно 3+ —
   надо добавить per-pair daily counter и forced re-evaluation.
3. **Real TradingView API** — текущий источник Yahoo Finance 1m. TradingView
   публичного API не имеет; альтернативы: Twelve Data (free 800 req/day),
   Alpha Vantage (5/min). Не интегрировано — Yahoo точен до 0.0001 vs
   ECB/exchangerate-api.com.
4. **Auto-close on news mid-trade** — paper_trader пока сам не закрывает
   на high-impact news. Добавили только UI warning перед открытием. Логику
   "auto-close при появлении news" надо добавить в paper_trader.tick_loop.

## Open TODO для следующей сессии

- [ ] Order-book-centric forecast в forecast_scanner (новый primary signal)
- [ ] Per-pair daily quota: ровно 3 forecasts/день/пара
- [ ] Auto-close при появлении high-impact news mid-trade
- [ ] Twelve Data fallback для cross-verify Yahoo prices
