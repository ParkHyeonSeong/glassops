"""WebSocket endpoint for agent metric ingestion and RPC."""

import asyncio
import hmac
import json
import logging
import math
import re
import time
import uuid

from fastapi import WebSocket, WebSocketDisconnect

from app.database import store_metric, get_max_metric_id, RESERVED_SAMPLE_KEYS
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

BROADCAST_SAFETY_TIMEOUT = 30  # seconds — last-resort bound on the commit-wins section; the real per-client bound is client_ws.CLIENT_SEND_TIMEOUT (5s)

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

            await ingest_metric(agent_id, data)
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


# Last metrics.id this process assigned — the arrival anchor for ephemeral
# broadcasts. Seeded once at startup by prime_last_assigned_id (below), then
# advanced on each successful store; only 0 when the DB has never held a metric.
_last_assigned_id: int = 0


async def prime_last_assigned_id() -> None:
    """Seed the ephemeral arrival anchor once at startup, before any agent
    connects — so concurrent first ingests can't race the seed. Called from
    the app lifespan after init_db; a read failure propagates and fails
    startup rather than issuing a false anchor."""
    global _last_assigned_id
    _last_assigned_id = await get_max_metric_id()


async def ingest_metric(agent_id: str, data: dict) -> None:
    """Canonicalize, persist, and fan out one metric message.

    The agent is outside the trust boundary: reserved identity keys it may
    have set are stripped before storage, and the server-assigned identity
    (raw:<rowid>, or ephemeral:<uuid> when the store failed) is attached to
    the broadcast copy only — the stored data JSON never carries identity."""
    now = time.time()
    timestamp = data.get("timestamp", 0)
    # math.isfinite also rejects inf from a huge numeric literal (e.g. 1e400),
    # which parse_constant above does NOT catch (it only sees the bare NaN/
    # Infinity tokens). bool is an int subclass, so exclude it explicitly.
    if (not isinstance(timestamp, (int, float)) or isinstance(timestamp, bool)
            or not math.isfinite(timestamp) or timestamp <= 0):
        return
    # Clamp an implausible (clock-skewed / forged) timestamp to server time so
    # one agent can't poison metric ordering or time-range charts.
    if timestamp > now + 300 or timestamp < now - 86400:
        timestamp = now
    # Write the canonical timestamp back so the broadcast + alert consumers
    # below see the same server-verified value the DB row gets — not the raw
    # (possibly forged) field still sitting in `data`.
    data["timestamp"] = timestamp
    for key in RESERVED_SAMPLE_KEYS:
        data.pop(key, None)

    try:
        await extract_and_store_net_audit(agent_id, timestamp, data)
    except Exception:
        logger.exception("net_audit ingest failed for %s", agent_id)

    # Commit-wins (r3.5 #1): once store_metric's COMMIT is submitted to the
    # aiosqlite worker, cancelling the caller does NOT stop the commit — the
    # row persists. So run store + identity + the Live broadcast as one task
    # and drain it to completion even under cancellation, THEN re-raise the
    # cancellation. Otherwise a committed row would be broadcast as ephemeral
    # (or skipped), so History and Live disagree. A plain shield on
    # store_metric is not enough — the whole durable unit must drain.
    core = asyncio.ensure_future(_persist_and_broadcast(agent_id, timestamp, data))
    cancelled: asyncio.CancelledError | None = None
    while not core.done():
        try:
            await asyncio.shield(core)
        except asyncio.CancelledError as exc:
            # Distinguish OUR cancellation from the child's own (r3.6 #1): if
            # core itself was cancelled, re-shielding it would raise forever
            # (tight CPU spin that never lets the process exit), so propagate
            # at once. Otherwise keep draining the still-running core.
            if core.cancelled():
                raise
            cancelled = exc
    core.result()  # surface a child cancellation/exception explicitly
    if cancelled is not None:
        raise cancelled

    # Alerts are outside the durable contract — cancellable, best-effort, and
    # deliberately NOT part of the must-complete section (r3.6 #1).
    try:
        await check_and_alert(agent_id, data)
    except Exception:
        logger.debug("Alert check failed for %s", agent_id, exc_info=True)


async def _persist_and_broadcast(agent_id: str, timestamp: float, data: dict) -> None:
    """Durable unit of ingest, run as a shielded task (see ingest_metric):
    store -> identity attach -> Live broadcast. Every stage swallows its own
    errors, so this returns normally under any DB/broadcast failure and the
    drain loop always completes. It can still end cancelled if the task
    itself is cancelled (loop shutdown) — ingest_metric detects that via
    core.cancelled() and propagates immediately."""
    global _last_assigned_id
    row_id: int | None = None
    try:
        row_id = await store_metric(agent_id, timestamp, data)
    except Exception:
        logger.exception("Failed to store metric for %s", agent_id)

    if row_id is not None:
        # Monotonic: store_metric serializes id assignment under the write
        # lock, and this runs synchronously right after it returns — so a
        # concurrent success can never roll the tracker backwards.
        _last_assigned_id = row_id
        data["sample_id"] = f"raw:{row_id}"
        data["arrival_seq"] = row_id
        data["persisted"] = True
    else:
        data["sample_id"] = f"ephemeral:{uuid.uuid4()}"
        data["persisted"] = False
        # Server-issued arrival anchor: the last id the backend assigned
        # (seeded at startup from sqlite_sequence, so correct across restarts).
        # The frontend cannot reconstruct this — its buffer may be empty
        # mid-fetch. Only 0 when the DB has never held a metric row.
        data["after_seq"] = _last_assigned_id

    # broadcast_to_clients bounds itself per client (client_ws.CLIENT_SEND_TIMEOUT)
    # and evicts stalled sockets there. The generous net here only guarantees
    # this must-complete section terminates — it must stay well above the
    # per-client bound, or it would cancel the eviction mid-flight and a
    # stalled tab would then delay every later metric (r3.7 #1).
    try:
        await asyncio.wait_for(
            broadcast_to_clients(agent_id, data), timeout=BROADCAST_SAFETY_TIMEOUT
        )
    except asyncio.TimeoutError:
        logger.warning("Broadcast exceeded the safety timeout for %s", agent_id)
    except Exception:
        logger.exception("Failed to broadcast for %s", agent_id)
