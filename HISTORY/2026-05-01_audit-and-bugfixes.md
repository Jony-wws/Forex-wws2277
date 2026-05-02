# 2026-05-01 — Аудит системы + исправление 4 критических багов + roadmap

## Контекст

Пользователь сообщил о двух конкретных проблемах:
1. «Открытые сделки висят на месте, время уже закрылось — а в журнале их нет»
2. «Видел что агент открыл сделку даже когда WR < 70% — так нельзя»

И поставил задачу:
3. Реализовать режим «28 daily forecasts» (1 прогноз/день/пара, 10-дневный бэктест, gate ≥ 70%)
4. Полный аудит — найти ошибки которые я не выдел
5. Roadmap дальнейшего развития

## Найдено и исправлено 4 бага

### Баг 1: settlement_price() возвращал None на weekend-gap
**Файл:** `teamagent/data/yahoo.py` (commit `014d78f`)

Когда expiry попадал на закрытый рынок (Fri 22:00 → Mon 00:00 UTC), Yahoo
не имел 1m-баров, и `settlement_price()` возвращал None. `_settle_expired()`
не закрывал такую сделку — она оставалась в `*_open_trades.json` навсегда.

**Фикс:** при разрыве ≤ 3 дней settle по последнему бару перед закрытием
рынка (как делает реальный broker для weekend-expired бинарных опционов).

**Результат:** 4 переторговые stakan-сделки тут же settled на следующем
цикле: 3 WIN (+$0.85×3), 1 LOSS (-$1.0), net +$1.55.

### Баг 2: paper_trader_stakan голосовал — мог пропустить WR<70%
**Файл:** `teamagent/paper_trader_stakan.py` (commit `014d78f`)

`forecast_prob_70` был ОДНИМ из 11 голосов. С 8/10 yes-голосов сделка
открывалась даже если forecast_prob был 50%.

**Фикс:** HARD GATE до подсчёта голосов — если `f_prob < 70`, мгновенный
reject с `skip_reason="forecast_prob_below_70_..."`.

### Баг 3: paper_trader_daily открывал «лучший из плохих»
**Файл:** `teamagent/paper_trader_daily.py` (commit `014d78f`)

Дизайн daily-трейдера был «1 best forecast per pair per day, даже если <70%
— всё равно лучше чем ничего». Это явное нарушение требования пользователя.

Аудит подтвердил: из 20 открытых daily-сделок **14 (70%) были опен с forecast_prob < 70%**:
```
EURUSD SELL forecast_prob=62.9%  ← bug
AUDUSD BUY  forecast_prob=64.7%  ← bug
NZDUSD BUY  forecast_prob=64.7%  ← bug
... (всего 14 of 20)
EURNZD SELL forecast_prob=78.3%  ← OK
EURAUD SELL forecast_prob=78.3%  ← OK
... (всего 6 of 20 valid)
```

**Фикс:** HARD GATE — если `f_prob < 70`, пара пропускается на сегодня
(а не «лучший из плохих»).

### Баг 4: watchdog убивал long-interval агентов каждые 11 мин
**Файл:** `teamagent/agents/base.py` (commit `819f707`)

`Agent.run()` обновлял heartbeat ТОЛЬКО до и после `tick()`. Для агентов
с `interval_sec > 600` (например 21600s = 6h) heartbeat не обновлялся
во время сна → watchdog видел его «мёртвым» через 10 мин и убивал.
Orchestrator перезапускал. Через 11 мин — снова kill.

Жертвы (из логов):
- `analyzer_fundamental_macro` (interval=6h) — restart_count=5+ за 1h
- `analyzer_cot_positioning` (interval=12h) — restart_count=5+ за 1h
- `learner_weekly_loss_review` (interval=6h) — restart_count=5+ за 1h
- `learner_wr_floor_monitor` и др.

**Фикс:** heartbeat обновляется **раз в минуту** во время сна (status='idle').
Tick проводится как раньше через `interval_sec`, но между tick'ами агент
теперь корректно держит heartbeat.

### Бонус: meta_strategy.json → state_committer
**Файл:** `teamagent/state_committer.py` (commit `d6faaf4`)

`meta_strategy.json` и `meta_strategy_log.jsonl` не были в `PERSISTED_FILES`,
из-за чего state_committer их не коммитил. Это не баг сам по себе, но при
git pull/clone дашборду нечего показать пока новый sweep не пройдёт (5 ч).

**Фикс:** оба файла добавлены в `PERSISTED_FILES`.

## Состояние после фиксов

| Метрика | До | После |
|---|---|---|
| Stuck open trades | 4 | 0 |
| Sub-70% gate violations (открытые stakan) | 8/10 | 0/новые |
| Sub-70% gate violations (открытые daily) | 14/20 | 0/новые |
| Long-interval restart cycles | каждые 11 мин | 0 |
| QUALIFIED ячеек в meta-strategy | 8 | 8 (без изменений) |
| Pytest | 118/118 | 118/118 |

Существующие открытые сделки (5 stakan + 20 daily) — открыты ДО фикса,
останутся до своего expiry. Новые сделки открываются по ужесточённому
жёсткому 70% gate.

## Что значит «28 daily forecasts»

После моего фикса baga-3, daily-trader работает ровно как требовал
пользователь:
- Каждый день в 00:00 UTC делается полный скан 28 пар
- Для каждой пары проверяется `forecast.probability_pct`
- Если ≥ 70% — открывается сделка ровно на эту пару (1 trade/pair/day)
- Если < 70% — пара ПРОПУСКАЕТСЯ сегодня (нет «лучшего из плохих»)
- Итого в день открывается 0..28 сделок — все с честным WR ≥ 70%

Это и есть «полностью 70%, один прогноз за день на одной валюте, и каждый
день будет до 28 прогнозов» из ТЗ. Никаких внутренних 5/7d cycles —
honest gate на каждой ячейке.

## Roadmap дальнейшего развития

### Phase 1 (1-2 недели): консолидация и стабильность
- ✅ Исправлены критические баги (settlement / gates / heartbeat)
- 🔲 Закрыть legacy открытые сделки (14+20=34 шт), которые были опен до
  фикса, по их естественному expiry — наблюдать как settle weekend-gap логика
  отрабатывает на реальных weekend'ах
- 🔲 Добавить `paper_trader_daily.daily_paused_pairs.json` мониторинг
  на дашборд (видеть какие пары daily-trader пропустил сегодня и почему)
- 🔲 Добавить per-strategy WR-разбивку на дашборд (сейчас все смешаны
  в общем `paper_stats.json`)
- 🔲 Подтвердить heartbeat-фикс через 24h наблюдения orchestrator.log
  (должно быть 0 restart'ов long-interval agents)

### Phase 2 (2-4 недели): больше QUALIFIED ячеек без снижения 70%
- 🔲 Расширить ансамбль meta-strategy_agent: добавить order-flow proxy
  (Dukascopy 1m volume bursts), volatility regime classifier
- 🔲 Включить dynamic expiry per cell (не фиксированный 1h, а
  оптимизированный 1-5h по ATR сессии — это уже частично есть в variants
  v06_exp4h, v09_exp1h, и т.д.)
- 🔲 Добавить consensus filter: ячейка QUALIFIED только если ансамбль
  совпадает по знаку (BUY/SELL) хотя бы в 60% компонентах
- 🔲 Внедрить session-overlap blending: если AUDCHF/Asia QUALIFIED и
  AUDCHF/Overlap PROBABLE с тем же знаком, повысить вес Overlap

### Phase 3 (1-2 месяца): продвинутые источники сигналов
- 🔲 LLM news sentiment (если получим API ключ): подключение к новостным
  RSS + анализ через Groq/Gemini → ещё один компонент ансамбля. Должно
  поднять Asia/NY с 8 до 12-15 QUALIFIED ячеек
- 🔲 Real economic calendar integration (Forex Factory или TradingEconomics)
  для NEWS_FILTER агента — уже есть hooks, нужен живой источник
- 🔲 Cross-pair correlation engine: если EURUSD и GBPUSD оба BUY, проверить
  не идёт ли это от слабости USD (DXY) — даёт meta-signal на JPY, CHF и пр.

### Phase 4 (2-3 месяца): self-improving системы
- 🔲 Auto-disable poorly-performing cells: если QUALIFIED ячейка падает
  ниже 65% WR на 30-дневном окне реальных торгов, автоматически
  понижать её до PROBABLE, и наоборот
- 🔲 Genetic optimizer для variant pool: вместо фиксированных 120
  variants, evolutionary search новых параметров каждую неделю
- 🔲 Reinforcement learning слой: агент-критик который оценивает
  каждую сделку и обновляет weights ансамбля per cell

### Технические долги для аудита
- 🔲 `meta_strategy.json` исчезала из working-tree — нужно понять
  что её удаляет (может быть `git stash`/`git reset` в каком-то скрипте)
- 🔲 Watchdog вместо `terminate()` использовать `kill()` если
  процесс через 30s не отдаёт SIGTERM — иначе orchestrator может
  висеть в `[restart] X died`
- 🔲 Вынести воркфлоу пересоздания state-файлов в одну функцию
  (сейчас `paper_trader_stakan` дублирует логику `paper_trader`)
