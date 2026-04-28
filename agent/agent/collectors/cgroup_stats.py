"""Per-container CPU/memory stats by reading /sys/fs/cgroup directly.

Replaces Docker SDK's `container.stats(stream=False)`, which blocks ~1 s per
container while the daemon collects two samples to compute a delta. Reading
cgroup files takes microseconds and lets the agent keep a 1-second polling
cadence without measurable CPU cost on hosts with many containers.

Supports both cgroup v1 and v2. We resolve each container's cgroup path by
reading `/host/proc/<pid>/cgroup` once per container (cached) — the host pid
comes from a Docker inspect at the call site.

Returns CPU% in the same units as Docker stats: 100% == one fully-utilised CPU,
so an 8-core box can reach 800%.
"""

import logging
import os
import time as _time
from pathlib import Path

logger = logging.getLogger("glassops.agent.cgroup")

_CGROUP_ROOT = "/sys/fs/cgroup"
_HOST_PROC = os.environ.get("HOST_PROC", "/proc")

# v2 unified hierarchy is identified by the presence of cgroup.controllers at the root.
_IS_V2 = Path(f"{_CGROUP_ROOT}/cgroup.controllers").is_file()
# Best-effort presence check: if neither v2 nor a v1 controller dir exists, the host
# /sys/fs/cgroup probably isn't mounted into this container.
_AVAILABLE = _IS_V2 or Path(f"{_CGROUP_ROOT}/memory").is_dir() or Path(f"{_CGROUP_ROOT}/cpu,cpuacct").is_dir()
if not _AVAILABLE:
    logger.warning(
        "cgroup filesystem not accessible at %s — container CPU/MEM stats will be 0. "
        "Mount /sys/fs/cgroup:/sys/fs/cgroup:ro into the agent container to enable.",
        _CGROUP_ROOT,
    )

# Per-container state: short_id -> {"path": str, "last_usage_us": int, "last_ts": float}
_state: dict[str, dict] = {}


def _resolve_cgroup_path(host_pid: int) -> str | None:
    """Return the cgroup path under /sys/fs/cgroup for the given host pid, or None."""
    try:
        with open(f"{_HOST_PROC}/{host_pid}/cgroup", "r") as f:
            for line in f:
                # v2: "0::/system.slice/docker-<id>.scope"
                # v1: "<idx>:<controllers>:<path>"  — we want the cpu controller line
                parts = line.rstrip("\n").split(":", 2)
                if len(parts) != 3:
                    continue
                _, controllers, path = parts
                if _IS_V2:
                    if controllers == "":
                        return path
                else:
                    ctrl_set = set(controllers.split(",")) if controllers else set()
                    if "cpu" in ctrl_set or "cpuacct" in ctrl_set:
                        return path
    except FileNotFoundError:
        return None
    except Exception:
        logger.debug("Failed to read /proc/%d/cgroup", host_pid, exc_info=True)
        return None
    return None


def _read_int(path: str) -> int | None:
    try:
        with open(path, "r") as f:
            return int(f.read().strip())
    except (FileNotFoundError, ValueError):
        return None


def _read_v2(base: str) -> tuple[int, int, int] | None:
    """Returns (usage_microseconds, mem_bytes, mem_limit_bytes) for cgroup v2."""
    try:
        with open(f"{base}/cpu.stat", "r") as f:
            usage_us = 0
            for line in f:
                if line.startswith("usage_usec "):
                    usage_us = int(line.split()[1])
                    break
        mem = _read_int(f"{base}/memory.current") or 0
        try:
            with open(f"{base}/memory.max", "r") as f:
                raw = f.read().strip()
                mem_limit = 0 if raw == "max" else int(raw)
        except FileNotFoundError:
            mem_limit = 0
        return usage_us, mem, mem_limit
    except FileNotFoundError:
        return None


def _read_v1(base_cpu: str, base_mem: str) -> tuple[int, int, int] | None:
    """Returns (usage_microseconds, mem_bytes, mem_limit_bytes) for cgroup v1."""
    cpu_ns = _read_int(f"{base_cpu}/cpuacct.usage")
    if cpu_ns is None:
        return None
    mem = _read_int(f"{base_mem}/memory.usage_in_bytes") or 0
    mem_limit_raw = _read_int(f"{base_mem}/memory.limit_in_bytes") or 0
    # v1 reports an absurdly large value (≈ 8 EiB) when there's no limit set.
    mem_limit = 0 if mem_limit_raw > (1 << 60) else mem_limit_raw
    return cpu_ns // 1000, mem, mem_limit


def read(container_short_id: str, host_pid: int) -> dict | None:
    """Return {cpu_percent, mem_usage, mem_limit} for the container, or None on failure.

    First call returns 0.0 cpu_percent (no baseline yet); subsequent calls compute
    a delta against the previous sample.
    """
    if not _AVAILABLE:
        return None
    state = _state.get(container_short_id)
    if state is None:
        path = _resolve_cgroup_path(host_pid)
        if path is None:
            return None
        state = {"path": path, "last_usage_us": 0, "last_ts": 0.0}
        _state[container_short_id] = state

    path = state["path"]

    if _IS_V2:
        base = f"{_CGROUP_ROOT}{path}".rstrip("/") or _CGROUP_ROOT
        sample = _read_v2(base)
    else:
        base_cpu = f"{_CGROUP_ROOT}/cpu,cpuacct{path}"
        if not Path(base_cpu).is_dir():
            base_cpu = f"{_CGROUP_ROOT}/cpuacct{path}"
        base_mem = f"{_CGROUP_ROOT}/memory{path}"
        sample = _read_v1(base_cpu, base_mem)

    if sample is None:
        # Path went away (container restarted, etc.). Drop state so we re-resolve.
        _state.pop(container_short_id, None)
        return None

    usage_us, mem_usage, mem_limit = sample
    now = _time.time()

    last_usage = state["last_usage_us"]
    last_ts = state["last_ts"]
    state["last_usage_us"] = usage_us
    state["last_ts"] = now

    if last_ts == 0 or now <= last_ts:
        cpu_pct = 0.0
    else:
        delta_us = max(0, usage_us - last_usage)
        delta_s = now - last_ts
        cpu_pct = round((delta_us / 1_000_000.0) / delta_s * 100.0, 2)

    return {"cpu_percent": cpu_pct, "mem_usage": mem_usage, "mem_limit": mem_limit}


def gc(live_short_ids: set[str]) -> None:
    """Drop cached state for containers no longer present (avoids unbounded growth)."""
    for sid in list(_state.keys()):
        if sid not in live_short_ids:
            _state.pop(sid, None)
