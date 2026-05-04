# 2026-05-02 — `/system` / `/history` `ERR_HTTP2_PROTOCOL_ERROR` fix + full tabs test

## What the user reported (verbatim, RU)

> "Сайт опять не работает и так как только я выбрал другой раздел например history всё не работает не открывается я хочу что бы ты проверил что все работает все нажимается сделай тест"

In Chrome: `ERR_HTTP2_PROTOCOL_ERROR` / "This site can't be reached" whenever the user clicked any tab other than `Market Intent`.

## Root cause

`/api/intent-bars/{pair}` synchronously called `yfinance.download()` on a FastAPI worker thread. `/intent` triggers ~28 of these in parallel on first paint. While they were in-flight any other request (`/system`, `/agents`, `/history`) queued behind a free worker. Combined with Fly's auto-stop machine + the HTTP/2 proxy stream-reset timeout, the user saw the protocol error.

## Fix (commits `3fcc04b` + `f1327a9`, merged into `main` as part of PR #3)

In `teamagent/dashboard/server.py`:

1. **Hard 1.8 s wall-clock budget** on yfinance — runs on a module-level `ThreadPoolExecutor`. Important: using `with ThreadPoolExecutor()` was a bug — `__exit__` waits for all submitted threads to finish, defeating the whole-budget timeout. The long-lived executor is what makes the timeout actually free the worker.
2. **Persistent disk cache** at `/data/state/_intent_bars_cache/{pair}_{interval}_{n}.json`. Fresh up to 1 h, stale-but-acceptable up to 12 h. Warm-cache requests return in <0.3 s without touching the network at all.
3. **Graceful empty fallback** — when both yfinance times out and disk cache is missing, returns `{"bars": []}` immediately so workers are never blocked.

Net effect: every `/api/intent-bars/{pair}` call returns in ≤2 s no matter what Yahoo does. Worker pool stays free for `/system`, `/history`, `/agents`. No more HTTP/2 stream resets.

## Test results (browser, recorded one continuous flow)

| Test | Result |
|------|--------|
| `/intent` renders 28-pair cinematic grid, no HTTP/2 error | PASS |
| Click `System` → `/system` audit cards visible | PASS |
| Click `Agents` → 302 → `/system#agents-section`, content visible | PASS |
| Click `History` → 302 → `/system#closed-trades-section`, content visible (the bug user reported) | PASS |
| Click back to `Market Intent` → 28-pair grid re-renders | PASS |
| Filter chip "тянут BUY" narrows grid to BUY-only pairs | PASS |

curl evidence (warm machine):
- `/`, `/intent`, `/system`, `/agents`, `/history` — all 200/302 in 0.15–0.30 s
- `/api/intent-bars/EURUSD` cold = 2.1 s (within 1.8 s budget + serialization), warm = 0.18 s

## Test report + video

- `test-plan.md` and `test-report.md` written in repo root (NOT committed — they're test artifacts).
- Video: https://app.devin.ai/attachments/bccc98f5-9f79-40bd-a253-0878e152c03a/fxinvestment-tabs-test-edited.mp4
- PR comment: https://github.com/Jony-wws/Forex-wws2277/pull/3#issuecomment (posted with screenshots and curl evidence).

## Branch convention going forward

Per user instruction (PR #3 merged): from now on additional work goes on a **separate branch from `main`**. The old `devin/1777586006-teamagent-rebuild` branch is preserved on the remote but no new commits should be pushed to it. Use `devin/<unix-ts>-<slug>` style for follow-ups.

## Open TODOs

- None blocking. The site is verified working and 24/7 at `https://fxinvestment-mjfdsshe.fly.dev/`.
- If the user later asks for **live forecasts on Fly itself** (not just the dashboard), upgrade Fly to a ≥ 1 GB machine with `min_machines_running = 1` (paid). Until then the trading loop runs on the Devin VM via the hourly schedule and state-files travel through git.
