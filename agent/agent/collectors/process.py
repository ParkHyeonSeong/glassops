"""Collects top processes by CPU/memory via psutil.

On hosts with thousands of processes (pid:host + many containers/threads), the
naive `process_iter` over the full attr set dominated agent CPU. Two changes:

1. The first pass requests only cheap fields. `username` is excluded because
   it requires a getpwuid_r per pid, which can hit NSS / LDAP and easily costs
   more than everything else combined.
2. We sort cheaply, then resolve `username` and `create_time` for the top-N
   only. UID → name lookups are cached via `lru_cache` so repeated UIDs don't
   re-enter NSS.
"""

import logging
from functools import lru_cache

import psutil

logger = logging.getLogger("glassops.agent")

MAX_PROCESSES = 80


@lru_cache(maxsize=4096)
def _username_for(uid: int) -> str:
    try:
        import pwd
        return pwd.getpwuid(uid).pw_name
    except (KeyError, ImportError):
        return str(uid)


def collect_processes() -> list[dict]:
    try:
        # Cheap pass: skip username (NSS lookup) and create_time on the full list.
        rough: list[dict] = []
        for proc in psutil.process_iter(
            ["pid", "name", "cpu_percent", "memory_percent", "status", "uids"]
        ):
            try:
                info = proc.info
                rough.append({
                    "pid": info["pid"],
                    "name": info["name"] or "unknown",
                    "cpu": info["cpu_percent"] or 0,
                    "mem": round(info["memory_percent"] or 0, 1),
                    "status": info["status"] or "unknown",
                    # uids is a namedtuple-like (real, effective, saved); pick effective.
                    "_uid": info["uids"].effective if info["uids"] else 0,
                })
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue

        rough.sort(key=lambda p: (p["cpu"], p["mem"]), reverse=True)
        top = rough[:MAX_PROCESSES]

        # Detailed pass: resolve username + create_time only for the top N.
        result: list[dict] = []
        for r in top:
            user = _username_for(r["_uid"])
            started = 0
            try:
                started = psutil.Process(r["pid"]).create_time()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
            result.append({
                "pid": r["pid"],
                "name": r["name"],
                "cpu": r["cpu"],
                "mem": r["mem"],
                "user": user,
                "status": r["status"],
                "started": started,
            })
        return result
    except Exception:
        logger.exception("Failed to collect processes")
        return []
