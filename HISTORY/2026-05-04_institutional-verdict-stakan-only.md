# 2026-05-04 — институциональный вердикт + сайт «только стакан»

## User asks (verbatim, начало сессии)

> Пользователь хочет МИНИМАЛЬНЫЙ сайт с ОДНИМ разделом — стакан ордеров. Но
> под капотом система должна использовать ВСЕ существующие данные и думать
> как КРУПНЫЙ ИГРОК (институционал), а не как розничный трейдер. Когда
> система говорит "КУПИТЬ" — это окончательный вердикт, все данные
> указывают на это и рынок не развернётся.

> Система проверяет направление на 24 часа, но даёт прогноз на 5 часов —
> за это время прогноз должен отработать.

Спецификация весов (от пользователя):

- институционал: VP big-players ×4, no-return ×3, COT ×3, Market Radar ×3,
  FRED-macro ×2, 11-vote stakan-консенсус ×3, VP impulse ×2
- розница: EMA-stack 4H ×0.5, ADX-trend ×0.5, MACD-1H ×0.5
- veto: news blackout ±30 мин (high-impact) → ОЖИДАНИЕ независимо от голосов
- ≥80% институциональных согласны И перевес ≥65% И ≥3 голосовавших → КУПИТЬ/ПРОДАТЬ
- 60–80% → СКОРЕЕ КУПИТЬ / СКОРЕЕ ПРОДАТЬ (жёлтый)
- <60% или blackout → ОЖИДАНИЕ (серый)

Дополнительно: `hours_to_midnight_utc5`, `target_by_midnight`,
`favorite_side`, `favorite_balance_pct`, `institutional_sources_agree/voted/total`.

Сайт: только селектор 28 пар + БОЛЬШОЙ ВЕРДИКТ (цвет, причина на русском,
баланс, фаворит, прогноз до полуночи UTC+5, сколько источников согласны)
+ visual orderbook + крупные игроки + live-цена 5–10 сек. НЕТ: nav-вкладок,
«Сделок», «Системы», «Агентов».

Уточнения в процессе:

> Видео не надо только 5 фото

## What was done

### Backend
- `teamagent/stakan_view.py`:
  - константы файлов: `_STAKAN_SIGNALS_FILE`, `_NEWS_BLACKOUTS_FILE`
  - 5 хелперов: `_stakan_vote_for`, `_fnd_signal_for`, `_cot_signal_for`,
    `_hours_to_midnight_utc5`, `_check_news_blackout`
  - **новая функция `_institutional_verdict()`** (≈360 строк) — агрегирует
    7 институциональных + 3 розничных голоса с указанными выше весами,
    считает agreement по числу проголосовавших (а не от total — иначе «нет
    данных» разбавило бы согласие), решает вердикт по дереву условий,
    возвращает `verdict`, `verdict_color`, `reason_ru` на русском, баланс,
    список из 10 sources, `hours_to_midnight_utc5`, `target_by_midnight`,
    `no_return_level`, `news_blackout`.
  - `build_view()` загружает ещё `stakan_signals.json` + `news_blackouts.json`
    и кладёт результат в поле `verdict` ответа `/api/stakan-view/{pair}`.

### Frontend (новый минимальный сайт)
- `teamagent/dashboard/static/stakan-only.html` — селектор 28 пар (chips),
  БОЛЬШОЙ верд блок, развёрнутый список источников, Volume-Profile-стакан,
  крупные игроки.
- `teamagent/dashboard/static/stakan-only.css` — тёмная тема, цветовое
  кодирование зелёный/красный/жёлтый/серый, адаптивный layout, бейджи
  «СИЛЬНЫЙ СИГНАЛ» / «УМЕРЕННЫЙ СИГНАЛ» / «ЖДЁМ».
- `teamagent/dashboard/static/stakan-only.js` — fetch
  `/api/stakan-view/{pair}` каждые 10с, `/api/live-price/{pair}` каждые 5с,
  ленивый рендер, автоскролл стакана к текущей цене, авто-обновление
  цвета чипа после загрузки соответствующего пары.

### Routing
- `teamagent/dashboard/server.py`:
  - `GET /` теперь отдаёт `stakan-only.html` (главная)
  - `GET /stakan` — алиас
  - `GET /intent` сохранён как legacy cinematic-панель
  - всё остальное (`/system`, `/trades`, `/agents`, `/history`, API) не тронуто

### Документация
- `AGENTS.md` — раздел "Where to find the user's data" обновлён: новый
  канонический URL `https://fxinvestment-jwodwfwy.fly.dev/`, описание
  маршрутов `/`, `/stakan`, `/intent`.
- `.agents/skills/fly-deploy/SKILL.md` — обновлён канонический URL.

## Verification

1. `python3 -m py_compile teamagent/stakan_view.py teamagent/dashboard/server.py` — OK.
2. `bash scripts/start_all.sh`, `curl /api/health` → все 12 компонентов alive.
3. `curl /api/stakan-view/EURUSD` — поле `verdict` присутствует с подполями
   `verdict`, `verdict_color`, `reason_ru`, `sources`, `institutional_sources_*`,
   `hours_to_midnight_utc5`, `target_by_midnight`, `no_return_level`,
   `news_blackout`. Логи показывают: на текущих данных EURGBP даёт
   `ПРОДАТЬ` (5/5 voted, 91% balance), GBPCAD — `СКОРЕЕ КУПИТЬ`
   (2/3 voted, 54% balance), EURUSD — `ОЖИДАНИЕ` (2/3 voted, 75%).
4. `deploy backend --dir … --volume true` → `https://fxinvestment-jwodwfwy.fly.dev/`,
   200 OK на `/`, `/stakan`, `/intent`, `/api/stakan-view/EURGBP`.
5. 5 скриншотов сняты на live-публичном URL (видео пользователь не просил):
   - 28-pair picker + EURUSD ОЖИДАНИЕ (gray)
   - EURGBP огромный красный ПРОДАТЬ + «СИЛЬНЫЙ СИГНАЛ»
   - развёрнутый блок «Источники голосования» (10 строк, INST ×4/×3/×2 + РОЗНИЦА ×0.5)
   - стакан + крупные игроки крупным планом
   - GBPCAD жёлтый СКОРЕЕ КУПИТЬ + «УМЕРЕННЫЙ СИГНАЛ»

## Current state

- PR #14 открыт в `Jony-wws/Forex-wws2277` (base
  `devin/1777586006-teamagent-rebuild`, head
  `devin/1777915011-institutional-verdict-stakan-only`).
- CI на репо не настроена — pr_checks возвращает 0.
- Публичный Fly URL: https://fxinvestment-jwodwfwy.fly.dev/
- Ветка `devin/1777586006-teamagent-rebuild` НЕ изменена (PR ещё не merge).
  Каноническая старая ссылка `fxinvestment-nbmuknwe.fly.dev` всё ещё
  показывает прежний cinematic UI.

## Open TODOs / cautions

- **Merge PR #14** в `devin/1777586006-teamagent-rebuild`, затем
  `deploy backend` ещё раз — это перенесёт STAKAN-only UI и на
  каноническую ссылку `fxinvestment-nbmuknwe.fly.dev/` (если deploy
  попадёт в существующее приложение). Если subdomain снова сменится —
  обновить AGENTS.md + SKILL.md.
- На текущих закоммиченных state-файлах (snapshot перед PR-сборкой)
  большинство пар → ОЖИДАНИЕ, потому что VP/COT/FRED данные из вчерашних
  снимков. Каждый час Schedule `sched-083b…` подтянет свежие state и
  вердикты уплотнятся.
- В summary `/api/stakan-view` (без пары) verdict-поля для каждой пары
  не считаются (это было бы 28 × VP-вычисление, дорого). На главной
  странице чипы показывают цвет вердикта только после клика на пару.
  Если пользователь захочет «цвет на чипе сразу» — добавить лёгкий
  «verdict-only» эндпоинт (без VP, только из forecast + radar + COT +
  stakan_signals).

## Files touched in this session

- M: `teamagent/stakan_view.py`
- M: `teamagent/dashboard/server.py`
- A: `teamagent/dashboard/static/stakan-only.html`
- A: `teamagent/dashboard/static/stakan-only.css`
- A: `teamagent/dashboard/static/stakan-only.js`
- M: `AGENTS.md`
- M: `.agents/skills/fly-deploy/SKILL.md`
- A: `HISTORY/2026-05-04_institutional-verdict-stakan-only.md` (this file)
