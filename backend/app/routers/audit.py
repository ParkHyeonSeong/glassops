"""Audit log read API — admin only. The trail is written from the action paths
(terminal, process kill, docker actions, settings, auth, user management)."""

from fastapi import APIRouter, Depends

from app.database import get_audit_log
from app.dependencies import require_admin

router = APIRouter(prefix="/api/audit", tags=["audit"])


@router.get("")
async def list_audit(limit: int = 200, before: float | None = None,
                     user: str | None = None, action: str | None = None,
                     _: str = Depends(require_admin)):
    return {"entries": await get_audit_log(limit=limit, before=before, user=user, action=action)}
