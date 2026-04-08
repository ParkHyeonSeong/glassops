"""WebSocket client that pushes metrics to the GlassOps backend."""

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

logger = logging.getLogger("glassops.agent")

RECONNECT_DELAY = 1


class MetricsPusher:
    def __init__(self) -> None:
        self._ws: Any = None
        self._connected = False

    @property
    def connected(self) -> bool:
        return self._connected

    async def connect(self) -> None:
        """Establish WebSocket connection with retry."""
        # Close existing connection if any
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
