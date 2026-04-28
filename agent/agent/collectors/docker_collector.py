"""Collects Docker container info via the Docker SDK + cgroup direct reads.

Container metadata (id, name, image, status, ports, …) comes from the SDK's list call.
Per-container CPU/memory metrics come from `cgroup_stats` reading /sys/fs/cgroup
directly — much cheaper than `container.stats(stream=False)`, which would block ~1 s
per container inside the SDK.
"""

import logging
import time as _time
from typing import Optional

from agent.collectors import cgroup_stats

logger = logging.getLogger("glassops.agent")

_client = None
_last_fail: float = 0
_RETRY_INTERVAL = 60


def _ensure_client() -> bool:
    global _client, _last_fail
    if _client is not None:
        return True
    if _last_fail and (_time.time() - _last_fail) < _RETRY_INTERVAL:
        return False
    try:
        import docker  # type: ignore[import-untyped]
        _client = docker.from_env()
        _client.ping()
        _last_fail = 0
        return True
    except Exception:
        _client = None
        _last_fail = _time.time()
        logger.info("Docker not available, will retry in %ds", _RETRY_INTERVAL)
        return False


def _safe_image_label(c) -> str:
    """Return a display string for the container's image, tolerating dangling/deleted images."""
    try:
        img = c.image
        if img.tags:
            return str(img.tags[0])
        return str(img.short_id)
    except Exception:
        raw = c.attrs.get("Image", "") or c.attrs.get("Config", {}).get("Image", "")
        if isinstance(raw, str) and raw.startswith("sha256:"):
            return raw[7:19]
        return raw or "<unknown>"


def _parse_ports(ports: dict) -> list[str]:
    result = []
    for container_port, bindings in (ports or {}).items():
        if bindings:
            for b in bindings:
                result.append(f"{b.get('HostPort', '?')}:{container_port}")
        else:
            result.append(container_port)
    return result


def collect_containers() -> Optional[list[dict]]:
    if not _ensure_client():
        return None

    try:
        containers = _client.containers.list(all=True)
        result = []
        live_ids: set[str] = set()
        for c in containers:
            labels = c.labels or {}
            stats = _stats_for(c) if c.status == "running" else None

            info: dict = {
                "id": c.short_id,
                "name": c.name,
                "image": _safe_image_label(c),
                "status": c.status,
                "state": c.attrs.get("State", {}).get("Status", "unknown"),
                "created": c.attrs.get("Created", ""),
                "ports": _parse_ports(c.attrs.get("NetworkSettings", {}).get("Ports", {})),
                "compose_project": labels.get("com.docker.compose.project", ""),
                "compose_service": labels.get("com.docker.compose.service", ""),
                "cpu_percent": stats.get("cpu_percent", 0.0) if stats else 0.0,
                "mem_usage": stats.get("mem_usage", 0) if stats else 0,
                "mem_limit": stats.get("mem_limit", 0) if stats else 0,
            }
            result.append(info)
            live_ids.add(c.short_id)

        # Drop caches for containers that disappeared so memory doesn't grow.
        cgroup_stats.gc(live_ids)
        for sid in list(_pid_cache.keys()):
            if sid not in live_ids:
                _pid_cache.pop(sid, None)
        return result
    except Exception:
        logger.exception("Failed to collect Docker containers")
        return None


# ── per-container stats via cgroup ────────────────────────────────────


# Cached host PID (and resolved cgroup path) per container short_id. Inspect is needed
# only the first time we see a container — it's a single Docker API call (~5 ms),
# vastly cheaper than the per-cycle `stats(stream=False)` it replaces.
_pid_cache: dict[str, int] = {}


def _stats_for(c) -> dict | None:
    short_id = c.short_id
    pid = _pid_cache.get(short_id)
    if pid is None:
        try:
            attrs = _client.api.inspect_container(c.id)
            pid = int(attrs.get("State", {}).get("Pid") or 0)
        except Exception:
            pid = 0
        if pid > 0:
            _pid_cache[short_id] = pid
        else:
            return None
    stats = cgroup_stats.read(short_id, pid)
    if stats is None:
        # Container restarted (different pid) or cgroup vanished — drop cache and retry next cycle.
        _pid_cache.pop(short_id, None)
    return stats
