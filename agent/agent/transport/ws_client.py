"""WebSocket client that pushes metrics and serves RPC requests from the GlassOps backend.

Supports unary RPC (`rpc.req` → `rpc.res`) and streaming RPC
(`rpc.req` → `rpc.chunk` × N → `rpc.end` / `rpc.err`, cancellable via `rpc.cancel`).
"""

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
from agent.rpc import dispatch as rpc_dispatch, dispatch_stream, is_stream

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
                    max_size=2_097_152,
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


# rpc_id -> Task running the stream
_active_streams: dict[str, asyncio.Task] = {}


async def _run_unary(pusher: MetricsPusher, rpc_id: str, method: str, params: dict) -> None:
    loop = asyncio.get_running_loop()
    try:
        result = await loop.run_in_executor(None, rpc_dispatch, method, params)
        await pusher.send({"type": "rpc.res", "id": rpc_id, "ok": True, "result": result})
    except Exception as e:
        logger.warning("RPC handler '%s' failed: %s", method, e)
        await pusher.send({
            "type": "rpc.res",
            "id": rpc_id,
            "ok": False,
            "error": f"{type(e).__name__}: {e}",
        })


async def _run_stream(pusher: MetricsPusher, rpc_id: str, method: str, params: dict) -> None:
    """Drive a stream handler: each yielded chunk → rpc.chunk; finally rpc.end (or rpc.err)."""
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue(maxsize=128)
    SENTINEL = object()

    def producer() -> None:
        """Runs in a worker thread: drives the blocking generator."""
        try:
            for chunk in dispatch_stream(method, params):
                fut = asyncio.run_coroutine_threadsafe(queue.put(chunk), loop)
                fut.result()  # back-pressure if queue full
            asyncio.run_coroutine_threadsafe(queue.put(SENTINEL), loop).result()
        except BaseException as e:  # includes generator close on cancel
            asyncio.run_coroutine_threadsafe(queue.put(("__err__", str(e))), loop).result()

    producer_task = loop.run_in_executor(None, producer)

    try:
        while True:
            item = await queue.get()
            if item is SENTINEL:
                await pusher.send({"type": "rpc.end", "id": rpc_id})
                return
            if isinstance(item, tuple) and item and item[0] == "__err__":
                await pusher.send({"type": "rpc.err", "id": rpc_id, "error": item[1]})
                return
            await pusher.send({"type": "rpc.chunk", "id": rpc_id, "data": item})
    except asyncio.CancelledError:
        # Backend asked us to cancel — try to interrupt the generator and report end.
        try:
            await pusher.send({"type": "rpc.end", "id": rpc_id})
        except Exception:
            pass
        raise
    finally:
        # Best-effort: producer thread will exit when generator's docker stream closes.
        # We don't have a clean way to interrupt a blocking iterator from outside,
        # but generators tied to docker streams stop when the upstream socket is closed.
        if not producer_task.done():
            producer_task.cancel()


async def serve_rpc(pusher: MetricsPusher, stop: asyncio.Event) -> None:
    """Receive loop — handles rpc.req / rpc.cancel from the backend."""
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

        if not isinstance(msg, dict):
            continue

        msg_type = msg.get("type")
        rpc_id = msg.get("id")
        if not isinstance(rpc_id, str):
            continue

        if msg_type == "rpc.cancel":
            task = _active_streams.pop(rpc_id, None)
            if task and not task.done():
                task.cancel()
            continue

        if msg_type != "rpc.req":
            continue

        method = msg.get("method", "")
        params = msg.get("params") or {}
        if not isinstance(method, str):
            continue

        if is_stream(method):
            task = asyncio.create_task(_run_stream(pusher, rpc_id, method, params))
            _active_streams[rpc_id] = task

            def _cleanup(t: asyncio.Task, rid: str = rpc_id) -> None:
                _active_streams.pop(rid, None)

            task.add_done_callback(_cleanup)
        else:
            asyncio.create_task(_run_unary(pusher, rpc_id, method, params))
