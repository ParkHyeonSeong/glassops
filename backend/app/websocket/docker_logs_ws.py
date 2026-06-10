"""WebSocket endpoint streaming Docker container logs to authenticated clients.

Local agent: opens `container.logs(stream=True, follow=True)` in a worker thread and
forwards chunks. Remote agent: forwards via the agent_rpc streaming RPC.

Each WS message is a small JSON object: `{"line": "..."}`, `{"event": "end"}`, or
`{"event": "error", "error": "..."}`. The client closes the WS to cancel.
"""

import asyncio
import json
import logging
import re

from fastapi import WebSocket, WebSocketDisconnect

from app.config import settings
from app.database import get_user
from app.services import agent_rpc
from app.services.auth_service import verify_token, access_revoked
from app.websocket.ws_auth import accept_subprotocol, origin_ok, ws_token

logger = logging.getLogger("glassops.docker_logs_ws")

CONTAINER_ID_PATTERN = re.compile(r"^[a-zA-Z0-9_.-]{1,128}$")
AGENT_ID_PATTERN = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")

# Client-facing error codes — keep raw docker SDK / agent exception text out of the
# browser (it leaks host paths and internal detail); the real cause is logged server-side.
_ERR_MESSAGES = {
    "stream_failed": "Log stream error",
    "agent_unavailable": "Agent not connected",
}


async def _send_error(ws: WebSocket, code: str) -> None:
    try:
        await ws.send_text(json.dumps(
            {"event": "error", "code": code, "error": _ERR_MESSAGES.get(code, "Log stream error")}
        ))
    except Exception:
        pass


async def handle_docker_logs_ws(ws: WebSocket) -> None:
    if not origin_ok(ws):
        await ws.close(code=4003, reason="Origin mismatch")
        return

    token = ws_token(ws)
    email = verify_token(token)
    if not email:
        await ws.close(code=4003, reason="Authentication required")
        return

    # Container logs are sensitive (may contain secrets) — admin only, matching
    # the REST endpoint GET /api/docker/containers/{id}/logs, and confined like the
    # terminal: active admin, not pending a forced password change, not revoked.
    user = await get_user(email)
    if not user or user.get("role") != "admin" or not user.get("is_active", True):
        await ws.close(code=4403, reason="Admin access required")
        return
    if user.get("must_change_password"):
        await ws.close(code=4403, reason="Password change required")
        return
    if await access_revoked(token, user):
        await ws.close(code=4401, reason="Token revoked")
        return

    container_id = ws.query_params.get("container_id", "")
    agent_id = ws.query_params.get("agent_id", settings.local_agent_id)
    tail_raw = ws.query_params.get("tail", "200")

    if not CONTAINER_ID_PATTERN.match(container_id):
        await ws.close(code=4400, reason="Invalid container_id")
        return
    if not AGENT_ID_PATTERN.match(agent_id):
        await ws.close(code=4400, reason="Invalid agent_id")
        return

    try:
        tail = max(1, min(int(tail_raw), 5000))
    except ValueError:
        tail = 200

    await ws.accept(subprotocol=accept_subprotocol(ws))

    # Both local and remote hosts stream via the agent RPC (the bundled local agent
    # serves agent_id=local); the backend opens no docker log iterator itself.
    try:
        await _stream_remote(ws, agent_id, container_id, tail)
    except WebSocketDisconnect:
        pass
    except Exception:
        logger.exception("Log stream failed")
        await _send_error(ws, "stream_failed")


# ── stream path (via agent RPC) ──────────────────────────────────────


async def _stream_remote(ws: WebSocket, agent_id: str, container_id: str, tail: int) -> None:
    async def on_chunk(data: str) -> None:
        await ws.send_text(json.dumps({"line": data}))

    try:
        rpc_id = await agent_rpc.start_stream(
            agent_id,
            "docker.logs.follow",
            {"container_id": container_id, "tail": tail},
            on_chunk,
        )
    except agent_rpc.AgentNotConnected as e:
        logger.warning("docker logs: agent %s not connected: %s", agent_id, e)
        await _send_error(ws, "agent_unavailable")
        return

    async def reader() -> None:
        while True:
            await ws.receive_text()

    reader_task = asyncio.create_task(reader())
    end_task = asyncio.create_task(agent_rpc.await_stream_end(rpc_id))

    try:
        done, _ = await asyncio.wait(
            {reader_task, end_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if reader_task in done:
            await agent_rpc.cancel_stream(rpc_id)
        else:
            try:
                end_task.result()
                await ws.send_text(json.dumps({"event": "end"}))
            except agent_rpc.RpcError as e:
                logger.warning("docker logs RPC error for agent %s: %s", agent_id, e)
                await _send_error(ws, "stream_failed")
    finally:
        if not reader_task.done():
            reader_task.cancel()
        if not end_task.done():
            end_task.cancel()
        await agent_rpc.cancel_stream(rpc_id)
