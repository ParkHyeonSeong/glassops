"""Docker REST API router."""

import re

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.services.docker_service import (
    list_containers,
    container_action,
    container_logs,
    container_detail,
)

router = APIRouter(prefix="/api/docker", tags=["docker"])

CONTAINER_ID_PATTERN = re.compile(r"^[a-zA-Z0-9_.-]{1,128}$")


def _validate_id(container_id: str) -> str:
    if not CONTAINER_ID_PATTERN.match(container_id):
        raise HTTPException(400, "Invalid container ID")
    return container_id


class ActionRequest(BaseModel):
    action: str  # start | stop | restart


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
async def get_logs(container_id: str, tail: int = 200):
    cid = _validate_id(container_id)
    result = container_logs(cid, max(1, min(tail, 2000)))
    if not result.get("ok"):
        raise HTTPException(404, result.get("error", "Not found"))
    return result
