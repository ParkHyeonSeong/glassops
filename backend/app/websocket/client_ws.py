"""WebSocket endpoint for frontend clients — relays real-time metrics."""

import asyncio
import json
import logging

from fastapi import WebSocket, WebSocketDisconnect

logger = logging.getLogger("glassops.client_ws")

_clients: set[WebSocket] = set()


async def handle_client_ws(ws: WebSocket) -> None:
    await ws.accept()
    _clients.add(ws)
    logger.info("Client connected (total: %d)", len(_clients))

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
