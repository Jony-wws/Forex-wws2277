"""Orchestrator — запускает forecast_scanner, paper_trader и все 60 агентов как
дочерние процессы. Каждые 60 сек обновляет state/agents.json для дашборда.

Каждый агент = отдельный python -m teamagent.agents._runner <name> процесс,
чтобы изоляция была полной (зависание одного не валит остальных).

При SIGTERM — рассылает SIGTERM всем детям и ждёт их.
"""
from __future__ import annotations
import json
import logging
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from . import config
from .agents.registry import all_agents

log = logging.getLogger("orchestrator")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.FileHandler(config.LOGS_DIR / "orchestrator.log"),
        logging.StreamHandler(sys.stdout),
    ],
)

HEARTBEAT_FILE = config.STATE_DIR / "heartbeat_orchestrator.json"
AGENTS_STATE_FILE = config.STATE_DIR / "agents.json"


def _heartbeat() -> None:
    HEARTBEAT_FILE.write_text(json.dumps({
        "name": "orchestrator",
        "ts": datetime.now(timezone.utc).isoformat(),
        "pid": os.getpid(),
    }))


class ChildProc:
    def __init__(self, name: str, cmd: list[str]):
        self.name = name
        self.cmd = cmd
        self.proc: subprocess.Popen | None = None
        self.start_count = 0
        self.last_start: datetime | None = None
        self._stdout_fh = None
        self._stderr_fh = None

    def is_alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def _close_log_handles(self) -> None:
        for fh_attr in ("_stdout_fh", "_stderr_fh"):
            fh = getattr(self, fh_attr, None)
            if fh is None:
                continue
            try:
                fh.close()
            except Exception:
                pass
            setattr(self, fh_attr, None)

    def _reap_previous(self) -> None:
        """Дождаться завершения и реапнуть предыдущий Popen, чтобы не накапливать
        зомби-процессы при auto-restart. Если процесс ещё жив — не блокируемся."""
        if self.proc is None:
            return
        if self.proc.poll() is None:
            return  # ещё жив, не ждём
        try:
            self.proc.wait(timeout=1)
        except Exception:
            pass

    def start(self) -> None:
        # Перед перезапуском: закрываем старые FD и реапаем зомби.
        self._reap_previous()
        self._close_log_handles()

        log_path = config.LOGS_DIR / f"{self.name}.out"
        err_path = config.LOGS_DIR / f"{self.name}.err"
        self._stdout_fh = open(log_path, "ab")
        self._stderr_fh = open(err_path, "ab")
        self.proc = subprocess.Popen(
            self.cmd,
            stdout=self._stdout_fh,
            stderr=self._stderr_fh,
            preexec_fn=os.setsid if hasattr(os, "setsid") else None,
        )
        self.start_count += 1
        self.last_start = datetime.now(timezone.utc)
        log.info(f"[start] {self.name} pid={self.proc.pid} (start_count={self.start_count})")

    def stop(self, sig: int = signal.SIGTERM) -> None:
        if self.proc is None:
            return
        try:
            os.killpg(os.getpgid(self.proc.pid), sig)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                self.proc.terminate()
            except Exception:
                pass

    def close(self) -> None:
        """Финальная зачистка ресурсов: дождаться процесса и закрыть FD."""
        self._reap_previous()
        self._close_log_handles()


def _build_agent_cmd(agent: dict) -> list[str]:
    return [
        sys.executable, "-m", "teamagent.agents._runner",
        agent["module"], agent["class"],
        json.dumps(agent.get("init_args", {})),
        agent["name"], agent["category"],
    ]


def _build_all_children() -> dict[str, ChildProc]:
    out: dict[str, ChildProc] = {}
    out["forecast_scanner"] = ChildProc("forecast_scanner", [sys.executable, "-m", "teamagent.forecast_scanner"])
    out["paper_trader"] = ChildProc("paper_trader", [sys.executable, "-m", "teamagent.paper_trader"])
    # Параллельная стратегия "Стакан" (2026-05-01): отдельный процесс, отдельные state-файлы.
    out["paper_trader_stakan"] = ChildProc(
        "paper_trader_stakan",
        [sys.executable, "-m", "teamagent.paper_trader_stakan"],
    )
    out["state_committer"] = ChildProc("state_committer", [sys.executable, "-m", "teamagent.state_committer"])
    out["backtester"] = ChildProc("backtester", [sys.executable, "-m", "teamagent.backtester"])
    out["strategy_search"] = ChildProc("strategy_search", [sys.executable, "-m", "teamagent.strategy_search", "--loop"])
    for a in all_agents():
        out[a["name"]] = ChildProc(a["name"], _build_agent_cmd(a))
    return out


def _refresh_agents_state(children: dict[str, ChildProc]) -> None:
    """Читает heartbeat_*.json и формирует state/agents.json."""
    now = datetime.now(timezone.utc)
    rows = []
    for name, ch in children.items():
        hb_path = config.STATE_DIR / f"heartbeat_{name}.json"
        alive_proc = ch.is_alive()
        hb = None
        age_sec = None
        if hb_path.exists():
            try:
                hb = json.loads(hb_path.read_text())
                ts = datetime.fromisoformat(hb["ts"])
                age_sec = int((now - ts).total_seconds())
            except Exception:
                pass
        rows.append({
            "name": name,
            "category": (hb or {}).get("category", "?"),
            "alive": bool(alive_proc and (age_sec is not None and age_sec < config.AGENT_DEAD_AFTER_SEC)),
            "process_alive": alive_proc,
            "age_sec": age_sec,
            "last_seen": (hb or {}).get("ts"),
            "start_count": ch.start_count,
            "tick_count": (hb or {}).get("tick_count"),
        })
    AGENTS_STATE_FILE.write_text(json.dumps({"as_of": now.isoformat(), "agents": rows}, indent=2))


def run() -> None:
    log.info("orchestrator start")
    children = _build_all_children()
    for ch in children.values():
        ch.start()
        time.sleep(0.05)   # лёгкое расхождение запусков

    stop = {"flag": False}

    def _sig(_a, _b):
        stop["flag"] = True
        log.info("orchestrator: SIGTERM/SIGINT — stopping children")
    signal.signal(signal.SIGTERM, _sig)
    signal.signal(signal.SIGINT, _sig)

    while not stop["flag"]:
        _heartbeat()
        # авто-рестарт умерших процессов (чисто process-level; watchdog ещё heartbeat-level)
        for ch in children.values():
            if not ch.is_alive():
                log.warning(f"[restart] {ch.name} died; restarting")
                ch.start()
        _refresh_agents_state(children)
        for _ in range(config.WATCHDOG_INTERVAL_SEC):
            if stop["flag"]:
                break
            time.sleep(1)

    # graceful stop
    for ch in children.values():
        ch.stop(signal.SIGTERM)
    deadline = time.time() + 10
    for ch in children.values():
        while ch.is_alive() and time.time() < deadline:
            time.sleep(0.2)
        if ch.is_alive():
            ch.stop(signal.SIGKILL)
    # реапнуть процессы и закрыть FDs
    for ch in children.values():
        ch.close()
    log.info("orchestrator exit")


if __name__ == "__main__":
    run()
