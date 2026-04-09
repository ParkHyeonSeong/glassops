"""Docker service — direct Docker SDK calls from backend for container actions."""

import logging
import time as _time

logger = logging.getLogger("glassops.docker")

_client = None
_last_fail: float = 0
_RETRY_INTERVAL = 60

_SENSITIVE_KEYS = {"PASSWORD", "SECRET", "KEY", "TOKEN", "CREDENTIAL", "API_KEY"}


def _mask_env(env_list: list[str]) -> list[str]:
    result = []
    for entry in env_list:
        if "=" in entry:
            key, _ = entry.split("=", 1)
            if any(s in key.upper() for s in _SENSITIVE_KEYS):
                result.append(f"{key}=********")
            else:
                result.append(entry)
        else:
            result.append(entry)
    return result


def _get_client():
    global _client, _last_fail
    if _client is not None:
        return _client
    if _last_fail and (_time.time() - _last_fail) < _RETRY_INTERVAL:
        return None
    try:
        import docker  # type: ignore[import-untyped]
        _client = docker.from_env()
        _client.ping()
        _last_fail = 0
        return _client
    except Exception:
        _client = None
        _last_fail = _time.time()
        logger.warning("Docker not available, will retry in %ds", _RETRY_INTERVAL)
        return None


def list_containers() -> list[dict]:
    client = _get_client()
    if not client:
        return []
    try:
        containers = client.containers.list(all=True)
        return [
            {
                "id": c.short_id,
                "name": c.name,
                "image": str(c.image.tags[0]) if c.image.tags else str(c.image.short_id),
                "status": c.status,
                "state": c.attrs.get("State", {}).get("Status", "unknown"),
            }
            for c in containers
        ]
    except Exception:
        logger.exception("Failed to list containers")
        return []


def container_action(container_id: str, action: str) -> dict:
    """Execute start/stop/restart on a container."""
    client = _get_client()
    if not client:
        return {"ok": False, "error": "Docker not available"}

    if action not in ("start", "stop", "restart"):
        return {"ok": False, "error": f"Invalid action: {action}"}

    try:
        container = client.containers.get(container_id)
        getattr(container, action)()
        return {"ok": True, "container": container.name, "action": action}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def container_logs(container_id: str, tail: int = 200) -> dict:
    client = _get_client()
    if not client:
        return {"ok": False, "error": "Docker not available"}

    try:
        container = client.containers.get(container_id)
        logs = container.logs(tail=tail, timestamps=True).decode("utf-8", errors="replace")
        return {"ok": True, "container": container.name, "logs": logs}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def container_detail(container_id: str) -> dict:
    client = _get_client()
    if not client:
        return {"ok": False, "error": "Docker not available"}

    try:
        container = client.containers.get(container_id)
        attrs = container.attrs
        return {
            "ok": True,
            "id": container.short_id,
            "name": container.name,
            "image": str(container.image.tags[0]) if container.image.tags else str(container.image.short_id),
            "status": container.status,
            "created": attrs.get("Created", ""),
            "ports": attrs.get("NetworkSettings", {}).get("Ports", {}),
            "env": _mask_env(attrs.get("Config", {}).get("Env", [])),
            "mounts": [
                {"source": m.get("Source", ""), "destination": m.get("Destination", ""), "mode": m.get("Mode", "")}
                for m in attrs.get("Mounts", [])
            ],
            "networks": list(attrs.get("NetworkSettings", {}).get("Networks", {}).keys()),
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


def list_images() -> list[dict]:
    client = _get_client()
    if not client:
        return []
    try:
        return [
            {
                "id": img.short_id.replace("sha256:", ""),
                "tags": img.tags,
                "size": img.attrs.get("Size", 0),
                "created": img.attrs.get("Created", ""),
            }
            for img in client.images.list()
        ]
    except Exception:
        return []


def list_volumes() -> list[dict]:
    client = _get_client()
    if not client:
        return []
    try:
        return [
            {
                "name": v.name,
                "driver": v.attrs.get("Driver", ""),
                "mountpoint": v.attrs.get("Mountpoint", ""),
            }
            for v in client.volumes.list()
        ]
    except Exception:
        return []


def list_networks() -> list[dict]:
    client = _get_client()
    if not client:
        return []
    try:
        return [
            {
                "id": n.short_id,
                "name": n.name,
                "driver": n.attrs.get("Driver", ""),
                "scope": n.attrs.get("Scope", ""),
            }
            for n in client.networks.list()
        ]
    except Exception:
        return []
