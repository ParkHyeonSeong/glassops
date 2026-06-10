"""Process management API."""

import logging
import os
import signal

from fastapi import APIRouter, Depends, HTTPException, Query

from app.config import settings
from app.database import audit
from app.dependencies import require_admin
from app.services.agent_dispatch import call_remote, is_local

router = APIRouter(prefix="/api/process", tags=["process"])

logger = logging.getLogger("glassops.process")

PROTECTED_PIDS = {0, 1, os.getpid(), os.getppid()}


@router.post("/{pid}/kill")
async def kill_process(pid: int, agent_id: str = Query(settings.local_agent_id),
                       actor: str = Depends(require_admin)):
    if is_local(agent_id):
        # Refresh self-protection
        PROTECTED_PIDS.update({os.getpid(), os.getppid()})
        if pid in PROTECTED_PIDS:
            raise HTTPException(400, "Cannot kill protected process")
        if pid < 0:
            raise HTTPException(400, "Invalid PID")

        try:
            os.kill(pid, signal.SIGTERM)
            logger.info("Sent SIGTERM to PID %d (by %s)", pid, actor)
            await audit(actor, "process.kill", agent_id, {"pid": pid, "signal": "SIGTERM", "ok": True})
            return {"ok": True, "pid": pid, "signal": "SIGTERM"}
        except ProcessLookupError:
            await audit(actor, "process.kill", agent_id, {"pid": pid, "ok": False, "error": "not_found"})
            raise HTTPException(404, f"Process {pid} not found")
        except PermissionError:
            await audit(actor, "process.kill", agent_id, {"pid": pid, "ok": False, "error": "permission"})
            raise HTTPException(403, f"Permission denied to kill PID {pid}")
        except Exception:
            logger.exception("Failed to kill PID %d", pid)
            await audit(actor, "process.kill", agent_id, {"pid": pid, "ok": False, "error": "error"})
            raise HTTPException(500, "Failed to kill process")

    result = await call_remote(agent_id, "process.kill", {"pid": pid})
    await audit(actor, "process.kill", agent_id, {"pid": pid, "ok": result.get("ok", True)})
    return result
