"""5 health/recovery агентов.

Не путать с watchdog (отдельный процесс) — эти agents смотрят на свои аспекты:
- recovery_supervisor: держит список «упавших» агентов и просит watchdog их рестартить
- memory_doctor: следит за RSS / VSZ
- data_freshness: цены не старше 5 минут
- disk_janitor: ротация логов
- api_health_pinger: пинг внешних API
"""
from __future__ import annotations
import json
import os
import shutil
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import psutil
import requests

from ... import config
from ...data import yahoo
from ..base import Agent


class RecoverySupervisor(Agent):
    name = "health_recovery_supervisor"
    category = "health"
    interval_sec = 90

    def tick(self):
        # читает все heartbeat_*.json, ищет stale (>10 мин) → пишет recommended_restart.json
        now = datetime.now(timezone.utc)
        stale = []
        alive = 0
        for p in config.STATE_DIR.glob("heartbeat_*.json"):
            try:
                hb = json.loads(p.read_text())
                ts = datetime.fromisoformat(hb["ts"])
                age = (now - ts).total_seconds()
                if age > config.AGENT_DEAD_AFTER_SEC:
                    stale.append({"name": hb.get("name"), "age_sec": int(age)})
                else:
                    alive += 1
            except Exception:
                continue
        rec_path = config.STATE_DIR / "recommended_restart.json"
        rec_path.write_text(json.dumps({"as_of": now.isoformat(), "stale": stale}))
        return {"alive": alive, "stale_count": len(stale), "stale": stale[:5]}


class MemoryDoctor(Agent):
    name = "health_memory_doctor"
    category = "health"
    interval_sec = 120

    def tick(self):
        vm = psutil.virtual_memory()
        return {
            "total_mb": round(vm.total / 1e6, 1),
            "used_mb": round(vm.used / 1e6, 1),
            "available_mb": round(vm.available / 1e6, 1),
            "percent": vm.percent,
        }


class DataFreshnessChecker(Agent):
    name = "health_data_freshness"
    category = "health"
    interval_sec = 180

    def tick(self):
        # выборочно — 4 пары
        sample = ["EURUSD", "GBPUSD", "USDJPY", "AUDUSD"]
        out = {}
        for p in sample:
            df = yahoo.latest_bars(p, "1m", 5)
            if df.empty:
                out[p] = "no_data"
                continue
            age_min = (datetime.now(timezone.utc) - df.index[-1].to_pydatetime()).total_seconds() / 60
            out[p] = round(age_min, 1)
        return out


class DiskJanitor(Agent):
    name = "health_disk_janitor"
    category = "health"
    interval_sec = 600

    def tick(self):
        cleared = 0
        log_dir = config.LOGS_DIR
        cutoff = time.time() - 7 * 86400
        for f in log_dir.glob("*.log"):
            try:
                if f.stat().st_mtime < cutoff:
                    f.unlink()
                    cleared += 1
            except Exception:
                pass
        usage = shutil.disk_usage(str(config.LOGS_DIR))
        return {
            "removed_old_logs": cleared,
            "logs_dir_free_mb": round(usage.free / 1e6, 1),
        }


class APIHealthPinger(Agent):
    name = "health_api_health_pinger"
    category = "health"
    interval_sec = 300

    def tick(self):
        out = {}
        # forexfactory
        try:
            r = requests.head("https://nfs.faireconomy.media/ff_calendar_thisweek.xml", timeout=5)
            out["forexfactory"] = r.status_code
        except Exception as e:
            out["forexfactory"] = f"err: {e}"
        # yahoo
        try:
            df = yahoo.latest_bars("EURUSD", "1h", 5)
            out["yahoo"] = "ok" if not df.empty else "empty"
        except Exception as e:
            out["yahoo"] = f"err: {e}"
        return out
