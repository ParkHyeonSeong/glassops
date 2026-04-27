"""Collects Docker container info via Docker SDK. Gracefully returns empty if unavailable."""

import logging
import threading
import time as _time
from typing import Optional

logger = logging.getLogger("glassops.agent")

_client = None
_last_fail: float = 0
_RETRY_INTERVAL = 60

# Cached stats — updated in background thread
_stats_cache: dict[str, dict] = {}  # container_id -> {cpu_percent, mem_usage, mem_limit}
_stats_thread: threading.Thread | None = None
_stats_running = False


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


def _update_stats_background():
    """Background thread that collects container stats without blocking the main loop."""
    global _stats_running
    _stats_running = True

    while _stats_running and _client:
        try:
            containers = _client.containers.list()
            new_cache: dict[str, dict] = {}
            for c in containers:
                if c.status != "running":
                    continue
                try:
                    stats = c.stats(stream=False)
                    new_cache[c.short_id] = {
                        "cpu_percent": _calc_cpu_percent(stats),
                        "mem_usage": stats.get("memory_stats", {}).get("usage", 0),
                        "mem_limit": stats.get("memory_stats", {}).get("limit", 0),
                    }
                except Exception:
                    pass

            _stats_cache.clear()
            _stats_cache.update(new_cache)
        except Exception:
            logger.debug("Stats background update failed", exc_info=True)

        # Wait 10s between full stats cycles
        _time.sleep(10)


def _ensure_stats_thread():
    global _stats_thread
    if _stats_thread is None or not _stats_thread.is_alive():
        _stats_thread = threading.Thread(target=_update_stats_background, daemon=True)
        _stats_thread.start()


def _safe_image_label(c) -> str:
    """Return a display string for the container's image, tolerating dangling/deleted images."""
    try:
        img = c.image
        if img.tags:
            return str(img.tags[0])
        return str(img.short_id)
    except Exception:
        # Image was removed but container still exists — fall back to raw image ref.
        raw = c.attrs.get("Image", "") or c.attrs.get("Config", {}).get("Image", "")
        if isinstance(raw, str) and raw.startswith("sha256:"):
            return raw[7:19]
        return raw or "<unknown>"


def collect_containers() -> Optional[list[dict]]:
    if not _ensure_client():
        return None

    _ensure_stats_thread()

    try:
        containers = _client.containers.list(all=True)
        result = []
        for c in containers:
            labels = c.labels or {}
            cached = _stats_cache.get(c.short_id, {})

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
                "cpu_percent": cached.get("cpu_percent", 0),
                "mem_usage": cached.get("mem_usage", 0),
                "mem_limit": cached.get("mem_limit", 0),
            }
            result.append(info)
        return result
    except Exception:
        logger.exception("Failed to collect Docker containers")
        return None


def _parse_ports(ports: dict) -> list[str]:
    result = []
    for container_port, bindings in (ports or {}).items():
        if bindings:
            for b in bindings:
                result.append(f"{b.get('HostPort', '?')}:{container_port}")
        else:
            result.append(container_port)
    return result


def _calc_cpu_percent(stats: dict) -> float:
    try:
        cpu = stats["cpu_stats"]
        pre = stats["precpu_stats"]
        delta = cpu["cpu_usage"]["total_usage"] - pre["cpu_usage"]["total_usage"]
        system_delta = cpu["system_cpu_usage"] - pre["system_cpu_usage"]
        n_cpus = cpu.get("online_cpus", len(cpu["cpu_usage"].get("percpu_usage", [1])))
        if system_delta > 0 and delta > 0:
            return round((delta / system_delta) * n_cpus * 100, 2)
    except (KeyError, ZeroDivisionError):
        pass
    return 0.0
