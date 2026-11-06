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


def _has_gpu_reservation(c) -> bool:
    """True if the container reserves NVIDIA GPU capacity at the runtime layer.

    Detects both --gpus / DeviceRequests (driver=nvidia, capabilities contain gpu)
    and the legacy nvidia-runtime case. We don't require a process to be using the
    GPU yet — the reservation alone means the dashboard should show idle GPU charts
    so users can watch them ramp up.
    """
    try:
        host_cfg = c.attrs.get("HostConfig", {}) or {}
        # Modern path: --gpus / docker-compose deploy.resources.devices nvidia
        for req in host_cfg.get("DeviceRequests") or []:
            driver = (req.get("Driver") or "").lower()
            caps = req.get("Capabilities") or []
            flat = {cap for group in caps for cap in (group or [])}
            if driver == "nvidia" or "gpu" in flat or "compute" in flat:
                return True
        # Legacy path: explicit --runtime=nvidia
        if (host_cfg.get("Runtime") or "").lower() == "nvidia":
            return True
        # Fallback: NVIDIA_VISIBLE_DEVICES env (set by nvidia-container-toolkit).
        # `=void` and `=none` are explicit opt-outs (toolkit convention); empty value
        # means no devices either. Anything else (all / 0 / GPU-UUID / index list)
        # is a real reservation.
        env = c.attrs.get("Config", {}).get("Env") or []
        for e in env:
            if not e.startswith("NVIDIA_VISIBLE_DEVICES="):
                continue
            val = e[len("NVIDIA_VISIBLE_DEVICES="):].strip().lower()
            if val and val not in ("void", "none"):
                return True
    except Exception:
        pass
    return False


def _num(v, kind=float):
    """A non-negative number of the given kind, else None (bool and strings rejected)."""
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0:
        return None
    return kind(v)


def container_limits(attrs: dict) -> dict:
    """CPU / memory limits from HostConfig; each field falls back to its default on its own."""
    host = attrs.get("HostConfig") if isinstance(attrs, dict) else None
    if not isinstance(host, dict):
        host = {}
    cpus = None
    nano = _num(host.get("NanoCpus"))
    quota = _num(host.get("CpuQuota"))
    period = _num(host.get("CpuPeriod"))
    if nano and nano > 0:
        cpus = round(nano / 1e9, 3)
    elif quota and quota > 0:
        # Docker stores CpuPeriod 0 for `--cpu-quota` alone: the kernel default period applies.
        cpus = round(quota / (period if period and period > 0 else 100_000), 3)
    cpuset = host.get("CpusetCpus")
    return {
        "cpus": cpus,
        "cpuset": cpuset if isinstance(cpuset, str) else "",
        "memory": _num(host.get("Memory"), int) or 0,
        "memory_reservation": _num(host.get("MemoryReservation"), int) or 0,
    }


def container_times(attrs: dict) -> dict:
    """Start / finish timestamps and exit code from State; Docker's zero time becomes ''."""
    state = attrs.get("State") if isinstance(attrs, dict) else None
    if not isinstance(state, dict):
        state = {}

    def ts(v) -> str:
        return v if isinstance(v, str) and v != "0001-01-01T00:00:00Z" else ""

    code = state.get("ExitCode")
    return {
        "started_at": ts(state.get("StartedAt")),
        "finished_at": ts(state.get("FinishedAt")),
        "exit_code": code if isinstance(code, int) and not isinstance(code, bool) else None,
    }


def gpu_request(attrs: dict) -> tuple[bool, tuple[str, ...]]:
    """(all_gpus, raw_ids) a container asks for: DeviceRequests first, nvidia-runtime env otherwise."""
    host = attrs.get("HostConfig") if isinstance(attrs, dict) else None
    if not isinstance(host, dict):
        host = {}
    all_gpus = False
    ids: list[str] = []
    counted = False
    for req in host.get("DeviceRequests") or []:
        if not isinstance(req, dict):
            continue
        caps = req.get("Capabilities") or []
        flat = {cap for group in caps for cap in (group or [])}
        if (req.get("Driver") or "").lower() != "nvidia" and "gpu" not in flat:
            continue
        counted = True
        dev_ids = [str(i) for i in (req.get("DeviceIDs") or [])]
        count = req.get("Count")
        if dev_ids:
            ids.extend(dev_ids)
        elif isinstance(count, int) and count == -1:
            all_gpus = True
        elif isinstance(count, int) and count > 0:
            ids.extend(str(i) for i in range(count))
    if not counted and (host.get("Runtime") or "").lower() == "nvidia":
        cfg = attrs.get("Config") if isinstance(attrs.get("Config"), dict) else {}
        for e in cfg.get("Env") or []:
            if not isinstance(e, str) or not e.startswith("NVIDIA_VISIBLE_DEVICES="):
                continue
            val = e[len("NVIDIA_VISIBLE_DEVICES="):].strip()
            if val.lower() == "all":
                all_gpus = True
            elif val.lower() not in ("", "none", "void"):
                ids.extend(p.strip() for p in val.split(",") if p.strip())
    return all_gpus, tuple(dict.fromkeys(ids))


def resolve_gpu_indices(request: tuple[bool, tuple[str, ...]], gpus: Optional[list[dict]]) -> list[int]:
    """Map a gpu_request onto the host's GPU indices; unknown ids are dropped."""
    if not gpus:
        return []
    all_gpus, ids = request
    by_index = {g.get("index") for g in gpus if isinstance(g.get("index"), int)}
    by_uuid = {g.get("uuid"): g.get("index") for g in gpus
               if isinstance(g.get("index"), int) and isinstance(g.get("uuid"), str)}
    if all_gpus:
        return sorted(by_index)
    out: set[int] = set()
    for raw in ids:
        if raw.isdecimal():
            if int(raw) in by_index:
                out.add(int(raw))
        elif raw.startswith(("GPU-", "MIG-")) and raw in by_uuid:
            out.add(by_uuid[raw])
    return sorted(out)


def collect_containers(gpus: Optional[list[dict]] = None,
                       own_container_id: Optional[str] = None) -> Optional[list[dict]]:
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
                "gpu_reserved": _has_gpu_reservation(c),
            }
            try:
                info["limits"] = container_limits(c.attrs)
                info.update(container_times(c.attrs))
                info["gpu_reserved_indices"] = (
                    [] if c.short_id == own_container_id
                    else resolve_gpu_indices(gpu_request(c.attrs), gpus))
            except Exception:
                # The fields read so far stay; the rest are left out, which the frontend
                # reads as "unknown" (a default such as `limits: {}` would read as "no limit").
                logger.debug("topology fields failed for %s", c.name, exc_info=True)
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
