"""RPC over agent WebSocket — request/response routing for remote-agent operations.

Two modes:
  - Unary  : `call()` → single rpc.req, awaits a single rpc.res
  - Stream : `start_stream()` → rpc.req, then receives rpc.chunk(s) and rpc.end/rpc.err.
             Cancellable via `cancel_stream()` which sends rpc.cancel to the agent.
"""

import asyncio
import json
import logging
import uuid
from typing import Awaitable, Callable

from app.config import settings

logger = logging.getLogger("glassops.agent_rpc")


class AgentNotConnected(Exception):
    pass


class RpcError(Exception):
    pass


class RpcTimeout(Exception):
    pass


# rpc_id -> (agent_id, Future) — for unary calls
_pending: dict[str, tuple[str, asyncio.Future]] = {}

# rpc_id -> stream state — for streaming calls
_streams: dict[str, "_StreamState"] = {}

# Per-agent locks to serialize concurrent sends on the same WebSocket.
_send_locks: dict[str, asyncio.Lock] = {}


class _StreamState:
    __slots__ = ("agent_id", "on_chunk", "done", "error")

    def __init__(self, agent_id: str, on_chunk: Callable[[str], Awaitable[None]]) -> None:
        self.agent_id = agent_id
        self.on_chunk = on_chunk
        self.done: asyncio.Event = asyncio.Event()
        self.error: str | None = None


def _lock_for(agent_id: str) -> asyncio.Lock:
    lock = _send_locks.get(agent_id)
    if lock is None:
        lock = asyncio.Lock()
        _send_locks[agent_id] = lock
    return lock


async def _send(agent_id: str, payload: dict) -> None:
    from app.websocket.agent_ws import connected_agents

    ws = connected_agents.get(agent_id)
    if ws is None:
        raise AgentNotConnected(f"Agent '{agent_id}' is not connected")
    msg = json.dumps(payload)
    async with _lock_for(agent_id):
        await ws.send_text(msg)


# ── Unary ─────────────────────────────────────────────────────────────


async def call(agent_id: str, method: str, params: dict | None = None, timeout: int | None = None) -> dict:
    """Send an RPC request to an agent and await its single response."""
    rpc_id = uuid.uuid4().hex
    fut: asyncio.Future = asyncio.get_running_loop().create_future()
    _pending[rpc_id] = (agent_id, fut)

    try:
        await _send(agent_id, {"type": "rpc.req", "id": rpc_id, "method": method, "params": params or {}})
    except AgentNotConnected:
        _pending.pop(rpc_id, None)
        raise
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


# ── Streaming ─────────────────────────────────────────────────────────


async def start_stream(
    agent_id: str,
    method: str,
    params: dict | None,
    on_chunk: Callable[[str], Awaitable[None]],
) -> str:
    """Open a streaming RPC. `on_chunk` is awaited for each rpc.chunk that arrives.

    Returns the stream rpc_id. Caller awaits `await_stream_end(rpc_id)` to detect
    natural termination, or invokes `cancel_stream(rpc_id)` to stop early.
    """
    rpc_id = uuid.uuid4().hex
    state = _StreamState(agent_id, on_chunk)
    _streams[rpc_id] = state

    try:
        await _send(agent_id, {"type": "rpc.req", "id": rpc_id, "method": method, "params": params or {}})
    except Exception:
        _streams.pop(rpc_id, None)
        raise

    return rpc_id


async def await_stream_end(rpc_id: str) -> None:
    """Wait until the stream ends (normally or with error). Raises RpcError on rpc.err."""
    state = _streams.get(rpc_id)
    if state is None:
        return
    await state.done.wait()
    err = state.error
    _streams.pop(rpc_id, None)
    if err:
        raise RpcError(err)


async def cancel_stream(rpc_id: str) -> None:
    """Send rpc.cancel to the agent and clean up local state."""
    state = _streams.pop(rpc_id, None)
    if state is None:
        return
    if not state.done.is_set():
        state.done.set()
    try:
        await _send(state.agent_id, {"type": "rpc.cancel", "id": rpc_id})
    except Exception:
        # Agent may have already disconnected — nothing to do.
        pass


async def on_chunk(rpc_id: str, data: str) -> None:
    """Called by agent_ws when rpc.chunk arrives."""
    state = _streams.get(rpc_id)
    if state is None:
        return
    try:
        await state.on_chunk(data)
    except Exception:
        logger.exception("on_chunk callback raised for rpc_id=%s", rpc_id)


def on_end(rpc_id: str, error: str | None = None) -> None:
    """Called by agent_ws when rpc.end / rpc.err arrives."""
    state = _streams.get(rpc_id)
    if state is None:
        return
    state.error = error
    state.done.set()


def cancel_for_agent(agent_id: str) -> None:
    """Cancel all pending unary calls and streams targeting a disconnected agent."""
    # Unary
    to_cancel = [rid for rid, (aid, _) in _pending.items() if aid == agent_id]
    for rid in to_cancel:
        _, fut = _pending.pop(rid)
        if not fut.done():
            fut.set_exception(AgentNotConnected(f"Agent '{agent_id}' disconnected"))

    # Streams
    stream_ids = [rid for rid, st in _streams.items() if st.agent_id == agent_id]
    for rid in stream_ids:
        st = _streams.pop(rid)
        if not st.done.is_set():
            st.error = f"Agent '{agent_id}' disconnected"
            st.done.set()

    _send_locks.pop(agent_id, None)
