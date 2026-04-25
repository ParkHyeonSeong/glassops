"""Docker REST API router — local agent calls go straight to docker SDK; remote agents go via RPC."""

import re
from datetime import datetime
from typing import Literal, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from app.config import settings
from app.services.agent_dispatch import call_remote, is_local
from app.services.docker_service import (
    list_containers,
    container_action,
    container_logs,
    container_detail,
    list_images,
    list_volumes,
    list_networks,
)

router = APIRouter(prefix="/api/docker", tags=["docker"])

CONTAINER_ID_PATTERN = re.compile(r"^[a-zA-Z0-9_.-]{1,128}$")


def _validate_id(container_id: str) -> str:
    if not CONTAINER_ID_PATTERN.match(container_id):
        raise HTTPException(400, "Invalid container ID")
    return container_id


def _agent_param() -> str:
    return Query(settings.local_agent_id, description="Agent ID; defaults to local")


class ActionRequest(BaseModel):
    action: Literal["start", "stop", "restart"]


@router.get("/containers")
async def get_containers(agent_id: str = _agent_param()):
    if is_local(agent_id):
        return {"containers": list_containers()}
    return await call_remote(agent_id, "docker.list")


@router.get("/containers/{container_id}")
async def get_container(container_id: str, agent_id: str = _agent_param()):
    cid = _validate_id(container_id)
    if is_local(agent_id):
        result = container_detail(cid)
        if not result.get("ok"):
            raise HTTPException(404, result.get("error", "Not found"))
        return result
    return await call_remote(agent_id, "docker.detail", {"container_id": cid})


@router.post("/containers/{container_id}/action")
async def post_action(container_id: str, body: ActionRequest, agent_id: str = _agent_param()):
    cid = _validate_id(container_id)
    if is_local(agent_id):
        result = container_action(cid, body.action)
        if not result.get("ok"):
            raise HTTPException(400, result.get("error", "Action failed"))
        return result
    return await call_remote(agent_id, "docker.action", {"container_id": cid, "action": body.action})


@router.get("/containers/{container_id}/logs")
async def get_logs(
    container_id: str,
    tail: int = 200,
    since: Optional[str] = Query(None, description="ISO8601 datetime"),
    until: Optional[str] = Query(None, description="ISO8601 datetime"),
    agent_id: str = _agent_param(),
):
    cid = _validate_id(container_id)

    since_dt: datetime | None = None
    until_dt: datetime | None = None
    try:
        if since is not None:
            since_dt = datetime.fromisoformat(since)
        if until is not None:
            until_dt = datetime.fromisoformat(until)
    except ValueError:
        raise HTTPException(400, "Invalid ISO8601 datetime for since/until")

    if since_dt and until_dt and since_dt >= until_dt:
        raise HTTPException(400, "'since' must be before 'until'")

    tail = max(1, min(tail, 2000))

    if is_local(agent_id):
        result = container_logs(cid, tail, since_dt, until_dt)
        if not result.get("ok"):
            raise HTTPException(404, result.get("error", "Not found"))
        return result

    params: dict = {"container_id": cid, "tail": tail}
    if since:
        params["since"] = since
    if until:
        params["until"] = until
    return await call_remote(agent_id, "docker.logs", params)


@router.get("/images")
async def get_images(agent_id: str = _agent_param()):
    if is_local(agent_id):
        return {"images": list_images()}
    return await call_remote(agent_id, "docker.images")


@router.get("/volumes")
async def get_volumes(agent_id: str = _agent_param()):
    if is_local(agent_id):
        return {"volumes": list_volumes()}
    return await call_remote(agent_id, "docker.volumes")


@router.get("/networks")
async def get_networks(agent_id: str = _agent_param()):
    if is_local(agent_id):
        return {"networks": list_networks()}
    return await call_remote(agent_id, "docker.networks")
