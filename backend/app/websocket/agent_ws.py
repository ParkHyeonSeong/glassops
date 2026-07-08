"""WebSocket endpoint for agent metric ingestion and RPC."""

import hmac
import json
import logging
import math
import re
import time

from fastapi import WebSocket, WebSocketDisconnect

from app.database import store_metric
from app.net import resolve_client_ip
from app.middleware import rate_limit as rl
from app.websocket.client_ws import broadcast_to_clients
from app.services.alert_service import check_and_alert
from app.services.net_audit_ingest import extract_and_store_net_audit
from app.services import agent_rpc

logger = logging.getLogger("glassops.agent_ws")

AGENT_ID_PATTERN = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")


def _reject_nonfinite(_constant: str):
    """parse_constant hook: reject the JSON literals NaN/Infinity/-Infinity at the
    trust boundary so an agent can't smuggle a non-finite value into stored/broadcast
    metrics (NaN in particular slips past every numeric comparison)."""
    raise ValueError("non-finite JSON literal")
MAX_MESSAGE_SIZE = 1_048_576  # 1MB — RPC responses (e.g., container logs) can be sizable

# Per-connection metric-flood guard. RPC chunks (demand-driven log streams) are NOT
# counted; only the metric path is, whose steady cadence is ~1/s (COLLECT_INTERVAL).
AGENT_METRIC_MAX = 100
AGENT_METRIC_WINDOW = 10

# Connected agents: agent_id -> WebSocket
connected_agents: dict[str, WebSocket] = {}


async def handle_agent_ws(ws: WebSocket) -> None:
    # Abuse guards (AGENT-06): per-IP bad-key lockout + connection-rate cap. IP is
    # resolved via the trusted-proxy model so it can't be spoofed. Reject before
    # accept so abusive peers never enter the metric loop.
    ip = resolve_client_ip(ws.scope)
    if rl.is_agent_key_locked(ip):
        await ws.close(code=4029, reason="Too many failed attempts")
        return
    if not rl.agent_conn_allowed(ip):
        await ws.close(code=4029, reason="Too many connections")
        return

    agent_id = ws.headers.get("x-agent-id", "")
    agent_key = ws.headers.get("x-agent-key", "")

    # Validate agent_id format
    if not agent_id or not AGENT_ID_PATTERN.match(agent_id):
        rl.record_agent_key_failure(ip)
        await ws.close(code=4001, reason="Invalid agent ID")
        return

    # Validate agent key against the derived agent auth key (separate from the
    # JWT signing secret so handing it to a remote agent never leaks the secret).
    _state = getattr(ws.app.state, "settings", None)
    expected_key = _state.agent_key if _state else ""
    if not expected_key or not agent_key or not hmac.compare_digest(agent_key, expected_key):
        rl.record_agent_key_failure(ip)
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

    metric_times: list[float] = []

    try:
        while True:
            raw = await ws.receive_text()

            # Size limit
            if len(raw) > MAX_MESSAGE_SIZE:
                logger.warning("Message too large from agent %s (%d bytes)", agent_id, len(raw))
                continue

            try:
                data = json.loads(raw, parse_constant=_reject_nonfinite)
            except (json.JSONDecodeError, ValueError):
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

            if msg_type == "rpc.chunk":
                rpc_id = data.get("id")
                chunk = data.get("data", "")
                if isinstance(rpc_id, str) and isinstance(chunk, str):
                    await agent_rpc.on_chunk(rpc_id, chunk)
                continue

            if msg_type == "rpc.end":
                rpc_id = data.get("id")
                if isinstance(rpc_id, str):
                    agent_rpc.on_end(rpc_id, None)
                continue

            if msg_type == "rpc.err":
                rpc_id = data.get("id")
                err = data.get("error") or "Stream error"
                if isinstance(rpc_id, str):
                    agent_rpc.on_end(rpc_id, str(err))
                continue

            # Default path: metric ingestion — flood guard (RPC chunks not counted).
            now = time.time()
            metric_times = [t for t in metric_times if t > now - AGENT_METRIC_WINDOW]
            if len(metric_times) >= AGENT_METRIC_MAX:
                logger.warning("Agent %s exceeded metric rate; disconnecting", agent_id)
                break
            metric_times.append(now)

            timestamp = data.get("timestamp", 0)
            # math.isfinite also rejects inf from a huge numeric literal (e.g. 1e400),
            # which parse_constant above does NOT catch (it only sees the bare NaN/
            # Infinity tokens). bool is an int subclass, so exclude it explicitly.
            if (not isinstance(timestamp, (int, float)) or isinstance(timestamp, bool)
                    or not math.isfinite(timestamp) or timestamp <= 0):
                continue
            # Clamp an implausible (clock-skewed / forged) timestamp to server time so
            # one agent can't poison metric ordering or time-range charts.
            if timestamp > now + 300 or timestamp < now - 86400:
                timestamp = now
            # Write the canonical timestamp back so the broadcast + alert consumers
            # below see the same server-verified value the DB row gets — not the raw
            # (possibly forged) field still sitting in `data`.
            data["timestamp"] = timestamp

            try:
                await extract_and_store_net_audit(agent_id, timestamp, data)
            except Exception:
                logger.exception("net_audit ingest failed for %s", agent_id)

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
        # Only tear down this agent's RPC state if we're still the current connection.
        # On a reconnect-replace the new handler already owns connected_agents[agent_id]
        # and its streams; cancelling by agent_id here would kill the new session too.
        if connected_agents.get(agent_id) is ws:
            connected_agents.pop(agent_id, None)
            agent_rpc.cancel_for_agent(agent_id)
