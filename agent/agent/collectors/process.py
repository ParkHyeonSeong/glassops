"""Collects top processes by CPU/memory via psutil."""

import logging
import time as _time

import psutil

logger = logging.getLogger("glassops.agent")

MAX_PROCESSES = 80


def collect_processes() -> list[dict]:
    try:
        procs = []
        for proc in psutil.process_iter(
            ["pid", "name", "cpu_percent", "memory_percent", "username", "status", "create_time"]
        ):
            try:
                info = proc.info
                procs.append({
                    "pid": info["pid"],
                    "name": info["name"] or "unknown",
                    "cpu": info["cpu_percent"] or 0,
                    "mem": round(info["memory_percent"] or 0, 1),
                    "user": info["username"] or "",
                    "status": info["status"] or "unknown",
                    "started": info["create_time"] or 0,
                })
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue

        # Sort by CPU desc, then MEM desc — return top N
        procs.sort(key=lambda p: (p["cpu"], p["mem"]), reverse=True)
        return procs[:MAX_PROCESSES]
    except Exception:
        logger.exception("Failed to collect processes")
        return []
