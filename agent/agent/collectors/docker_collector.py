"""Collects Docker container info via Docker SDK. Gracefully returns empty if unavailable."""

import logging
from typing import Optional

logger = logging.getLogger("glassops.agent")

import time as _time

_client = None
_last_fail: float = 0
_RETRY_INTERVAL = 60  # seconds before retrying after failure


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


def collect_containers() -> Optional[list[dict]]:
    if not _ensure_client():
        return None

    try:
        containers = _client.containers.list(all=True)
        result = []
        for c in containers:
            labels = c.labels or {}
            info: dict = {
                "id": c.short_id,
                "name": c.name,
                "image": str(c.image.tags[0]) if c.image.tags else str(c.image.short_id),
                "status": c.status,
                "state": c.attrs.get("State", {}).get("Status", "unknown"),
                "created": c.attrs.get("Created", ""),
                "ports": _parse_ports(c.attrs.get("NetworkSettings", {}).get("Ports", {})),
                "compose_project": labels.get("com.docker.compose.project", ""),
                "compose_service": labels.get("com.docker.compose.service", ""),
            }

            # Get resource stats for running containers
            if c.status == "running":
                try:
                    stats = c.stats(stream=False)
                    info["cpu_percent"] = _calc_cpu_percent(stats)
                    info["mem_usage"] = stats.get("memory_stats", {}).get("usage", 0)
                    info["mem_limit"] = stats.get("memory_stats", {}).get("limit", 0)
                except Exception:
                    info["cpu_percent"] = 0
                    info["mem_usage"] = 0
                    info["mem_limit"] = 0
            else:
                info["cpu_percent"] = 0
                info["mem_usage"] = 0
                info["mem_limit"] = 0

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
