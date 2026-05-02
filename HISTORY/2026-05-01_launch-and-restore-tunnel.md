# 2026-05-01 — launch & restore tunnel for new Devin account

- **Devin session**: `devin-8b14ed6c3cae4792bc7fde71f8db373f`
  (https://app.devin.ai/sessions/8b14ed6c3cae4792bc7fde71f8db373f)
- **User**: Jony (`Jony-wws`, massaw750@gmail.com), Android Chrome, on a fresh
  Devin org/account (no prior knowledge notes / playbooks / schedules in this org).
- **Branch**: `devin/1777586006-teamagent-rebuild` (Forex-wws2277).

## What the user asked (verbatim)

> "Провер всё репозиторий и все rewind всё работало но сайт не работает просто
> нужно запустить сайт и один раз проверить что всё работает и дат мне ссылку
> которую будет работать без логин и пароль …"

Then, after I mistakenly deployed the AI-Studio FOREX repo:

> "Я говорил не про это а система сайт который сам создал devin провер всё
> репозиторий и review что это вообще я говорю только о тот система которая
> создана devin"

Then, the cross-account/migration request:

> "теперь нам нужно весь код система переписка наша история сделать не пропадал
> никогда нужно сохранить куда-то например в git hub и в review … удалил все
> ненужные репозиторий из github должен остаться только devin … каждый
> переписку новую и каждый код каждую систему каждое изменение … чтобы я не
> повторился много раз"

## What was done

### 1. Identified the right repo
- 6 repos total. Only `Forex-wws2277` on branch `devin/1777586006-teamagent-rebuild`
  is the Devin-built **TeamAgent FOREX AI 2026** system.
- `FOREX` and `FOREX21` are separate AI-Studio (Gemini) projects — unrelated.
- `Forex-wws2`, `Forex-wws22`, `Forex-wws27` are mostly-empty pointer repos with
  a one-line `SESSION_STATE.md` that points back to `Forex-wws2277`.

### 2. Started the system
```
cd ~/repos/Forex-wws2277
pip install -q -r teamagent/requirements.txt
bash scripts/start_all.sh
```
All components green: `forecast_scanner`, `paper_trader`, `orchestrator`,
`watchdog`, `backtester`, `state_committer`, `strategy_search`.

### 3. Exposed dashboard
```
deploy expose port=8080
```
Tunnel: `https://8b14ed6c3cae-tunnel-pchp0vd8.devinapps.com/`
Auth: `user` / `bfb871a7d9c5bc32830e1df7d8956536`
Auto-login URL sent to user (works in Android Chrome with one tap).

### 4. Updated `AGENTS.md`
Bumped the "Where to find the user's data" section to the new tunnel URL.
Committed as `ef0adaa` on `devin/1777586006-teamagent-rebuild`.

### 5. (this commit) Set up cross-account memory
- Added `HISTORY/` folder with `README.md` describing the new session-log
  protocol.
- Added this file as the first entry.
- Updated `AGENTS.md` to mandate reading + writing HISTORY/ on every session.
- Mirrored the updated SESSION_STATE.md pointer to the 5 sibling repos.
- Will create org-level Devin knowledge note + playbook in this same session
  so future "продолжай" commands work without re-explanation.

## Current live state at end of session

- Live URL: see `AGENTS.md` § "Where to find the user's data".
- Open trades: 3 (EURNZD SELL, AUDCAD BUY, AUDNZD BUY) at session start;
  paper-trader running normally.
- PROGNOZY-28 top forecasts: AUDCHF 79.6% BUY, EURAUD 78.3% SELL, EURNZD 78.3% SELL,
  AUDUSD 77.0% BUY.
- WR (history): 0% (fresh account, no closed trades yet — closed trades from prior
  account live in git history of the branch).

## Open TODOs for the next session

- Decide whether to deploy a permanent Fly.io instance (`infra/fly/`) so the
  user gets a stable URL that doesn't die with the Devin VM.
- The user mentioned wanting to delete the unused repos (`FOREX`, `FOREX21`,
  `Forex-wws2`, `-wws22`, `-wws27`) — they have to do this manually from
  GitHub mobile (Settings → Delete repository). I left the SESSION_STATE.md
  pointers in place in case they keep them.
- Recreate the hourly Devin schedule (`sched-083b11171a0841668f4608b075d769b5`
  was tied to the previous org and is gone here).
- Recreate the org Knowledge Note (was in previous org, gone here) — done in
  this session via `devin_knowledge_manage`.
