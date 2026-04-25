"""RPC over agent WebSocket — request/response routing for remote-agent operations."""

import asyncio
import json
import logging
import uuid

from app.config import settings

logger = logging.getLogger("glassops.agent_rpc")


class AgentNotConnected(Exception):
    pass


class RpcError(Exception):
    pass


class RpcTimeout(Exception):
    pass


# rpc_id -> (agent_id, Future)
_pending: dict[str, tuple[str, asyncio.Future]] = {}

# Per-agent locks to serialize concurrent sends on the same WebSocket.
_send_locks: dict[str, asyncio.Lock] = {}


def _lock_for(agent_id: str) -> asyncio.Lock:
    lock = _send_locks.get(agent_id)
    if lock is None:
        lock = asyncio.Lock()
        _send_locks[agent_id] = lock
    return lock


async def call(agent_id: str, method: str, params: dict | None = None, timeout: int | None = None) -> dict:
    """Send an RPC request to an agent and await its response.

    Raises AgentNotConnected, RpcTimeout, or RpcError.
    """
    from app.websocket.agent_ws import connected_agents

    ws = connected_agents.get(agent_id)
    if ws is None:
        raise AgentNotConnected(f"Agent '{agent_id}' is not connected")

    rpc_id = uuid.uuid4().hex
    fut: asyncio.Future = asyncio.get_running_loop().create_future()
    _pending[rpc_id] = (agent_id, fut)

    msg = json.dumps({
        "type": "rpc.req",
        "id": rpc_id,
        "method": method,
        "params": params or {},
    })

    try:
        async with _lock_for(agent_id):
            await ws.send_text(msg)
    except Exception as e:
        _pending.pop(rpc_id, None)
        raise AgentNotConnected(f"Failed to send to agent '{agent_id}': {e}") from e

    try:
        payload = await asyncio.wait_for(fut, timeout=timeout or settings.rpc_timeout)
    except asyncio.TimeoutError as e:
        raise RpcTimeout(f"RPC '{method}' to agent '{agent_id}' timed out") from e
    finally:
        _pending.pop(rpc_id, None)

    if not payload.get("ok"):
        raise RpcError(payload.get("error") or "Unknown error")
    return payload.get("result") or {}


def resolve(rpc_id: str, payload: dict) -> None:
    """Called by agent_ws when an rpc.res arrives."""
    entry = _pending.get(rpc_id)
    if entry is None:
        logger.debug("Dropping rpc.res with unknown id=%s", rpc_id)
        return
    _, fut = entry
    if not fut.done():
        fut.set_result(payload)


def cancel_for_agent(agent_id: str) -> None:
    """Cancel all pending requests targeting a disconnected agent."""
    to_cancel = [rid for rid, (aid, _) in _pending.items() if aid == agent_id]
    for rid in to_cancel:
        _, fut = _pending.pop(rid)
        if not fut.done():
            fut.set_exception(AgentNotConnected(f"Agent '{agent_id}' disconnected"))
    _send_locks.pop(agent_id, None)
