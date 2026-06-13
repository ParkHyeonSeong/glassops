"""Docker REST API router — all calls go through the agent RPC layer (the bundled
local agent for agent_id=local, remote agents otherwise). The backend itself holds
no docker socket, so a backend compromise can't reach the host Docker daemon."""

import re
from datetime import datetime
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from app.config import settings
from app.database import audit
from app.dependencies import require_admin
from app.services.agent_dispatch import call_remote

router = APIRouter(prefix="/api/docker", tags=["docker"])

CONTAINER_ID_PATTERN = re.compile(r"^[a-zA-Z0-9_.-]{1,128}$")


def _validate_id(container_id: str) -> str:
    if not CONTAINER_ID_PATTERN.match(container_id):
        raise HTTPException(400, "Invalid container ID")
    return container_id


def _agent_param() -> str:
    return Query(settings.local_agent_id, pattern=r"^[a-zA-Z0-9_-]{1,64}$",
                 description="Agent ID; defaults to local")


class ActionRequest(BaseModel):
    action: Literal["start", "stop", "restart"]


@router.get("/containers")
async def get_containers(agent_id: str = _agent_param()):
    return await call_remote(agent_id, "docker.list")


@router.get("/containers/{container_id}")
async def get_container(container_id: str, agent_id: str = _agent_param()):
    cid = _validate_id(container_id)
    return await call_remote(agent_id, "docker.detail", {"container_id": cid})


@router.post("/containers/{container_id}/action")
async def post_action(container_id: str, body: ActionRequest, agent_id: str = _agent_param(),
                      actor: str = Depends(require_admin)):
    cid = _validate_id(container_id)
    result = await call_remote(agent_id, "docker.action", {"container_id": cid, "action": body.action})
    await audit(actor, f"docker.{body.action}", agent_id, {"container": cid, "ok": result.get("ok", True)})
    return result


@router.get("/containers/{container_id}/logs")
async def get_logs(
    container_id: str,
    tail: int = 200,
    since: Optional[str] = Query(None, description="ISO8601 datetime"),
    until: Optional[str] = Query(None, description="ISO8601 datetime"),
    agent_id: str = _agent_param(),
    _: str = Depends(require_admin),
):
    cid = _validate_id(container_id)

    try:
        since_dt = datetime.fromisoformat(since) if since is not None else None
        until_dt = datetime.fromisoformat(until) if until is not None else None
    except ValueError:
        raise HTTPException(400, "Invalid ISO8601 datetime for since/until")

    if since_dt and until_dt and since_dt >= until_dt:
        raise HTTPException(400, "'since' must be before 'until'")

    tail = max(1, min(tail, 2000))

    params: dict = {"container_id": cid, "tail": tail}
    if since:
        params["since"] = since
    if until:
        params["until"] = until
    return await call_remote(agent_id, "docker.logs", params)


@router.get("/images")
async def get_images(agent_id: str = _agent_param()):
    return await call_remote(agent_id, "docker.images")


@router.get("/volumes")
async def get_volumes(agent_id: str = _agent_param()):
    return await call_remote(agent_id, "docker.volumes")


@router.get("/networks")
async def get_networks(agent_id: str = _agent_param()):
    return await call_remote(agent_id, "docker.networks")
