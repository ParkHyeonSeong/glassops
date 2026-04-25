"""RPC handlers — invoked by ws_client when an rpc.req arrives from the backend."""

import logging
import os
import re
import signal
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger("glassops.agent.rpc")

_SENSITIVE_KEYS = {"PASSWORD", "SECRET", "KEY", "TOKEN", "CREDENTIAL", "API_KEY"}
_HOST_LOG = os.environ.get("HOST_LOG", "/var/log")
_SYSTEM_LOG_PATHS = [
    f"{_HOST_LOG}/syslog",
    f"{_HOST_LOG}/messages",
    f"{_HOST_LOG}/auth.log",
    f"{_HOST_LOG}/kern.log",
    f"{_HOST_LOG}/nginx/access.log",
    f"{_HOST_LOG}/nginx/error.log",
    f"{_HOST_LOG}/dpkg.log",
    f"{_HOST_LOG}/daemon.log",
]
_SAFE_NAME = re.compile(r"^[a-zA-Z0-9_.-]{1,128}$")


def _docker_client():
    from agent.collectors import docker_collector
    if not docker_collector._ensure_client():
        raise RuntimeError("Docker not available on this host")
    return docker_collector._client


def _mask_env(env_list: list[str]) -> list[str]:
    out = []
    for entry in env_list:
        if "=" in entry:
            key, _ = entry.split("=", 1)
            if any(s in key.upper() for s in _SENSITIVE_KEYS):
                out.append(f"{key}=********")
                continue
        out.append(entry)
    return out


def _parse_ports(ports: dict) -> list[str]:
    out = []
    for cport, bindings in (ports or {}).items():
        if bindings:
            for b in bindings:
                out.append(f"{b.get('HostPort', '?')}:{cport}")
        else:
            out.append(cport)
    return out


# ── docker handlers ───────────────────────────────────────────────────


def docker_list(_: dict) -> dict:
    from agent.collectors.docker_collector import collect_containers
    containers = collect_containers() or []
    return {"containers": containers}


def docker_detail(params: dict) -> dict:
    cid = params.get("container_id", "")
    if not _SAFE_NAME.match(cid):
        raise ValueError("Invalid container ID")
    c = _docker_client().containers.get(cid)
    attrs = c.attrs
    return {
        "ok": True,
        "id": c.short_id,
        "name": c.name,
        "image": str(c.image.tags[0]) if c.image.tags else str(c.image.short_id),
        "status": c.status,
        "created": attrs.get("Created", ""),
        "ports": attrs.get("NetworkSettings", {}).get("Ports", {}),
        "env": _mask_env(attrs.get("Config", {}).get("Env", [])),
        "mounts": [
            {"source": m.get("Source", ""), "destination": m.get("Destination", ""), "mode": m.get("Mode", "")}
            for m in attrs.get("Mounts", [])
        ],
        "networks": list(attrs.get("NetworkSettings", {}).get("Networks", {}).keys()),
    }


def docker_logs(params: dict) -> dict:
    cid = params.get("container_id", "")
    if not _SAFE_NAME.match(cid):
        raise ValueError("Invalid container ID")
    tail = max(1, min(int(params.get("tail", 200)), 2000))
    since = params.get("since")
    until = params.get("until")
    kwargs: dict[str, Any] = {"timestamps": True, "tail": tail}
    if since:
        kwargs["since"] = datetime.fromisoformat(since)
    if until:
        kwargs["until"] = datetime.fromisoformat(until)
    c = _docker_client().containers.get(cid)
    logs = c.logs(**kwargs).decode("utf-8", errors="replace")
    return {"ok": True, "container": c.name, "logs": logs}


def docker_action(params: dict) -> dict:
    cid = params.get("container_id", "")
    action = params.get("action", "")
    if not _SAFE_NAME.match(cid):
        raise ValueError("Invalid container ID")
    if action not in ("start", "stop", "restart"):
        raise ValueError(f"Invalid action: {action}")
    c = _docker_client().containers.get(cid)
    getattr(c, action)()
    return {"ok": True, "container": c.name, "action": action}


def docker_images(_: dict) -> dict:
    images = []
    for img in _docker_client().images.list():
        images.append({
            "id": img.short_id.replace("sha256:", ""),
            "tags": img.tags,
            "size": img.attrs.get("Size", 0),
            "created": img.attrs.get("Created", ""),
        })
    return {"images": images}


def docker_volumes(_: dict) -> dict:
    volumes = []
    for v in _docker_client().volumes.list():
        volumes.append({
            "name": v.name,
            "driver": v.attrs.get("Driver", ""),
            "mountpoint": v.attrs.get("Mountpoint", ""),
        })
    return {"volumes": volumes}


def docker_networks(_: dict) -> dict:
    networks = []
    for n in _docker_client().networks.list():
        networks.append({
            "id": n.short_id,
            "name": n.name,
            "driver": n.attrs.get("Driver", ""),
            "scope": n.attrs.get("Scope", ""),
        })
    return {"networks": networks}


# ── process handlers ──────────────────────────────────────────────────


_PROTECTED_PIDS = {0, 1, os.getpid(), os.getppid()}


def process_kill(params: dict) -> dict:
    pid = int(params.get("pid", -1))
    _PROTECTED_PIDS.update({os.getpid(), os.getppid()})
    if pid in _PROTECTED_PIDS:
        raise PermissionError("Cannot kill protected process")
    if pid < 0:
        raise ValueError("Invalid PID")
    os.kill(pid, signal.SIGTERM)
    return {"ok": True, "pid": pid, "signal": "SIGTERM"}


# ── log handlers ──────────────────────────────────────────────────────


def log_sources(_: dict) -> dict:
    sources = []
    for path in _SYSTEM_LOG_PATHS:
        if os.path.isfile(path):
            sources.append({"type": "system", "name": Path(path).name})
    try:
        for c in _docker_client().containers.list(all=True):
            sources.append({"type": "docker", "name": c.name, "container_id": c.short_id})
    except Exception:
        pass
    sources.append({"type": "app", "name": "glassops"})
    return {"sources": sources}


def log_read(params: dict) -> dict:
    source_type = params.get("source_type", "")
    name = params.get("name", "")
    tail = max(1, min(int(params.get("tail", 200)), 5000))
    search = params.get("search", "") or ""
    if not _SAFE_NAME.match(name):
        raise ValueError("Invalid name")

    if source_type == "docker":
        c = _docker_client().containers.get(name)
        body = c.logs(timestamps=True, tail=tail).decode("utf-8", errors="replace")
        lines = body.split("\n")
        if search:
            sl = search.lower()
            lines = [l for l in lines if sl in l.lower()]
        return {"lines": lines[-tail:], "total": len(lines)}

    if source_type == "system":
        path = next((p for p in _SYSTEM_LOG_PATHS if Path(p).name == name and os.path.isfile(p)), None)
        if not path:
            raise FileNotFoundError(f"Log not found: {name}")
        with open(path, "r", errors="replace") as f:
            last_lines = deque(f, maxlen=tail * 2 if search else tail)
        lines = list(last_lines)
        if search:
            sl = search.lower()
            lines = [l for l in lines if sl in l.lower()]
            lines = lines[-tail:]
        return {"lines": [l.rstrip() for l in lines], "total": len(lines)}

    if source_type == "app":
        return {"lines": ["GlassOps internal log — coming soon"], "total": 1}

    raise ValueError(f"Unknown source type: {source_type}")


HANDLERS: dict[str, Callable[[dict], dict]] = {
    "docker.list": docker_list,
    "docker.detail": docker_detail,
    "docker.logs": docker_logs,
    "docker.action": docker_action,
    "docker.images": docker_images,
    "docker.volumes": docker_volumes,
    "docker.networks": docker_networks,
    "process.kill": process_kill,
    "log.sources": log_sources,
    "log.read": log_read,
}


def dispatch(method: str, params: dict) -> dict:
    handler = HANDLERS.get(method)
    if handler is None:
        raise ValueError(f"Unknown RPC method: {method}")
    return handler(params or {})
