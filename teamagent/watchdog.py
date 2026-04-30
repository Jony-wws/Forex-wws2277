"""Watchdog — отдельный процесс, мониторит heartbeat-файлы.

Если heartbeat агента старше config.AGENT_DEAD_AFTER_SEC — пишет
state/recommended_restart.json. Orchestrator уже умеет рестартить мёртвые
процессы (POSIX-уровень), а watchdog добавляет heartbeat-уровень: «процесс
жив, но не пингует уже 10+ мин» → kill через psutil → orchestrator увидит и
перезапустит.

Также пишет heartbeat_watchdog.json для дашборда.
"""
from __future__ import annotations
import json
import logging
import os
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import psutil

from . import config

log = logging.getLogger("watchdog")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.FileHandler(config.LOGS_DIR / "watchdog.log"),
        logging.StreamHandler(sys.stdout),
    ],
)

HEARTBEAT_FILE = config.STATE_DIR / "heartbeat_watchdog.json"
RECOMMENDED_RESTART_FILE = config.STATE_DIR / "recommended_restart.json"


def _heartbeat() -> None:
    HEARTBEAT_FILE.write_text(json.dumps({
        "name": "watchdog",
        "ts": datetime.now(timezone.utc).isoformat(),
        "pid": os.getpid(),
    }))


def _scan() -> dict:
    now = datetime.now(timezone.utc)
    stale = []
    fresh = 0
    for p in config.STATE_DIR.glob("heartbeat_*.json"):
        if p.name in ("heartbeat_watchdog.json", "heartbeat_orchestrator.json"):
            # сами себя не убиваем
            pass
        try:
            hb = json.loads(p.read_text())
            ts = datetime.fromisoformat(hb["ts"])
            age = (now - ts).total_seconds()
            if age > config.AGENT_DEAD_AFTER_SEC:
                stale.append({"name": hb.get("name", p.stem), "age_sec": int(age), "pid": hb.get("pid")})
            else:
                fresh += 1
        except Exception:
            continue

    # пытаемся убить "зависшие" процессы по pid → orchestrator их перезапустит
    killed = []
    for s in stale:
        pid = s.get("pid")
        if not pid:
            continue
        try:
            proc = psutil.Process(pid)
            proc.terminate()
            killed.append(s["name"])
            log.warning(f"[kill] {s['name']} pid={pid} (no heartbeat for {s['age_sec']}s)")
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass

    RECOMMENDED_RESTART_FILE.write_text(json.dumps({
        "as_of": now.isoformat(),
        "stale": stale,
        "killed": killed,
        "fresh_count": fresh,
    }))
    return {"stale": len(stale), "killed": len(killed), "fresh": fresh}


def run() -> None:
    log.info(f"watchdog start (dead-after={config.AGENT_DEAD_AFTER_SEC}s)")
    stop = {"flag": False}

    def _sig(_a, _b):
        stop["flag"] = True
        log.info("watchdog: SIGTERM/SIGINT — stopping")
    signal.signal(signal.SIGTERM, _sig)
    signal.signal(signal.SIGINT, _sig)

    while not stop["flag"]:
        _heartbeat()
        try:
            r = _scan()
            log.info(f"scan: stale={r['stale']} killed={r['killed']} fresh={r['fresh']}")
        except Exception as e:
            log.exception(f"scan failed: {e}")
        _heartbeat()
        for _ in range(config.WATCHDOG_INTERVAL_SEC):
            if stop["flag"]:
                break
            time.sleep(1)
    log.info("watchdog exit")


if __name__ == "__main__":
    run()
