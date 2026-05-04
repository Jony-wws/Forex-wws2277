# Test plan — PR #11 unified Сделки section on main page

**Target:** https://static-build-ftaqiznm.devinapps.com/ (the static mirror
deployed from this PR's branch).
**PR:** https://github.com/Jony-wws/Forex-wws2277/pull/11

## What changed (user-visible)

The user previously had to navigate to `/trades.html` to see open + closed
trades. After this PR, the same data is the FIRST hero section on the main
page (`index.html` aka `/intent`) — title `💼 СДЕЛКИ — открытые + история
в одном месте` (id `main-trades-section`).

## Concrete expected values (baked into the static mirror at build time)

Verified via `curl` against the deployed baked JSON:

- `/api/stats.json` → `{total:10, wins:6, losses:4, win_rate_pct:60.0, total_pnl_usd:1.8}`
- `/api/open-trades.json` → `{count:0, trades:[]}`
- `/api/closed-trades.json` → `{count:10, trades:[…]}`

Therefore the section MUST render exactly these values.

## Primary flow — proves the feature works

1. **Open** `https://static-build-ftaqiznm.devinapps.com/` in Chrome, fully
   maximized.
2. **Confirm section is FIRST under tab nav.** The headline
   `💼 СДЕЛКИ — открытые + история в одном месте` should be visible without
   scrolling, immediately under the `📈 Прогнозы / 💼 Сделки / 🩺 Система /
   🤖 Агенты` tab row.
   - **PASS** if section is at the top under the tabs.
   - **FAIL** if it appears below "ФИНАЛЬНЫЕ ПРОГНОЗЫ" or is missing.
3. **Verify summary tiles populate with exact values.** After ~1s the six
   tiles should read:
   - `Всего`: **10**
   - `Открыто`: **0** (amber)
   - `WIN`: **6** (green)
   - `LOSS`: **4** (red)
   - `Win Rate`: **60.0%** (amber, since 50 ≤ wr < 70)
   - `PnL`: **+$1.80** (green)
   - **PASS** if all six numbers match exactly.
   - **FAIL** if any tile shows `…` after 5+ seconds, shows `NaN`, or shows
     a mismatched value.
4. **Verify «Открыты сейчас» empty-state.** With 0 open trades the table
   body should display the empty-state row containing the literal text:
   `Сейчас открытых сделок нет. Как только probability ≥ 70% — paper-trader
   откроет сам.`
   - **PASS** if that exact Russian sentence is visible.
   - **FAIL** if the row reads `загружаю открытые сделки…` (means
     `refreshMainTrades()` never ran) or any other text.
5. **Verify «Последние закрытые» table shows real data, not placeholder.**
   The first row should have a non-empty Pair (3-letter base + 3-letter
   quote, e.g. `EURNZD`), a colored Side (`BUY` green or `SELL` red), a
   formatted Probability percentage, a non-empty Strategy column (variant
   id like `v20_contra_high` plus a small `(WR%)` suffix), and a
   colored Result `WIN`/`LOSS` matching the PnL sign.
   - **PASS** if the table is populated with at least 1 well-formed row.
   - **FAIL** if rows show `загружаю историю…`, blank cells, or only `—`
     placeholders.
6. **Verify «Win Rate по парам» grid is colored.** At least 1 pair cell
   must be either green (`mt-pair-win`, WR ≥ 70%) or red (`mt-pair-loss`,
   WR < 50%). The pill in the section header reads `N пар в истории`.
   - **PASS** if at least 1 cell shows green or red coloring with a
     percentage.
   - **FAIL** if grid shows only `подсчитываю…` or `Пока нет закрытых
     сделок.`.

## Adversarial check — would these same observations happen if the
change were broken?

- If the new HTML section were missing → step 2 fails (no headline visible).
- If `refreshMainTrades()` never wired up → steps 3–6 fail (everything stays
  on `…` placeholder).
- If the static-shim weren't redirecting `/api/*` → tiles read 0/0/0/0
  with `0.0%` instead of the baked values.
- If a CSS prefix collided → tiles render unstyled.

So this 6-step plan distinguishes a working implementation from a broken
one in concrete, observable ways.

## Out of scope (NOT tested in this PR)

- Live re-trading on Fly backend (Fly is intermittently down on free tier;
  the static mirror serves baked snapshots which is exactly the user's
  Android-Chrome path).
- Per-pair-session 70%-WR strategy ceiling — covered by AGENTS.md and the
  honest 30/112 cells number in the PR description; not a frontend test.
- The `/trades.html` deep-dive page — unchanged by this PR.
