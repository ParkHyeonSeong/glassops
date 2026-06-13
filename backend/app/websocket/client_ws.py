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
    logger.info("Client connected (user=%s, total: %d)", email, len(_clients))

    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        _clients.discard(ws)
        logger.info("Client disconnected (total: %d)", len(_clients))


async def broadcast_to_clients(agent_id: str, data: dict) -> None:
    """Send metrics to all connected frontend clients concurrently."""
    if not _clients:
        return

    payload = json.dumps({"agent_id": agent_id, "metrics": data})

    async def safe_send(client: WebSocket) -> bool:
        try:
            await client.send_text(payload)
            return True
        except Exception:
            return False

    results = await asyncio.gather(
        *[safe_send(c) for c in list(_clients)],
        return_exceptions=True,
    )

    # Remove failed clients
    clients_list = list(_clients)
    for i, ok in enumerate(results):
        if ok is not True:
            _clients.discard(clients_list[i])
