"""Log streaming REST API — reads system and Docker container logs via the agent
RPC layer (the bundled local agent, or remote agents). The backend reads no host
logs itself, so it needs no host log mount or root."""

import re

from fastapi import APIRouter, Depends, HTTPException, Query

from app.config import settings
from app.dependencies import require_admin
from app.services.agent_dispatch import call_remote

router = APIRouter(prefix="/api/logs", tags=["logs"])

SAFE_ID_PATTERN = re.compile(r"^[a-zA-Z0-9_.-]{1,128}$")


@router.get("/sources")
async def list_sources(agent_id: str = Query(settings.local_agent_id),
                       _: str = Depends(require_admin)):
    return await call_remote(agent_id, "log.sources")


@router.get("/read")
async def read_log(
    source_type: str = Query(...),
    name: str = Query(...),
    tail: int = Query(200, ge=1, le=5000),
    search: str = Query(""),
    agent_id: str = Query(settings.local_agent_id),
    _: str = Depends(require_admin),
):
    if not SAFE_ID_PATTERN.match(name):
        raise HTTPException(400, "Invalid name")
    return await call_remote(
        agent_id, "log.read",
        {"source_type": source_type, "name": name, "tail": tail, "search": search},
    )
