# 2026-05-02 — FX INVESTMENT cinematic Market-Intent landing

## Что сделано
Запрос: «отдельные окна на сайте наглядно где видно что хотят сделать игроки рынка прямо сейчас на каждой валюте, визуально как TradingView, обновление каждые 10с, для обоих стратегий, без лагов на телефоне». Бренд → **FX INVESTMENT**.

Реализовано как новая главная `/intent` (она же `/`) поверх существующего бэкенда. Старый системный дашборд переехал на `/system`.

### Файлы
- `teamagent/dashboard/static/intent.html` — каркас FX INVESTMENT (header / controls / grid / dialog).
- `teamagent/dashboard/static/intent.css` — cinematic dark UI: aura-фон, мобильный grid (1→2→3→4 col), pressure-bar, chart-контейнер 110/130px.
- `teamagent/dashboard/static/intent.js` — клиент: fetch 7 эндпоинтов параллельно каждые 10с, расчёт pressure-score (0..100), strength-heatmap 8 валют, рендер 28 карточек с lightweight-charts area-серией, deep-dive модалка с микроструктурой.
- `teamagent/dashboard/server.py`:
  - `/` теперь отдаёт `intent.html` (новый landing).
  - `/intent` — алиас.
  - `/system` — старый аудит-дашборд (`index.html`).
  - `/api/forecasts` теперь включает `current_price` + сжатый набор indicators (4H/1H/15m: rsi14, ema, atr14, bb_pct, mom5, cei10, ofi10, vwap, bbp, close) — чтобы карточки рендерились с одного round-trip.
  - Новый `/api/intent-bars/{pair}?interval=15m&n=96` — OHLC через `yahoo.latest_bars` для cinematic chart, кэшируется TTL.

### UX-фичи
- 28 пар в адаптивной сетке. Сортировка по абс. отклонению prob от 50 (самые «однобокие» вверху).
- Pressure bar: визуальный tug-of-war BUY↔SELL, заполнение 0..100 от формулы `pressureScore` (forecast_prob 0.6 + radar 0.4 + OFI 0.4 + BB%B 0.4 + CEI 0.4 + mom5 0.4 → норм 0..100, clamp 2..98).
- Currency Strength Heatmap (USD/EUR/GBP/JPY/AUD/NZD/CAD/CHF) — реальный multi-pair расчёт.
- Live area-chart по 96 барам 15m + точка current_price (если новее последнего бара).
- Метрики (2×3): RSI 1H, ATR%, OFI, CEI, BB %B, Radar Score.
- Прогнозы (2×1): STAKAN side + DAILY side (берётся из `/api/stakan/signals` / `/api/daily/signals`, иначе общий `f.side` как fallback).
- Цели/разворот: `↑/↓ цель ≈ price ± 1.0×ATR`, разворот при `price ± 1.5×ATR`, окно `recommended_hours`.
- Тэги: BB extreme, RSI overbought/oversold, COT z-scores, есть открытая сделка, активный stakan-сигнал.
- Filter chips: все 28 / мажоры / тянут BUY / тянут SELL / у разворота / есть сделки.
- Search: substring по названию пары.
- Deep-dive модалка по клику на карточку: Wyckoff / Hurst / OB+FVG+Sweeps + полный indicators stack из `/api/forecast/{pair}`.
- Refresh каждые 10s; chart re-fetch не чаще раза в 60s (экономим Yahoo квоту).

### Критичный фикс
Chrome не даёт fetch() из URL с embedded credentials (`https://user:pass@host/`). Поэтому все fetch-вызовы перенаправлены через `abs(url)` — собирают абсолютный URL из `location.protocol + location.host` (без userinfo). Без этого все XHR падали с `TypeError: Request cannot be constructed from a URL that includes credentials`.

### Проверено руками
- `/` и `/intent` отдают новую страницу, `/system` — старую.
- `/api/forecasts` отдаёт 28 пар с `current_price` и compact indicators.
- `/api/intent-bars/EURUSD?n=20` отдаёт OHLC.
- В Chrome через tunnel (auto-login URL) карточки рендерятся, чарты живые, heatmap считает, filter+search работают.
- Скриншот в чате.

### Что дальше
- Деплой в Fly.io (`infra/fly/`) — постоянный URL, не привязан к VM.
- (опц.) `fxinvestment.com` через Cloudflare Registrar + `fly certs add` — инструкция пользователю.
