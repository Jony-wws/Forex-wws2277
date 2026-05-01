# HISTORY/ — session-by-session log of agent + user conversations

This folder is the **persistent memory** of every Devin session that worked on
this repo. The user explicitly requested that NO conversation, code change, or
system state ever be lost — even when switching to a fresh Devin account/org.

## Rule for every agent (Devin / Codex / Cursor / etc.)

**At the START of every new session:**

1. List existing files: `ls HISTORY/*.md`.
2. Read at least the last 3 dated files (most recent first).
3. Read `AGENTS.md` and `SESSION_STATE.md`.

**At the END of every session — BEFORE the final `message_user` / `block_on_user=true`:**

1. Create `HISTORY/<UTC-date>_<short-slug>.md` (e.g.
   `HISTORY/2026-05-01_launch-and-restore-tunnel.md`).
2. Inside, write:
   - **Date / session id / Devin org**
   - **What the user asked** (verbatim Russian / English quotes preserved)
   - **What was done** (commands, files changed, deploys, URLs)
   - **Current state** (live URL, open trades count, WR, anything notable)
   - **Open questions / TODOs for the next session**
3. `git add HISTORY/...md` → `git commit -m "history: <slug> [skip ci]"` →
   `git push origin devin/1777586006-teamagent-rebuild`.

If the session is terminated abruptly, the next agent MUST reconstruct what
happened from `git log` + `state/*.json` and fill in a HISTORY entry retroactively.

## Why a folder, not one big file

- One file per session = no merge conflicts when the
  hourly Devin schedule pushes state at the same time.
- Easy to grep across sessions: `grep -l "продолжай" HISTORY/*.md`.

## Naming

`HISTORY/YYYY-MM-DD_short-slug.md` — date is UTC, slug is lowercase-kebab.

If multiple sessions happen on the same UTC day, append a counter:
`HISTORY/YYYY-MM-DD_short-slug_2.md`.
