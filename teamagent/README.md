# TeamAgent — мульти-агентная система прогнозов и paper-trade для бинарных опционов

Перестройка системы с прошлой сессии. Главные принципы:
- **Только реальные данные** (Yahoo Finance, Dukascopy 30-дневный кэш 1-мин баров, ForexFactory RSS). Никаких симуляторов.
- **Один источник правды**: панель `PROGNOZY (28 валютных пар)` и мета-голосование объединены — в дашборде показывается одно и то же число.
- **Paper-trader 24/7**: при прогнозе ≥70% сам открывает виртуальную сделку (бинарный опцион, $50 stake, 85% payout, экспирация 1–4 ч), потом закрывает по реальной цене Yahoo, считает HIT/MISS и PnL.
- **Живая панель открытых сделок**: цена входа, время открытия, таймер до экспирации, текущая цена, текущий PnL — обновление каждые 30 секунд по каждой паре.
- **Volume Profile (Стакан) с прогнозом**: уровни поддержки/сопротивления + куда цена не вернётся до 00:00 UTC+5 + крупные игроки на каждом уровне.
- **60 агентов с heartbeat-watchdog**: если агент молчит >10 минут — orchestrator его перезапускает (auto-recovery).
- **Кэп вероятности 92%** (никогда не показываем 100% — это математически нечестно).

## Структура

```
teamagent/
├── config.py                       # 28 пар, торговые сессии, кэпы, тайминги
├── data/
│   ├── yahoo.py                    # Yahoo Finance live + history (без ключа)
│   ├── dukascopy.py                # 30-дневный кэш 1-мин баров (без ключа)
│   └── news.py                     # ForexFactory RSS (high-impact ±30 мин)
├── indicators.py                   # RSI/EMA/ATR/BB/Momentum/CEI/OFI
├── volume_profile.py               # Стакан + прогноз 00:00 UTC+5
├── forecast_scanner.py             # 28 пар × 5 мин — единый источник прогнозов
├── paper_trader.py                 # бинарные опционы, $50 / 85%, real settlement
├── orchestrator.py                 # запуск 60 агентов + heartbeat
├── watchdog.py                     # авто-рестарт упавших агентов
├── agents/
│   ├── base.py                     # базовый класс с heartbeat
│   ├── analyzers/                  # технический анализ (~20 агентов)
│   ├── learners/                   # ML-style обучение (~10 агентов)
│   ├── specialists/                # per-пара специалисты (~25 агентов)
│   └── health/                     # recovery / watchdog (5 агентов)
├── dashboard/
│   ├── server.py                   # FastAPI
│   └── static/                     # HTML/JS (без React, лёгкий, обновление 30 сек)
├── state/                          # runtime JSON: heartbeat, open trades, forecasts
└── logs/                           # logs от каждого процесса
```

## Запуск

```bash
# 1. установка зависимостей
pip install -r requirements.txt

# 2. ENV переменные (один раз)
export GROQ_API_KEY=...
export GOOGLE_API_KEY=...
export OPENROUTER_API_KEY=...
export DERIV_DEMO_TOKEN=...   # опционально, для real-котировки с Deriv

# 3. запуск всего
bash scripts/start_all.sh

# 4. остановка всего
bash scripts/stop_all.sh
```

## Тест индикаторов

```bash
pytest tests/
```

## API ключи

- Groq: https://console.groq.com/keys
- Google AI Studio: https://aistudio.google.com/apikey
- OpenRouter: https://openrouter.ai/keys
- Deriv: https://app.deriv.com/account/api-token (опционально)
