# Инструкции для любой AI-платформы (Cursor, Codex, Claude, ChatGPT, и др.)

## Что это за проект
FOREX AI 2026 — multi-agent paper-trading система для 28 валютных пар.
64 процесса: сканер цен, трейдер, бэктестер, поиск стратегий, мета-агент, коммиттер, оркестратор, watchdog + 60 sub-agents.

## Что хочет пользователь
1. Система должна работать 24/7 без ошибок
2. Все данные обновляются каждые 1-2 минуты (цены, прогнозы, новости, макро)
3. Сделки открываются автоматически когда probability >= 70%
4. Минимум 70% win rate на всех 28 парах и всех 4 сессиях
5. Индивидуальный подход к каждой паре и каждой сессии
6. Не симулятор — только реальные данные

## Как запустить
git clone https://github.com/Jony-wws/Forex-wws2277.git
git checkout devin/1777586006-teamagent-rebuild
pip install -q -r teamagent/requirements.txt
bash scripts/start_all.sh
Dashboard: http://127.0.0.1:8080/

## Ключевые файлы
- AGENTS.md — полная инструкция для AI-агентов
- SESSION_STATE.md — слепок состояния проекта
- HISTORY/*.md — лог всех сессий (читать последние 3)
- teamagent/config.py — конфигурация
- teamagent/paper_trader.py — логика открытия/закрытия сделок
- teamagent/forecast_scanner.py — сканер прогнозов (28 пар)
- teamagent/strategies.py — каталог 370 стратегий
- teamagent/strategy_search.py — бэктест всех вариантов × пар × сессий
- teamagent/indicators.py — 14 технических индикаторов
- teamagent/market_microstructure.py — Wyckoff, SMC, Order Blocks, FVG
- teamagent/fundamentals.py — FRED макро-данные
- teamagent/cot.py — CFTC COT данные
- teamagent/market_radar.py — 20 независимых сканеров
- teamagent/regime.py — классификация режима рынка
- teamagent/playbook.py — playbook 28×4×4 ячеек
- fly.toml — конфиг Fly.io деплоя
- state/*.json — текущее состояние

## Текущие URL
- Full live Devin tunnel: https://user:5f457c9656cd820841749ce6f3785c00@d2a19c266c48-tunnel-rbyxmhrg.devinapps.com/
- Fly.io: https://fxinvestment-uqfprqce.fly.dev/
- Static: https://static-build-lqdncvmx.devinapps.com/

## Текущая конфигурация (2026-05-04)
- STRICT_QUALIFIED_GATE = False
- FLY_FULL в repo fly.toml = 1, но Devin deploy backend не применяет repo fly.toml env напрямую; бесплатная Fly-машина при full mode OOM-kill через ~90 сек.
- Бесплатный стабильный вариант: Fly/static для просмотра, Devin tunnel для полного live запуска 64 процессов.
- strategy_config.json восстановлен на 30/112 qualified cells из commit f80fc53.

## TODO
1. Довести qualified ячейки до 60+/112 (сейчас 30/112)
2. Расширить Strategy.evaluate() чтобы читал market_microstructure.json, fundamentals.json, cot.json, market_radar.json напрямую
3. Когда >=80 ячеек qualified — включить STRICT_QUALIFIED_GATE=True
4. Если появится Fly token или другой бесплатный способ scale — поднять Fly memory и снова проверить FLY_FULL=1 без OOM

## Запреты
- НЕ создавать систему с нуля
- НЕ добавлять симуляторы / random / fake data
- НЕ показывать probability 100% (cap 92%)
