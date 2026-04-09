"""Process management API."""

import logging
import os
import signal

from fastapi import APIRouter, HTTPException

router = APIRouter(prefix="/api/process", tags=["process"])

logger = logging.getLogger("glassops.process")

PROTECTED_PIDS = {0, 1, os.getpid(), os.getppid()}


@router.post("/{pid}/kill")
async def kill_process(pid: int):
    # Refresh self-protection
    PROTECTED_PIDS.update({os.getpid(), os.getppid()})
    if pid in PROTECTED_PIDS:
        raise HTTPException(400, "Cannot kill protected process")
    if pid < 0:
        raise HTTPException(400, "Invalid PID")

    try:
        os.kill(pid, signal.SIGTERM)
        logger.info("Sent SIGTERM to PID %d", pid)
        return {"ok": True, "pid": pid, "signal": "SIGTERM"}
    except ProcessLookupError:
        raise HTTPException(404, f"Process {pid} not found")
    except PermissionError:
        raise HTTPException(403, f"Permission denied to kill PID {pid}")
    except Exception as e:
        raise HTTPException(500, f"Failed to kill process: {e}")
