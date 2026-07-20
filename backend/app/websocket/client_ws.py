"""WebSocket endpoint for frontend clients — relays real-time metrics."""

import asyncio
import json
import logging

from fastapi import WebSocket, WebSocketDisconnect

from app.database import get_user
from app.services.auth_service import verify_token, access_revoked
from app.websocket.ws_auth import accept_subprotocol, origin_ok, ws_token

logger = logging.getLogger("glassops.client_ws")

_clients: set[WebSocket] = set()
# Handler task per client, so a timed-out eviction can unblock/stop it too —
# discarding from _clients alone leaves the ASGI task parked in
# `await ws.receive_text()` on a socket nobody is reading from anymore.
_client_tasks: dict[WebSocket, asyncio.Task] = {}


async def handle_client_ws(ws: WebSocket) -> None:
    if not origin_ok(ws):
        await ws.close(code=4003, reason="Origin mismatch")
        return

    # Authenticate via Sec-WebSocket-Protocol token (or access_token cookie). The
    # live metrics stream is read-only dashboard data, so any active user may join.
    token = ws_token(ws)
    email = verify_token(token)
    if not email:
        await ws.close(code=4003, reason="Authentication required")
        return
    user = await get_user(email)
    if not user or not user.get("is_active", True):
        await ws.close(code=4403, reason="Inactive or unknown user")
        return
    if user.get("must_change_password"):
        # A user pending a forced password change is confined to that flow — they
        # must not receive the live host-metrics stream (matches the terminal/docker
        # WS gates and the HTTP middleware's must_change confinement).
        await ws.close(code=4403, reason="Password change required")
        return
    if await access_revoked(token, user):
        await ws.close(code=4401, reason="Token revoked")
        return

    await ws.accept(subprotocol=accept_subprotocol(ws))
    _clients.add(ws)
    _client_tasks[ws] = asyncio.current_task()
    logger.info("Client connected (user=%s, total: %d)", email, len(_clients))

    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        _clients.discard(ws)
        _client_tasks.pop(ws, None)
        logger.info("Client disconnected (total: %d)", len(_clients))


CLIENT_SEND_TIMEOUT = 5  # seconds — a stalled tab must not stall metric ingest
CLIENT_CLOSE_TIMEOUT = 2  # seconds — bound on unblocking an evicted client's handler


async def _evict_client(client: WebSocket) -> None:
    """Remove a client that failed/timed out on send, and unblock its handler
    task so it can't leak forever. client.close() is what normally unblocks
    a handler parked in `await ws.receive_text()` — but that unblocking still
    has to run as a separate step on the event loop, so a successful close()
    returning is not proof the handler has already finished. Give it a
    bounded grace period (shielded, so our own timeout can't cancel the
    handler mid-cleanup) before falling back to cancelling it directly —
    the fallback for when close() itself errors, hangs, or the handler
    still doesn't finish."""
    _clients.discard(client)
    task = _client_tasks.pop(client, None)
    try:
        await asyncio.wait_for(client.close(), timeout=CLIENT_CLOSE_TIMEOUT)
        if task is not None:
            await asyncio.wait_for(asyncio.shield(task), timeout=CLIENT_CLOSE_TIMEOUT)
    except Exception:
        pass
    if task is not None and not task.done():
        task.cancel()


async def broadcast_to_clients(agent_id: str, data: dict) -> None:
    """Send metrics to all connected frontend clients concurrently."""
    clients = list(_clients)  # single snapshot: results map 1:1 to it
    if not clients:
        return

    payload = json.dumps({"agent_id": agent_id, "metrics": data})

    async def safe_send(client: WebSocket) -> bool:
        try:
            # Per-client bound: a stalled socket must not hold the ingest path
            # open, and must still be evicted below — an outer timeout on the
            # whole gather would cancel that eviction and strand it here.
            await asyncio.wait_for(client.send_text(payload), timeout=CLIENT_SEND_TIMEOUT)
            return True
        except Exception:
            return False

    results = await asyncio.gather(*[safe_send(c) for c in clients], return_exceptions=True)

    # Evict every client that timed out or errored, indexed against the SAME
    # snapshot the sends were built from. Concurrent so N stalled clients
    # cost one CLIENT_CLOSE_TIMEOUT, not N, keeping this comfortably under
    # agent_ws.BROADCAST_SAFETY_TIMEOUT.
    failed = [client for client, ok in zip(clients, results) if ok is not True]
    if failed:
        await asyncio.gather(*[_evict_client(c) for c in failed], return_exceptions=True)
