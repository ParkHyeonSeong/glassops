"""Docker REST API router."""

import re
from datetime import datetime

from fastapi import APIRouter, HTTPException, Query
from typing import Literal, Optional
from pydantic import BaseModel

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


class ActionRequest(BaseModel):
    action: Literal["start", "stop", "restart"]


@router.get("/containers")
async def get_containers():
    return {"containers": list_containers()}


@router.get("/containers/{container_id}")
async def get_container(container_id: str):
    cid = _validate_id(container_id)
    result = container_detail(cid)
    if not result.get("ok"):
        raise HTTPException(404, result.get("error", "Not found"))
    return result


@router.post("/containers/{container_id}/action")
async def post_action(container_id: str, body: ActionRequest):
    cid = _validate_id(container_id)
    result = container_action(cid, body.action)
    if not result.get("ok"):
        raise HTTPException(400, result.get("error", "Action failed"))
    return result


@router.get("/containers/{container_id}/logs")
async def get_logs(
    container_id: str,
    tail: int = 200,
    since: Optional[str] = Query(None, description="ISO8601 datetime"),
    until: Optional[str] = Query(None, description="ISO8601 datetime"),
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

    result = container_logs(cid, max(1, min(tail, 2000)), since_dt, until_dt)
    if not result.get("ok"):
        raise HTTPException(404, result.get("error", "Not found"))
    return result


@router.get("/images")
async def get_images():
    return {"images": list_images()}


@router.get("/volumes")
async def get_volumes():
    return {"volumes": list_volumes()}


@router.get("/networks")
async def get_networks():
    return {"networks": list_networks()}
