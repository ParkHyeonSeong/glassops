"""Log streaming REST API — reads system and Docker container logs.

Local agent calls hit the filesystem / docker SDK directly; remote agents go via RPC.
"""

import logging
import os
import re
from collections import deque
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query

from app.config import settings
from app.services.agent_dispatch import call_remote, is_local

logger = logging.getLogger("glassops.logs")

router = APIRouter(prefix="/api/logs", tags=["logs"])

_HOST_LOG = os.environ.get("HOST_LOG", "/var/log")

SYSTEM_LOG_PATHS = [
    f"{_HOST_LOG}/syslog",
    f"{_HOST_LOG}/messages",
    f"{_HOST_LOG}/auth.log",
    f"{_HOST_LOG}/kern.log",
    f"{_HOST_LOG}/nginx/access.log",
    f"{_HOST_LOG}/nginx/error.log",
    f"{_HOST_LOG}/dpkg.log",
    f"{_HOST_LOG}/daemon.log",
]

SAFE_ID_PATTERN = re.compile(r"^[a-zA-Z0-9_.-]{1,128}$")


@router.get("/sources")
async def list_sources(agent_id: str = Query(settings.local_agent_id)):
    if is_local(agent_id):
        return _local_sources()
    return await call_remote(agent_id, "log.sources")


@router.get("/read")
async def read_log(
    source_type: str = Query(...),
    name: str = Query(...),
    tail: int = Query(200, ge=1, le=5000),
    search: str = Query(""),
    agent_id: str = Query(settings.local_agent_id),
):
    if not SAFE_ID_PATTERN.match(name):
        raise HTTPException(400, "Invalid name")

    if is_local(agent_id):
        if source_type == "docker":
            return _read_docker_log(name, tail, search)
        if source_type == "system":
            return _read_system_log(name, tail, search)
        if source_type == "app":
            return {"lines": ["GlassOps internal log — coming in Phase 6"], "total": 1}
        raise HTTPException(400, "Unknown source type")

    return await call_remote(
        agent_id,
        "log.read",
        {"source_type": source_type, "name": name, "tail": tail, "search": search},
    )


def _local_sources() -> dict:
    sources: list[dict] = []
    for path in SYSTEM_LOG_PATHS:
        if os.path.isfile(path):
            sources.append({"type": "system", "name": Path(path).name})

    try:
        from app.services.docker_service import list_containers
        for c in list_containers():
            sources.append({"type": "docker", "name": c["name"], "container_id": c["id"]})
    except Exception:
        pass

    sources.append({"type": "app", "name": "glassops"})
    return {"sources": sources}


def _read_system_log(name: str, tail: int, search: str) -> dict:
    path = next((p for p in SYSTEM_LOG_PATHS if Path(p).name == name and os.path.isfile(p)), None)
    if not path:
        raise HTTPException(404, f"Log not found: {name}")

    try:
        with open(path, "r", errors="replace") as f:
            last_lines = deque(f, maxlen=tail * 2 if search else tail)

        lines = list(last_lines)
        if search:
            search_lower = search.lower()
            lines = [l for l in lines if search_lower in l.lower()]
            lines = lines[-tail:]

        return {"lines": [l.rstrip() for l in lines], "total": len(lines)}
    except PermissionError:
        raise HTTPException(403, "Permission denied")
    except Exception:
        logger.exception("Failed to read log: %s", name)
        raise HTTPException(500, "Failed to read log")


def _read_docker_log(container_id: str, tail: int, search: str) -> dict:
    try:
        from app.services.docker_service import container_logs
        result = container_logs(container_id, tail)
        if not result.get("ok"):
            raise HTTPException(404, "Container not found")

        lines = result["logs"].split("\n")
        if search:
            search_lower = search.lower()
            lines = [l for l in lines if search_lower in l.lower()]

        return {"lines": lines[-tail:], "total": len(lines)}
    except HTTPException:
        raise
    except Exception:
        logger.exception("Failed to read docker log: %s", container_id)
        raise HTTPException(500, "Failed to read log")
