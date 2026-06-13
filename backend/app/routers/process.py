"""Process management API — kills run through the agent RPC layer (bundled local
agent or remote). The agent enforces protected-PID guards host-side."""

import logging

from fastapi import APIRouter, Depends, Query

from app.config import settings
from app.database import audit
from app.dependencies import require_admin
from app.services.agent_dispatch import call_remote

router = APIRouter(prefix="/api/process", tags=["process"])

logger = logging.getLogger("glassops.process")


@router.post("/{pid}/kill")
async def kill_process(pid: int,
                       agent_id: str = Query(settings.local_agent_id, pattern=r"^[a-zA-Z0-9_-]{1,64}$"),
                       actor: str = Depends(require_admin)):
    result = await call_remote(agent_id, "process.kill", {"pid": pid})
    await audit(actor, "process.kill", agent_id, {"pid": pid, "ok": result.get("ok", True)})
    return result
