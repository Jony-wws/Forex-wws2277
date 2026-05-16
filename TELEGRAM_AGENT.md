# Telegram-агент → GitHub Actions

Бот в Telegram, который понимает свободный текст по-русски и **запускает любую GitHub Actions workflow** из этого репо. Бесплатно, без сторонних API.

```
пользователь в TG          GitHub Actions (бесплатно)         GitHub Actions
──────────────────         ────────────────────────────       ────────────────
"свежий цикл"  ──►  scripts/telegram_bot.py          ──►  cycle_5h.yml
                    └─ scripts/telegram_agent.py          (запускается, шлёт
                       │  └─ GitHub Models gpt-4o-mini     отчёт в Telegram
                       │     (intent parser, FREE)         через progress-бар)
                       └─ POST /actions/workflows/.../dispatches
                          (PAT с actions:write)
```

## Что бот понимает

Просто пиши свободным текстом — LLM сама подберёт workflow из whitelist в [`scripts/telegram_agent.py`](scripts/telegram_agent.py):

| Что сказать | Что запустится | Workflow |
|---|---|---|
| «свежий цикл», «пересчитай топ-3», «обнови сигналы» | 5h-цикл по 28 парам | `cycle_5h.yml` |
| «скриншот сайта», «как сейчас выглядит дашборд» | Playwright-скрин дашборда | `site_screenshot.yml` |
| «график EURUSD на TradingView» | TV-скрин пары | `tv_screenshot.yml` |
| «бэктест 28 пар», «мульти-TF бэктест» | Бэктест по всем парам | `multi_tf_backtest.yml` |
| «бэктест», «прогон EURUSD» | Бэктест EURUSD | `backtest.yml` |
| «что улучшить?», «AI-обзор» | LLM-критика последнего цикла | `ai_review.yml` |
| «расскажи что на рынке», «narrative» | AI-сводка рынка | `ai_narrative.yml` |
| «AI-патч», «правь код» | LLM правит код, делает PR | `ai_patcher.yml` |
| «отчёт за неделю» | Недельный отчёт | `weekly_report.yml` |
| «работает ли сайт», «health» | Health-check | `health_check.yml` |
| «брокеры», «спред» | Сравнение цен брокеров | `multi_broker.yml` |
| «дрифт» | Детектор дрейфа стратегии | `drift_detector.yml` |
| «новости» | News-watcher | `news_watcher.yml` |

Также работают **явные команды** — имя workflow со слэшем, без LLM:

```
/cycle_5h
/site_screenshot
/backtest
/ai_review
/help
```

## Настройка (один раз)

1. **`TELEGRAM_BOT_TOKEN`** — токен от @BotFather (уже настроен, если бот живёт).
2. **`DASHBOARD_URL`** *(опционально)* — URL дашборда для `/start` Mini App кнопки.
3. **`GH_DISPATCH_TOKEN`** — fine-grained PAT с правом `actions:write` на этот репо. Создать: https://github.com/settings/tokens?type=beta → New token → Repository access: только `Forex-wws2277` → Permissions: `Actions: Read and write`. Скопировать → положить в repo secrets как `GH_DISPATCH_TOKEN`. **Без него бот сможет отвечать текстом, но не запускать workflows.**
4. **`TELEGRAM_ALLOWED_CHATS`** *(рекомендуется)* — comma-separated список ваших Telegram chat id, кому разрешено дёргать workflows. Свой chat id можно узнать через бота `@userinfobot`. Если оставить пустым, бот будет реагировать **на всех** — для публичного бота это небезопасно.

`GITHUB_TOKEN` для GitHub Models инжектится Actions автоматически — отдельно ничего настраивать не нужно.

## Как это устроено

### 1. Парсинг intent

`scripts/telegram_agent.py` → `parse_intent(text, github_token)`:

* Системный промт перечисляет все workflows из whitelist (`WORKFLOWS`).
* GPT-4o-mini возвращает JSON: `{"action": "dispatch", "workflow": "cycle_5h", "inputs": {}, "reply": "Запускаю..."}`.
* Если LLM недоступен (нет токена / quota исчерпана) — срабатывает регекс-эвристика `_HEURISTICS`, покрывающая базовые формулировки.

Slash-команды (`/cycle_5h`, `/help`) идут мимо LLM — прямой lookup по whitelist.

### 2. Запуск workflow

`dispatch_workflow(...)` → `POST https://api.github.com/repos/{owner}/{repo}/actions/workflows/{file}/dispatches`:

```http
Authorization: Bearer ${GH_DISPATCH_TOKEN}
Accept: application/vnd.github+json
Content-Type: application/json

{"ref": "main", "inputs": {}}
```

Успех — 204 No Content. Бот отвечает «Запускаю X. Отчёт пришлю как только workflow закончит» + ссылку на список запусков.

### 3. Отчёт обратно в Telegram

Воркфлоу **сами** шлют результат в Telegram (`cycle_5h.py` уже умеет, см. [`TELEGRAM_PROGRESS.md`](TELEGRAM_PROGRESS.md)). Для тех, кто пока не шлёт, можно добавить шаг в YAML:

```yaml
      - name: Notify Telegram
        if: always()
        env:
          TELEGRAM_BOT_TOKEN: ${{ secrets.TELEGRAM_BOT_TOKEN }}
          TELEGRAM_CHAT_ID:  ${{ secrets.TELEGRAM_CHAT_ID }}
        run: |
          curl -s -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
               -d chat_id="${TELEGRAM_CHAT_ID}" \
               -d text="✅ ${{ github.workflow }} завершён"
```

## Добавить новый tool

1. У workflow должен быть `on: workflow_dispatch:` (у нас он есть у 28 из 28).
2. Добавить запись в [`WORKFLOWS`](scripts/telegram_agent.py) внутри `telegram_agent.py`:

   ```python
   "my_new_tool": {
       "file": "my_new_tool.yml",
       "description": "Что делает. Запускать когда пользователь просит «...».",
       "inputs": {},   # или {"pair": "название пары, напр. EURUSD"}
   },
   ```
3. Готово. LLM прочтёт `description` в следующем запуске и сама поймёт, когда дёргать.

## Ограничения

* **Задержка ответа.** Keepalive workflow живёт 5 мин **раз в 30 мин**. Сообщение, отправленное в окно бездействия, ждёт до 30 мин пока бот стартует следующий раз (Telegram сам копит updates через `getUpdates`). Если нужна мгновенная реакция — переносить бота на Fly.io (`fly.toml` в репо уже есть) с постоянным процессом.
* **LLM rate-limit.** GitHub Models gpt-4o-mini ≈ 50 запросов/день free tier. Каждое свободное сообщение от пользователя = 1 запрос. Slash-команды лимит не тратят.
* **Workflow rate-limit.** GitHub Actions: 1000 запусков workflow_dispatch в час на repo — практически не достижим.
* **Безопасность.** LLM выбирает только из whitelist — нельзя «уговорить» бота сделать что-то другое (запустить `rm -rf` или поменять код). Самое «опасное» из whitelist — `ai_patcher`, и он делает PR, а не пушит напрямую.

## Локальная отладка

```bash
# Без LLM, без диспатча — только эвристика:
python scripts/telegram_agent.py "свежий цикл"

# С LLM (нужен PAT с models:read):
GITHUB_TOKEN=$(gh auth token) python scripts/telegram_agent.py "сделай скриншот сайта"

# Полный цикл — реально дёрнет workflow:
GITHUB_TOKEN=$(gh auth token) \
GH_DISPATCH_TOKEN=ghp_... \
GITHUB_REPOSITORY=Jony-wws/Forex-wws2277 \
python scripts/telegram_agent.py "запусти бэктест"
```

Внутри `telegram_bot.py` для smoke-теста без Telegram:

```bash
TELEGRAM_BOT_TOKEN= python scripts/telegram_bot.py
# → exits cleanly (нет токена — нечего поллить)
```

## Связанные файлы

* [`scripts/telegram_bot.py`](scripts/telegram_bot.py) — long-poll loop + delegating handler.
* [`scripts/telegram_agent.py`](scripts/telegram_agent.py) — intent parser + dispatcher.
* [`.github/workflows/telegram_bot_keepalive.yml`](.github/workflows/telegram_bot_keepalive.yml) — крон-обвязка.
* [`TELEGRAM_PROGRESS.md`](TELEGRAM_PROGRESS.md) — прогресс-бар внутри `cycle_5h.yml`.
