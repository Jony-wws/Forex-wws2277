# FOREX Сигналы 2026

> ## 🎯 АКТУАЛЬНАЯ СТРАТЕГИЯ (самая последняя версия системы)
> Полное описание торговой системы EUR/USD (бинарные опционы, 5ч) + код автосканера:
> **[`viktor-strategy/README.md`](viktor-strategy/README.md)**
> Создано Viktor AI (viktor.com) для JONY, 11.06.2026.

Система анализа рынка Forex в реальном времени для 28 валютных пар.

🌐 **Публичный URL:** <https://forex-wws2277.fly.dev/>
(Telegram Mini App: <https://forex-wws2277.fly.dev/tg>)

> Постоянный деплой на Fly.io free tier — каждый push в `main`
> автоматически разворачивает дашборд через
> `.github/workflows/deploy_fly.yml`. Подробности и one-time setup —
> в `.agents/skills/forex-strict-cycle/SKILL.md` § 12.

## Возможности

- **Реальные данные** — Yahoo Finance (без симуляторов)
- **15+ индикаторов**: RSI, MACD, EMA, Bollinger, Stochastic, ADX, Williams %R, Ichimoku, Momentum, VWAP, Price Action
- **Сигналы BUY/SELL** — только при уверенности ≥80%
- **Прогноз на 5 часов и 24 часа**
- **Стакан ордеров** — Bid/Ask, спред, глубина рынка, поддержка/сопротивление
- **Price Action** — анализ свечных паттернов
- **UTC+5**, весь интерфейс на русском
- **Мгновенная загрузка** — данные встроены в HTML
- **Обновление каждые 10 секунд**

## Запуск

```bash
pip install fastapi uvicorn yfinance pandas numpy
python -m uvicorn app.main:app --host 0.0.0.0 --port 8080
```

Откройте http://localhost:8080

## Структура

```
app/
├── config.py         # 28 пар, UTC+5, пороги
├── prices.py         # Yahoo Finance + кэш
├── indicators.py     # Технические индикаторы
├── price_action.py   # Свечные паттерны
├── orderbook.py      # Стакан ордеров
├── analyzer.py       # Мультитаймфреймовый анализ
└── main.py           # FastAPI сервер + сканер
static/
└── index.html        # UI: таблица сигналов + стакан ордеров
```

## API

| Endpoint | Описание |
|---|---|
| `GET /` | Главная страница |
| `GET /api/signals` | Все 28 пар: цены, сигналы, прогнозы |
| `GET /api/orderbook/{pair}` | Стакан ордеров для одной пары |
| `GET /api/orderbooks` | Стаканы ордеров для всех пар |
