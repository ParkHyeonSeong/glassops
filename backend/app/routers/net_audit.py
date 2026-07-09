"""Admin-only network-audit read API. Never broadcast; every read is audit-logged."""

import time

from fastapi import APIRouter, Depends, Path, Query

from app.database import get_net_conn_events, get_net_flow_rollup, audit
from app.dependencies import require_admin

router = APIRouter(prefix="/api/net-audit", tags=["net-audit"])

# Same agent-id pattern the metrics routes use (main.py `_AGENT_ID`) — reject
# malformed identifiers at the routing layer before the handler/DB (review P2).
_AGENT_ID = Path(pattern=r"^[a-zA-Z0-9_-]{1,64}$")
_DURATIONS = {"1h": 3600, "6h": 21600, "24h": 86400, "7d": 604800, "30d": 2592000}


@router.get("/{agent_id}/events")
async def events(agent_id: str = _AGENT_ID, before: float | None = None,
                 limit: int = Query(200, ge=1, le=1000),
                 proto: str | None = None, raddr: str | None = None,
                 port: int | None = None, pid: int | None = None,
                 actor: str = Depends(require_admin)):
    # limit is bounded by Query (ge=1, le=1000); get_net_conn_events also clamps as
    # defence-in-depth so a negative value can never become an unbounded LIMIT (P2).
    rows = await get_net_conn_events(agent_id, before=before, limit=limit,
                                     proto=proto, raddr=raddr, port=port, pid=pid)
    await audit(actor, "net_audit.read_events", agent_id,
                {"count": len(rows), "raddr": raddr, "port": port, "pid": pid})
    return {"agent_id": agent_id, "events": rows}


@router.get("/{agent_id}/rollup")
async def rollup(agent_id: str = _AGENT_ID, duration: str = "24h",
                 actor: str = Depends(require_admin)):
    now = time.time()
    seconds = _DURATIONS.get(duration, 86400)
    rows = await get_net_flow_rollup(agent_id, now - seconds, now)
    await audit(actor, "net_audit.read_rollup", agent_id, {"duration": duration})
    return {"agent_id": agent_id, "duration": duration, "rollups": rows}
