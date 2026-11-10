"""Host logs use agent RPC; the dashboard's own archive uses its data volume."""

import asyncio
import re

from fastapi import APIRouter, Depends, HTTPException, Query

from app.config import settings
from app import runtime_logs
from app.dependencies import require_admin
from app.services.agent_dispatch import call_remote

router = APIRouter(prefix="/api/logs", tags=["logs"])

SAFE_ID_PATTERN = re.compile(r"^[a-zA-Z0-9_.-]{1,128}$")


@router.get("/sources")
async def list_sources(agent_id: str = Query(settings.local_agent_id, pattern=r"^[a-zA-Z0-9_-]{1,64}$"),
                       _: str = Depends(require_admin)):
    try:
        result = await call_remote(agent_id, "log.sources")
    except HTTPException as exc:
        if agent_id != settings.local_agent_id or exc.status_code not in (503, 504):
            raise
        result = {"sources": []}
    sources = [source for source in result.get("sources", [])
               if not (source.get("type") == "app" and source.get("name") == "glassops")]
    if agent_id == settings.local_agent_id:
        sources.append({"type": "app", "name": "glassops"})
    return {**result, "sources": sources}


@router.get("/read")
async def read_log(
    source_type: str = Query(...),
    name: str = Query(...),
    tail: int = Query(200, ge=1, le=5000),
    search: str = Query(""),
    agent_id: str = Query(settings.local_agent_id, pattern=r"^[a-zA-Z0-9_-]{1,64}$"),
    _: str = Depends(require_admin),
):
    if not SAFE_ID_PATTERN.match(name):
        raise HTTPException(400, "Invalid name")
    if source_type == "app" and name == "glassops":
        if agent_id != settings.local_agent_id:
            raise HTTPException(404, "Runtime archive is available on the dashboard host only")
        try:
            return await asyncio.to_thread(runtime_logs.read_runtime_logs, runtime_logs.ROOT, tail=tail, search=search)
        except FileNotFoundError:
            raise HTTPException(404, "Runtime archive not found")
        except PermissionError:
            raise HTTPException(403, "Runtime archive is not readable")
        except OSError:
            raise HTTPException(503, "Runtime archive could not be read; retry")
    return await call_remote(
        agent_id, "log.read",
        {"source_type": source_type, "name": name, "tail": tail, "search": search},
    )
