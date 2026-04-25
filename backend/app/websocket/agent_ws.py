"""WebSocket endpoint for agent metric ingestion and RPC."""

import hmac
import json
import logging
import re

from fastapi import WebSocket, WebSocketDisconnect

from app.database import store_metric
from app.websocket.client_ws import broadcast_to_clients
from app.services.alert_service import check_and_alert
from app.services import agent_rpc

logger = logging.getLogger("glassops.agent_ws")

AGENT_ID_PATTERN = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")
MAX_MESSAGE_SIZE = 1_048_576  # 1MB — RPC responses (e.g., container logs) can be sizable

# Connected agents: agent_id -> WebSocket
connected_agents: dict[str, WebSocket] = {}


async def handle_agent_ws(ws: WebSocket) -> None:
    agent_id = ws.headers.get("x-agent-id", "")
    agent_key = ws.headers.get("x-agent-key", "")

    # Validate agent_id format
    if not agent_id or not AGENT_ID_PATTERN.match(agent_id):
        await ws.close(code=4001, reason="Invalid agent ID")
        return

    # Validate agent key against configured secret
    expected_key = ws.app.state.settings.secret_key if hasattr(ws.app.state, "settings") else ""
    if not agent_key or not hmac.compare_digest(agent_key, expected_key):
        await ws.close(code=4003, reason="Invalid agent key")
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

            msg_type = data.get("type", "metric")

            if msg_type == "rpc.res":
                rpc_id = data.get("id")
                if isinstance(rpc_id, str):
                    agent_rpc.resolve(rpc_id, data)
                continue

            # Default path: metric ingestion
            timestamp = data.get("timestamp", 0)
            if not isinstance(timestamp, (int, float)) or timestamp <= 0:
                continue

            try:
                await store_metric(agent_id, timestamp, data)
            except Exception:
                logger.exception("Failed to store metric for %s", agent_id)

            try:
                await broadcast_to_clients(agent_id, data)
            except Exception:
                logger.exception("Failed to broadcast for %s", agent_id)

            try:
                await check_and_alert(agent_id, data)
            except Exception:
                logger.debug("Alert check failed for %s", agent_id, exc_info=True)
    except WebSocketDisconnect:
        logger.info("Agent disconnected: %s", agent_id)
    except Exception:
        logger.exception("Agent handler error for %s", agent_id)
    finally:
        if connected_agents.get(agent_id) is ws:
            connected_agents.pop(agent_id, None)
        agent_rpc.cancel_for_agent(agent_id)
