"""state_committer — каждые 15 мин коммитит + пушит ключевые state-файлы в git,
чтобы новая сессия Devin могла продолжить с точной истории.

Что коммитим (и зачем):
- forecasts.json        — текущие 28-парные прогнозы
- open_trades.json      — активные виртуальные сделки
- closed_trades.json    — история (WIN/LOSS, PnL)
- paper_stats.json      — общая статистика (WR, PnL)

Что НЕ коммитим (мусор для git): heartbeat_*.json, agents.json, agent_*.json,
open_trades_enriched.json (производный), dukascopy_cache (бинарь).

Безопасность:
- Если в state нет изменений — ничего не коммитим (молча).
- Если git-команды падают (нет сети, конфликты) — логируем и идём дальше,
  не валимся, потому что нас супервайзит orchestrator + watchdog.
- Коммитим как [skip ci] чтобы не триггерить CI на каждом state-снапшоте.
"""
from __future__ import annotations
import json
import logging
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from . import config

log = logging.getLogger("state_committer")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.FileHandler(config.LOGS_DIR / "state_committer.log"),
        logging.StreamHandler(sys.stdout),
    ],
)

REPO_ROOT = Path(__file__).resolve().parent.parent
HEARTBEAT_FILE = config.STATE_DIR / "heartbeat_state_committer.json"

# Файлы, которые мы хотим хранить в git (если они есть)
PERSISTED_FILES = [
    "teamagent/state/forecasts.json",
    "teamagent/state/open_trades.json",
    "teamagent/state/closed_trades.json",
    "teamagent/state/paper_stats.json",
    # Без этих двух новая сессия не пройдёт гейт ≥70% и сделки не откроются:
    "teamagent/state/backtest_30d.json",
    "teamagent/state/strategy_config.json",
    # 365-дневный анализ поведения рынка (per pair × hour × dow × session) —
    # дорогой пересчёт (~15с по всем 28 парам), поэтому переносим между сессиями.
    "teamagent/state/market_regime_365d.json",
    # Locked baseline: snapshot strategy_config.json после первого валидного
    # 365-day sweep. Используется как fallback если очередной sweep дал хуже.
    "teamagent/state/strategy_config_locked.json",
    # WR floor monitor + weekly loss review — между сессиями полезно сохранять
    # чтобы дашборд сразу показывал состояние, не ждал первого tick (5 мин).
    "teamagent/state/agent_learner_wr_floor_monitor.json",
    "teamagent/state/agent_learner_weekly_loss_review.json",
    # FRED fundamentals (added 2026-05-01 per user request to add macro
    # signals): rates / 10y yields / CPI YoY per 8 currencies. CSV refresh
    # is 24h-cached so checking these in saves Yahoo/FRED calls on restart.
    "teamagent/state/fundamentals.json",
    "teamagent/state/agent_analyzer_fundamental_macro.json",
    # CFTC COT positioning (added 2026-05-01): weekly speculator long/short
    # in EUR/GBP/JPY/CHF/AUD/CAD/NZD futures. 24h cache; report itself is
    # weekly, so persisting saves a fresh API hit at restart.
    "teamagent/state/cot_positioning.json",
    "teamagent/state/agent_analyzer_cot_positioning.json",
    # Параллельная стратегия "Стакан" (added 2026-05-01): отдельные сделки/история/статы.
    "teamagent/state/stakan_open_trades.json",
    "teamagent/state/stakan_closed_trades.json",
    "teamagent/state/stakan_stats.json",
    "teamagent/state/stakan_signals.json",
    # «Военный радар» рынка (added 2026-05-01): 20+ независимых сканеров × 28 пар.
    "teamagent/state/market_radar.json",
]

COMMIT_INTERVAL_SEC = 15 * 60   # 15 мин


def _heartbeat(tick: int) -> None:
    HEARTBEAT_FILE.write_text(json.dumps({
        "name": "state_committer",
        "category": "system",
        "ts": datetime.now(timezone.utc).isoformat(),
        "pid": os.getpid(),
        "tick_count": tick,
    }))


def _git(*args: str) -> tuple[int, str]:
    """Запустить git с args в REPO_ROOT, вернуть (rc, stdout+stderr)."""
    try:
        r = subprocess.run(
            ["git", *args],
            cwd=REPO_ROOT,
            check=False,
            capture_output=True,
            text=True,
            timeout=60,
        )
        return r.returncode, (r.stdout + r.stderr).strip()
    except subprocess.TimeoutExpired:
        return 124, "git timeout"
    except Exception as e:
        return 1, f"git exec error: {e}"


def _existing_files() -> list[str]:
    return [p for p in PERSISTED_FILES if (REPO_ROOT / p).exists()]


def _commit_once() -> str:
    """Один проход коммита. Возвращает строку-резюме для лога."""
    files = _existing_files()
    if not files:
        return "no state files yet"

    # -f нужен потому что некоторые state-файлы в .gitignore (agent_*.json) —
    # но мы сами хотим их хранить чтобы дашборд видел их сразу при рестарте.
    rc, out = _git("add", "-f", "--", *files)
    if rc != 0:
        return f"git add failed: {out[:200]}"

    rc_diff, _ = _git("diff", "--cached", "--quiet", "--", *files)
    if rc_diff == 0:
        return "no changes — skip commit"

    summary = _summary_for_message()
    msg = f"state: {summary} [skip ci]"

    rc, out = _git(
        "-c", "user.email=massaw750@gmail.com",
        "-c", "user.name=Jony-wws",
        "commit", "-m", msg, "--", *files,
    )
    if rc != 0:
        return f"git commit failed: {out[:200]}"

    rc, out = _git("push")
    if rc != 0:
        return f"committed locally, push failed: {out[:200]}"
    return f"committed + pushed ({summary})"


def _summary_for_message() -> str:
    """Короткое summary для commit message — что изменилось в state."""
    parts = []
    paper = REPO_ROOT / "teamagent/state/paper_stats.json"
    if paper.exists():
        try:
            d = json.loads(paper.read_text())
            parts.append(
                f"trades={d.get('total', 0)}/W{d.get('wins', 0)}/L{d.get('losses', 0)} "
                f"WR={d.get('win_rate_pct', 0):.0f}% PnL={d.get('total_pnl_usd', 0):+.0f}$"
            )
        except Exception:
            pass
    open_t = REPO_ROOT / "teamagent/state/open_trades.json"
    if open_t.exists():
        try:
            n = len(json.loads(open_t.read_text()))
            parts.append(f"open={n}")
        except Exception:
            pass
    if not parts:
        parts.append(datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"))
    return " | ".join(parts)


def run() -> None:
    log.info("state_committer start; interval=%ds", COMMIT_INTERVAL_SEC)
    tick = 0
    while True:
        tick += 1
        _heartbeat(tick)
        try:
            res = _commit_once()
            log.info("[tick=%d] %s", tick, res)
        except Exception as e:
            log.exception("commit_once crashed: %s", e)
        # сон с регулярным heartbeat-ом, чтобы watchdog был доволен
        for _ in range(COMMIT_INTERVAL_SEC // 60):
            _heartbeat(tick)
            time.sleep(60)


if __name__ == "__main__":
    run()
