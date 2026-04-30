"""Базовый класс агента: heartbeat, лог, цикл, корректное завершение по сигналам."""
from __future__ import annotations
import abc
import json
import logging
import os
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from .. import config


class Agent(abc.ABC):
    name: str = "agent_base"
    interval_sec: int = 60          # как часто запускать tick()
    category: str = "analyzer"      # analyzer / learner / specialist / health / llm

    def __init__(self) -> None:
        self.logger = logging.getLogger(self.name)
        if not self.logger.handlers:
            handler = logging.FileHandler(config.LOGS_DIR / f"{self.name}.log")
            handler.setFormatter(logging.Formatter(
                "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
            ))
            self.logger.addHandler(handler)
        self.logger.setLevel(logging.INFO)
        self.heartbeat_path = config.STATE_DIR / f"heartbeat_{self.name}.json"
        self.state_path = config.STATE_DIR / f"agent_{self.name}.json"
        self.last_state: dict = {}
        self.tick_count = 0

    # ─────────────────────── heartbeat ───────────────────────
    def heartbeat(self, status: str = "ok", extra: dict | None = None) -> None:
        payload = {
            "name": self.name,
            "category": self.category,
            "status": status,
            "ts": datetime.now(timezone.utc).isoformat(),
            "pid": os.getpid(),
            "interval_sec": self.interval_sec,
            "tick_count": self.tick_count,
        }
        if extra:
            payload.update(extra)
        try:
            self.heartbeat_path.write_text(json.dumps(payload))
        except Exception as e:
            self.logger.warning(f"heartbeat write failed: {e}")

    # ─────────────────────── state ───────────────────────
    def save_state(self, state: dict) -> None:
        self.last_state = state
        try:
            self.state_path.write_text(json.dumps(state, indent=2))
        except Exception as e:
            self.logger.warning(f"state write failed: {e}")

    def load_state(self) -> dict:
        if not self.state_path.exists():
            return {}
        try:
            return json.loads(self.state_path.read_text())
        except Exception:
            return {}

    # ─────────────────────── core ───────────────────────
    @abc.abstractmethod
    def tick(self) -> dict:
        """Один шаг работы агента. Должен вернуть короткое summary для UI/логов."""
        ...

    def run(self) -> None:
        self.logger.info(f"{self.name} start (category={self.category}, interval={self.interval_sec}s)")
        stop = {"flag": False}

        def _sig(_a, _b):
            stop["flag"] = True
            self.logger.info(f"{self.name}: SIGTERM/SIGINT — stopping")

        try:
            signal.signal(signal.SIGTERM, _sig)
            signal.signal(signal.SIGINT, _sig)
        except ValueError:
            # вне основного потока — окей
            pass

        while not stop["flag"]:
            self.heartbeat("ok")
            try:
                summary = self.tick()
                self.tick_count += 1
                if summary:
                    self.save_state({
                        "summary": summary,
                        "tick_count": self.tick_count,
                        "as_of": datetime.now(timezone.utc).isoformat(),
                    })
            except Exception as e:
                self.logger.exception(f"tick failed: {e}")
                self.heartbeat("error", {"error": str(e)})
            self.heartbeat("ok")
            for _ in range(self.interval_sec):
                if stop["flag"]:
                    break
                time.sleep(1)
        self.heartbeat("stopped")
        self.logger.info(f"{self.name} exit")
