"""WebSocket endpoint for agent metric ingestion."""

import json
import logging
import re

from fastapi import WebSocket, WebSocketDisconnect

from app.database import store_metric
from app.websocket.client_ws import broadcast_to_clients

logger = logging.getLogger("glassops.agent_ws")

AGENT_ID_PATTERN = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")
MAX_MESSAGE_SIZE = 65_536  # 64KB

# Connected agents: agent_id -> WebSocket
connected_agents: dict[str, WebSocket] = {}


async def handle_agent_ws(ws: WebSocket) -> None:
    agent_id = ws.headers.get("x-agent-id", "")
    agent_key = ws.headers.get("x-agent-key", "")

    # Validate agent_id format
    if not agent_id or not AGENT_ID_PATTERN.match(agent_id):
        await ws.close(code=4001, reason="Invalid agent ID")
        return

    # Basic agent key validation (Phase 6 will add proper per-agent keys)
    expected_key = ws.app.state.settings.secret_key if hasattr(ws.app.state, "settings") else ""
    if not agent_key:
        await ws.close(code=4003, reason="Missing agent key")
        return

    await ws.accept()

    # Disconnect existing connection with same agent_id
    if agent_id in connected_agents:
        try:
            await connected_agents[agent_id].close(code=4002, reason="Replaced by new connection")
        except Exception:
            pass

    connected_agents[agent_id] = ws
    logger.info("Agent connected: %s", agent_id)

    try:
        while True:
            raw = await ws.receive_text()

            # Size limit
            if len(raw) > MAX_MESSAGE_SIZE:
                logger.warning("Message too large from agent %s (%d bytes)", agent_id, len(raw))
                continue

            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                logger.warning("Invalid JSON from agent %s", agent_id)
                continue

            if not isinstance(data, dict):
                continue

            timestamp = data.get("timestamp", 0)
            if not isinstance(timestamp, (int, float)) or timestamp <= 0:
                continue

            await store_metric(agent_id, timestamp, data)
            await broadcast_to_clients(agent_id, data)
    except WebSocketDisconnect:
        logger.info("Agent disconnected: %s", agent_id)
    finally:
        if connected_agents.get(agent_id) is ws:
            connected_agents.pop(agent_id, None)
