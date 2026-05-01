# SESSION_STATE.md — full snapshot for cross-account continuity

Этот файл — полный слепок состояния проекта **FOREX AI 2026 / TeamAgent**.
Он автоматически синхронизируется в **6 репозиториев пользователя** (`Jony-wws/FOREX`,
`FOREX21`, `Forex-wws2`, `Forex-wws22`, `Forex-wws2277`, `Forex-wws27`),
плюс в Devin Knowledge Note. Назначение — чтобы на **новом аккаунте Devin**
или **новом чате** при сообщении «продолжай» агент мог немедленно подхватить
работу с того места, где остановился, не задавая вопросов и не пересоздавая
систему с нуля.

---

## 1. Канонический репозиторий

**`Jony-wws/Forex-wws2277`** — единственный «живой» репо. Все остальные
(`FOREX`, `FOREX21`, `Forex-wws2`, `Forex-wws22`, `Forex-wws27`) — копии
или старые template-проекты; они существуют только как **резерв**, чтобы при
открытии любого из них агент сразу нашёл этот SESSION_STATE.md и пошёл в
правильное место.

- **Активная ветка:** `devin/1777586006-teamagent-rebuild`
- **Активный PR:** [#1](https://github.com/Jony-wws/Forex-wws2277/pull/1)
- **Базовая ветка PR:** `devin/trading-bot`

---

## 2. Что это за система

Multi-agent paper-trading платформа для **28 валютных пар** Forex.
~64 процесса:

- 60 субпроцессов-агентов (28 specialist + 14 analyzer + 10 learner + 5 health
  + 3 LLM)
- 7 ядерных процессов: `forecast_scanner` · `paper_trader` · `backtester` ·
  `strategy_search` · `state_committer` · `orchestrator` · `watchdog`
- FastAPI dashboard на `:8080`

**Источники данных только реальные:**
- Yahoo Finance (live + history)
- Dukascopy (1-min cache fallback)
- ForexFactory RSS (новостной blackout)

**Запреты (из AGENTS.md):**
- НЕ вводить симуляторы / random / fake data
- НЕ вводить второй meta-voting endpoint (PROGNOZY-28 — единственный источник)
- НЕ показывать 100% probability (cap 92%)
- НЕ снижать 70%-WR гейт без явного разрешения пользователя
- НЕ переписывать систему с нуля (всё уже построено)

---

## 3. Где запустить (быстрый старт)

```bash
cd ~/repos/Forex-wws2277
git pull
pip install -q -r teamagent/requirements.txt
bash scripts/start_all.sh        # стартует ВСЁ (orchestrator + 64 процесса)
                                 # dashboard на http://127.0.0.1:8080/
```

Открыть наружу — через Devin tool `deploy expose port=8080`. Туннельный URL
кладётся в `AGENTS.md` (раздел «Где найти данные пользователя»).

Стоп: `bash scripts/stop_all.sh`.

---

## 4. Дашборд (текущий URL — обновляется каждой сессией)

Свежий URL и Basic Auth — в `AGENTS.md` файла `Forex-wws2277`. На момент
последнего апдейта: см. секцию «Where to find the user's data» в `AGENTS.md`.

URL формата `https://<vm-id>-tunnel-<hash>.devinapps.com/` — он живёт пока
жива Devin VM. После остановки сессии URL умирает; чтобы постоянный — нужно
деплой на Fly.io (`infra/fly/Dockerfile`, `fly.toml` уже в репо).

Для Android Chrome пользователь использует `https://user:<pass>@host/`-формат
(auto-login). Это требует чтобы фронт делал fetch через `location.origin`
(см. фикс `9fab29d`).

---

## 5. Текущее состояние стратегии (на 2026-05-01 20:00 UTC)

### Stability Engine (NEW · 2026-05-01) — мат. гарантии стабильности

`teamagent/stability_engine.py` + `teamagent/resume_ru.py` добавляют 50+
функций нижних математических границ на основе реальных данных:

- **Wilson / Clopper-Pearson** — нижние границы для биномиальной WR.
- **Bootstrap CI** — resampling реальных PnL, фикс seed → репродуцируемо.
- **Conformal price band** — гарантированный 90% коридор цены через H часов.
- **VaR / CVaR / Sharpe / Sortino / Max DD / Calmar / Kelly half / PF**.
- **Brier / log loss / calibration table** — точность вероятностей.
- **Hurst / Variance Ratio / Skew / Kurtosis** — характеристики распределения.
- **Streak analysis / Stress-test** — длинные серии и худшие неделя/день.
- **Pair stability score (0–100)** + **System stability report**.

API: `/api/stability`, `/api/stability/{pair}`, `/api/min-guarantee`,
`/api/conformal/{pair}`, `/api/risk-metrics`, `/api/calibration`.

UI: 2 hero-секции — «ОБЩАЯ ОЦЕНКА СИСТЕМЫ» и «ГАРАНТИИ СТАБИЛЬНОСТИ» (50+
карточек, всё на русском). Премиум-дизайн: floating neon stars, breathing
auras, glassmorphism, animated gradient borders, pulsing dots, shimmer bars.

Тесты: `teamagent/tests/test_stability_engine.py` — 28/28 pass за 0.1 сек.

Коммит: `2b6bf1e feat: stability_engine + resume_ru + 50+ guarantees + premium UI`.

### Strategy Search

**Strategy Search** перебирает **120 вариантов** стратегий × **4 канонические
сессии** × **28 пар** на **365-дневном** Yahoo 1H бэктесте (минимум 10 сделок
для статистической значимости). Re-train каждые 5 дней (`LOOP_INTERVAL_SEC = 5 * 24 * 3600`).

**Сессии UTC** (не пересекаются):
- `Asia` — 00:00–06:59
- `London` — 07:00–12:59
- `Overlap` — 13:00–16:59
- `NY` — 17:00–21:59

**Реальный результат:**

| сессия | пар ≥70% WR / 28 |
|---|---|
| Asia | **1/28** — USDCAD 80% |
| London | **15/28** |
| Overlap | **18/28** |
| NY | **2/28** — EURGBP 74%, GBPNZD 75% |

**Итого: 36 из 112 (пара × сессия) ячеек реально достигают ≥70% WR.**
18 из 28 пар имеют edge хотя бы в одной сессии. 10 пар честно заморожены.

10 «замороженных» пар (на 60-дневных Yahoo-данных не достигают 70% ни в одной
сессии): EURUSD, GBPUSD, USDCHF, NZDCAD, CADCHF, CHFJPY, NZDJPY (примерный
список; сверять с `state/strategy_config.json`).

**Paper-trader gate** (per-session):
1. `forecast.probability_pct ≥ 70` (live signal)
2. `strategy_config[pair].by_session[current_session].qualifies_70pct == True`
   (per-session WR ≥70% на 60-дневном бэктесте)
3. фолбэк на `strategy_config[pair].best_variant` если он сам qualifies (без
   session-фильтра)
4. сигнал должен пройти фильтры выбранного варианта (|score|, prob, session)

Если ни (2), ни (3) — пара/сессия **frozen**, сделка не открывается.

---

## 6. Куда коммититься

Только в `Jony-wws/Forex-wws2277`, ветка `devin/1777586006-teamagent-rebuild`,
все коммиты идут в PR #1.

Каждые 15 минут `state_committer` авто-пушит:
- `state/forecasts.json`
- `state/open_trades.json`
- `state/closed_trades.json`
- `state/paper_stats.json`
- `state/backtest_30d.json`
- `state/strategy_config.json`

Это значит — даже если VM умрёт, **вся история сделок и стратегия выживут в git**.

---

## 7. Известные ограничения (не скрывать)

- Real 60-day backtest EDGE-44 multi-TF technical scanner-а имеет honest
  потолок ~36/112 ячеек ≥70% WR. Asia (1/28) и NY (2/28) фундаментально
  тяжелы — там цена эффективная и низкий объём.
- Чтобы поднять Asia/NY, нужны нон-технические сигналы:
  - LLM news/sentiment (агенты есть, но требуют API-ключи `GROQ_API_KEY`,
    `GOOGLE_API_KEY`, `OPENROUTER_API_KEY`)
  - COT data, order flow, sentiment
  - Возможно более длинная история (90-180 дней — но Yahoo 1H лимит 730 дней)
- Paper-trader **не понижает** 70% гейт ради «соответствия требованию» —
  лучше честно заморозить (pair, session) чем фейково открывать слабые.

---

## 8. План на будущие сессии (что делать после «продолжай»)

1. **Прочитать** этот файл (или AGENTS.md в `Forex-wws2277`).
2. **Запустить систему**: `bash scripts/start_all.sh` в `~/repos/Forex-wws2277`.
3. **Открыть дашборд наружу**: `deploy expose port=8080` (Devin tool).
4. **Обновить `AGENTS.md`** под новый URL/логин/пароль.
5. **Передать пользователю URL** в `message_user`.
6. **Если пользователь не дал других задач** — продолжать улучшать стратегию:
   - добавлять новые варианты (v91+, v100+ ...)
   - тестировать LLM сигналы если ключи есть
   - отслеживать долгосрочную статистику (paper_stats.json)
7. **Каждое изменение коммитить в PR #1**, ничего НЕ создавать с нуля.

---

## 9. Команды-чек-лист

```bash
# Где репо?
cd ~/repos/Forex-wws2277

# Я на правильной ветке?
git branch --show-current      # должно: devin/1777586006-teamagent-rebuild

# Установлены зависимости?
pip install -q -r teamagent/requirements.txt

# Что сейчас живёт?
ps -ef | grep -E "orchestrator|forecast|paper|backtest|strategy_search|dashboard" | grep -v grep

# API живой?
curl -s http://127.0.0.1:8080/api/health | head -c 200

# Свежий strategy_config?
python -c "import json; c=json.load(open('teamagent/state/strategy_config.json')); print('as_of:', c['as_of']); print('qualified_count:', c['summary']['qualified_count'])"

# Сколько сделок открыто/закрыто?
python -c "import json; print(json.load(open('teamagent/state/paper_stats.json')))"

# Запустить
bash scripts/start_all.sh

# Остановить
bash scripts/stop_all.sh

# Открыть наружу (внутри Devin)
# → используй deploy expose port=8080 (Devin tool)
```

---

## 10. Связанные документы

- `Forex-wws2277/AGENTS.md` — короткий чек-лист для агента + текущий URL
- `Forex-wws2277/README.md` — пользовательская документация
- `Forex-wws2277/HISTORY/` — лог по одному файлу на каждую Devin-сессию.
  Агент ОБЯЗАН читать последние 3 файла на старте и дописывать новый
  в конце сессии. См. `HISTORY/README.md`.
- `Forex-wws2277/PR #1` — все коммиты + история обсуждения
- Devin Knowledge Note (если создан) — копия этого файла на уровне org

Если все 3 утрачены — этот файл (`SESSION_STATE.md`) + `HISTORY/` сам по себе
достаточен, чтобы новый Devin-агент полностью восстановил контекст.

---

_Последнее обновление: 2026-05-01_
_Если этот файл устарел больше чем на сутки — сверяйся с `git log` в
`Forex-wws2277` и доверяй последним коммитам._
