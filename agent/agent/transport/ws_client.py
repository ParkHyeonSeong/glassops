"""WebSocket client that pushes metrics and serves RPC requests from the GlassOps backend.

Supports unary RPC (`rpc.req` → `rpc.res`) and streaming RPC
(`rpc.req` → `rpc.chunk` × N → `rpc.end` / `rpc.err`, cancellable via `rpc.cancel`).
"""

import asyncio
import json
import logging
import ssl
from typing import Any
from urllib.parse import urlparse

import websockets
from websockets.exceptions import (
    ConnectionClosedError,
    ConnectionClosedOK,
    InvalidURI,
    WebSocketException,
)

from agent.config import AGENT_ID, AGENT_KEY, SERVER_URL, TLS_CA, REQUIRE_TLS
from agent.rpc import (
    dispatch as rpc_dispatch,
    dispatch_stream,
    is_stream,
    is_bidi_stream,
    BIDI_STREAM_HANDLERS,
)

logger = logging.getLogger("glassops.agent")

RECONNECT_DELAY = 1

_LOOPBACK = {"localhost", "127.0.0.1", "::1", ""}


def check_transport_security() -> None:
    """Warn (or refuse, when GLASSOPS_REQUIRE_AGENT_TLS=true) if a remote agent
    connects over plaintext ws://. Loopback (the built-in agent) and wss:// are
    exempt. Call once at startup."""
    parsed = urlparse(SERVER_URL)
    if parsed.scheme == "wss" or (parsed.hostname or "") in _LOOPBACK:
        return
    msg = (
        f"GLASSOPS_SERVER_URL uses plaintext ws:// to a remote host ({SERVER_URL}). "
        "The agent key and all RPC traffic (including shell/exec commands) are "
        "exposed to anyone on the network path. Use wss:// (TLS)."
    )
    if REQUIRE_TLS:
        raise SystemExit("FATAL: " + msg + " (set GLASSOPS_REQUIRE_AGENT_TLS=false to override.)")
    logger.warning("SECURITY: %s  Set GLASSOPS_REQUIRE_AGENT_TLS=true to enforce.", msg)


def _ssl_context() -> ssl.SSLContext:
    # Verification is always on (no insecure opt-out). For a self-signed / private
    # CA, GLASSOPS_AGENT_CA adds it to the trust store.
    ctx = ssl.create_default_context()
    if TLS_CA:
        ctx.load_verify_locations(TLS_CA)
    return ctx


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
                kwargs: dict[str, Any] = dict(
                    additional_headers={
                        "X-Agent-Id": AGENT_ID,
                        "X-Agent-Key": AGENT_KEY,
                    },
                    ping_interval=20,
                    ping_timeout=10,
                    max_size=2_097_152,
                )
                if urlparse(SERVER_URL).scheme == "wss":
                    kwargs["ssl"] = _ssl_context()
                self._ws = await websockets.connect(SERVER_URL, **kwargs)
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

# rpc_id -> StreamContext (only for BIDI streams that accept inbound control messages)
_active_bidi_ctx: dict[str, "StreamContext"] = {}


def _teardown_all_streams() -> None:
    """Cancel every in-flight stream/terminal — used when the backend link drops.
    The backend can't deliver a per-id rpc.cancel over a dead socket, so we do the
    equivalent locally for ALL active ids: a cancelled terminal's _run_bidi unwinds
    and runs `finally: session.kill()`, terminating the host shell. Snapshot the
    dicts first (cancel triggers done-callbacks that mutate them). Idempotent."""
    for ctx in list(_active_bidi_ctx.values()):
        try:
            ctx.cancel()   # push the rpc.cancel sentinel so controller() returns
        except Exception:
            pass
    for task in list(_active_streams.values()):
        if not task.done():
            task.cancel()
    _active_bidi_ctx.clear()
    _active_streams.clear()


class StreamContext:
    """Hands a bidirectional stream handler a way to push chunks out and read control messages in."""

    def __init__(self, pusher: "MetricsPusher", rpc_id: str) -> None:
        self._pusher = pusher
        self._rpc_id = rpc_id
        self._control: asyncio.Queue = asyncio.Queue()
        self._cancelled = False

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    async def send_chunk(self, data: str) -> None:
        await self._pusher.send({"type": "rpc.chunk", "id": self._rpc_id, "data": data})

    async def recv_control(self) -> dict | None:
        """Returns next control message, or None if cancelled."""
        if self._cancelled:
            return None
        return await self._control.get()

    def push_control(self, msg: dict) -> None:
        self._control.put_nowait(msg)

    def cancel(self) -> None:
        self._cancelled = True
        # Unblock any pending recv_control with a sentinel.
        try:
            self._control.put_nowait({"type": "rpc.cancel"})
        except Exception:
            pass


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


async def _run_bidi(pusher: MetricsPusher, rpc_id: str, method: str, params: dict) -> None:
    """Drive a bidirectional async handler. Handler is responsible for ending."""
    ctx = StreamContext(pusher, rpc_id)
    _active_bidi_ctx[rpc_id] = ctx
    handler = BIDI_STREAM_HANDLERS[method]
    try:
        await handler(params, ctx)
        await pusher.send({"type": "rpc.end", "id": rpc_id})
    except asyncio.CancelledError:
        try:
            await pusher.send({"type": "rpc.end", "id": rpc_id})
        except Exception:
            pass
        raise
    except Exception as e:
        logger.warning("BIDI stream '%s' failed: %s", method, e)
        try:
            await pusher.send({"type": "rpc.err", "id": rpc_id, "error": f"{type(e).__name__}: {e}"})
        except Exception:
            pass
    finally:
        _active_bidi_ctx.pop(rpc_id, None)


async def serve_rpc(pusher: MetricsPusher, stop: asyncio.Event) -> None:
    """Receive loop — handles rpc.req / rpc.cancel / rpc.input / rpc.resize from the backend."""
    while not stop.is_set():
        if not pusher.connected or pusher.ws is None:
            await asyncio.sleep(0.5)
            continue

        try:
            raw = await pusher.ws.recv()
        except (ConnectionClosedError, ConnectionClosedOK):
            # Link dropped — the backend can't send rpc.cancel over a dead socket,
            # so tear down any open terminals/streams here (idempotent).
            _teardown_all_streams()
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
            ctx = _active_bidi_ctx.get(rpc_id)
            if ctx is not None:
                ctx.cancel()
            task = _active_streams.pop(rpc_id, None)
            if task and not task.done():
                task.cancel()
            continue

        if msg_type in ("rpc.input", "rpc.resize"):
            ctx = _active_bidi_ctx.get(rpc_id)
            if ctx is not None:
                ctx.push_control(msg)
            continue

        if msg_type != "rpc.req":
            continue

        method = msg.get("method", "")
        params = msg.get("params") or {}
        if not isinstance(method, str):
            continue

        if is_bidi_stream(method):
            task = asyncio.create_task(_run_bidi(pusher, rpc_id, method, params))
            _active_streams[rpc_id] = task

            def _cleanup_bidi(t: asyncio.Task, rid: str = rpc_id) -> None:
                _active_streams.pop(rid, None)

            task.add_done_callback(_cleanup_bidi)
        elif is_stream(method):
            task = asyncio.create_task(_run_stream(pusher, rpc_id, method, params))
            _active_streams[rpc_id] = task

            def _cleanup(t: asyncio.Task, rid: str = rpc_id) -> None:
                _active_streams.pop(rid, None)

            task.add_done_callback(_cleanup)
        else:
            asyncio.create_task(_run_unary(pusher, rpc_id, method, params))
