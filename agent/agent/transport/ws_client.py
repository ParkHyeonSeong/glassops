"""WebSocket client that pushes metrics and serves RPC requests from the GlassOps backend."""

import asyncio
import json
import logging
from typing import Any

import websockets
from websockets.exceptions import (
    ConnectionClosedError,
    ConnectionClosedOK,
    InvalidURI,
    WebSocketException,
)

from agent.config import AGENT_ID, AGENT_KEY, SERVER_URL
from agent.rpc import dispatch as rpc_dispatch

logger = logging.getLogger("glassops.agent")

RECONNECT_DELAY = 1


class MetricsPusher:
    def __init__(self) -> None:
        self._ws: Any = None
        self._connected = False
        self._send_lock = asyncio.Lock()

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def ws(self) -> Any:
        return self._ws

    async def connect(self) -> None:
        """Establish WebSocket connection with retry."""
        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None
            self._connected = False

        while True:
            try:
                self._ws = await websockets.connect(
                    SERVER_URL,
                    additional_headers={
                        "X-Agent-Id": AGENT_ID,
                        "X-Agent-Key": AGENT_KEY,
                    },
                    ping_interval=20,
                    ping_timeout=10,
                    max_size=2_097_152,  # 2MB — accommodate large RPC responses
                )
                self._connected = True
                logger.info("Connected to backend: %s", SERVER_URL)
                return
            except (OSError, InvalidURI, WebSocketException) as e:
                self._connected = False
                logger.warning(
                    "Connection failed (%s), retrying in %ds...",
                    e,
                    RECONNECT_DELAY,
                )
                await asyncio.sleep(RECONNECT_DELAY)

    async def send(self, payload: dict) -> bool:
        if not self._ws:
            return False
        try:
            async with self._send_lock:
                await self._ws.send(json.dumps(payload))
            return True
        except (ConnectionClosedError, ConnectionClosedOK, WebSocketException):
            self._connected = False
            logger.warning("Connection lost, will reconnect...")
            return False

    async def close(self) -> None:
        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None
            self._connected = False


async def serve_rpc(pusher: MetricsPusher, stop: asyncio.Event) -> None:
    """Receive loop — handles rpc.req messages from the backend.

    Runs alongside the metric send loop and shares the same connection.
    """
    while not stop.is_set():
        if not pusher.connected or pusher.ws is None:
            await asyncio.sleep(0.5)
            continue

        try:
            raw = await pusher.ws.recv()
        except (ConnectionClosedError, ConnectionClosedOK):
            await asyncio.sleep(0.5)
            continue
        except Exception:
            logger.debug("RPC recv error", exc_info=True)
            await asyncio.sleep(0.5)
            continue

        try:
            msg = json.loads(raw)
        except (ValueError, TypeError):
            continue

        if not isinstance(msg, dict) or msg.get("type") != "rpc.req":
            continue

        rpc_id = msg.get("id")
        method = msg.get("method", "")
        params = msg.get("params") or {}
        if not isinstance(rpc_id, str) or not isinstance(method, str):
            continue

        # Dispatch off the event loop — handlers may do blocking docker SDK calls
        loop = asyncio.get_running_loop()
        try:
            result = await loop.run_in_executor(None, rpc_dispatch, method, params)
            response = {"type": "rpc.res", "id": rpc_id, "ok": True, "result": result}
        except Exception as e:
            logger.warning("RPC handler '%s' failed: %s", method, e)
            response = {"type": "rpc.res", "id": rpc_id, "ok": False, "error": f"{type(e).__name__}: {e}"}

        await pusher.send(response)
