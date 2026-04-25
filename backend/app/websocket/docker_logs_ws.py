"""WebSocket endpoint streaming Docker container logs to authenticated clients.

Local agent: opens `container.logs(stream=True, follow=True)` in a worker thread and
forwards chunks. Remote agent: forwards via the agent_rpc streaming RPC.

Each WS message is a small JSON object: `{"line": "..."}`, `{"event": "end"}`, or
`{"event": "error", "error": "..."}`. The client closes the WS to cancel.
"""

import asyncio
import json
import logging
import re
from urllib.parse import urlparse

from fastapi import WebSocket, WebSocketDisconnect

from app.config import settings
from app.services import agent_rpc
from app.services.auth_service import verify_token

logger = logging.getLogger("glassops.docker_logs_ws")

CONTAINER_ID_PATTERN = re.compile(r"^[a-zA-Z0-9_.-]{1,128}$")
AGENT_ID_PATTERN = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")


async def handle_docker_logs_ws(ws: WebSocket) -> None:
    # Origin check (mirrors terminal_ws)
    origin = ws.headers.get("origin", "")
    host = ws.headers.get("host", "")
    if origin and host:
        if urlparse(origin).netloc != host:
            await ws.close(code=4003, reason="Origin mismatch")
            return

    token = ws.query_params.get("token", "") or ws.cookies.get("access_token", "")
    if not verify_token(token):
        await ws.close(code=4003, reason="Authentication required")
        return

    container_id = ws.query_params.get("container_id", "")
    agent_id = ws.query_params.get("agent_id", settings.local_agent_id)
    tail_raw = ws.query_params.get("tail", "200")

    if not CONTAINER_ID_PATTERN.match(container_id):
        await ws.close(code=4400, reason="Invalid container_id")
        return
    if not AGENT_ID_PATTERN.match(agent_id):
        await ws.close(code=4400, reason="Invalid agent_id")
        return

    try:
        tail = max(1, min(int(tail_raw), 5000))
    except ValueError:
        tail = 200

    await ws.accept()

    is_local = agent_id == settings.local_agent_id
    try:
        if is_local:
            await _stream_local(ws, container_id, tail)
        else:
            await _stream_remote(ws, agent_id, container_id, tail)
    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.exception("Log stream failed")
        try:
            await ws.send_text(json.dumps({"event": "error", "error": str(e)}))
        except Exception:
            pass


# ── local path ────────────────────────────────────────────────────────


async def _stream_local(ws: WebSocket, container_id: str, tail: int) -> None:
    """Run a blocking log iterator in a thread, forward chunks via the queue."""
    try:
        from app.services.docker_service import _get_client
    except ImportError:
        await ws.send_text(json.dumps({"event": "error", "error": "docker_service unavailable"}))
        return

    client = _get_client()
    if client is None:
        await ws.send_text(json.dumps({"event": "error", "error": "Docker not available"}))
        return

    try:
        container = client.containers.get(container_id)
    except Exception as e:
        await ws.send_text(json.dumps({"event": "error", "error": f"Container not found: {e}"}))
        return

    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue(maxsize=256)
    SENTINEL = object()

    log_iter = container.logs(stream=True, follow=True, tail=tail, timestamps=True)

    def producer() -> None:
        try:
            for raw in log_iter:
                if isinstance(raw, bytes):
                    text = raw.decode("utf-8", errors="replace")
                else:
                    text = str(raw)
                fut = asyncio.run_coroutine_threadsafe(queue.put(text), loop)
                fut.result()
            asyncio.run_coroutine_threadsafe(queue.put(SENTINEL), loop).result()
        except BaseException as e:
            asyncio.run_coroutine_threadsafe(queue.put(("__err__", str(e))), loop).result()

    producer_task = loop.run_in_executor(None, producer)

    async def reader() -> None:
        # Detect client-side close.
        while True:
            await ws.receive_text()  # we ignore body; just detect disconnect

    reader_task = asyncio.create_task(reader())

    try:
        while True:
            done, _ = await asyncio.wait(
                {asyncio.create_task(queue.get()), reader_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            for fut in done:
                if fut is reader_task:
                    return  # client disconnected
                item = fut.result()
                if item is SENTINEL:
                    await ws.send_text(json.dumps({"event": "end"}))
                    return
                if isinstance(item, tuple) and item and item[0] == "__err__":
                    await ws.send_text(json.dumps({"event": "error", "error": item[1]}))
                    return
                await ws.send_text(json.dumps({"line": item}))
    finally:
        reader_task.cancel()
        try:
            log_iter.close()  # type: ignore[attr-defined]
        except Exception:
            pass
        if not producer_task.done():
            producer_task.cancel()


# ── remote path ───────────────────────────────────────────────────────


async def _stream_remote(ws: WebSocket, agent_id: str, container_id: str, tail: int) -> None:
    async def on_chunk(data: str) -> None:
        await ws.send_text(json.dumps({"line": data}))

    try:
        rpc_id = await agent_rpc.start_stream(
            agent_id,
            "docker.logs.follow",
            {"container_id": container_id, "tail": tail},
            on_chunk,
        )
    except agent_rpc.AgentNotConnected as e:
        await ws.send_text(json.dumps({"event": "error", "error": str(e)}))
        return

    async def reader() -> None:
        while True:
            await ws.receive_text()

    reader_task = asyncio.create_task(reader())
    end_task = asyncio.create_task(agent_rpc.await_stream_end(rpc_id))

    try:
        done, _ = await asyncio.wait(
            {reader_task, end_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if reader_task in done:
            await agent_rpc.cancel_stream(rpc_id)
        else:
            try:
                end_task.result()
                await ws.send_text(json.dumps({"event": "end"}))
            except agent_rpc.RpcError as e:
                await ws.send_text(json.dumps({"event": "error", "error": str(e)}))
    finally:
        if not reader_task.done():
            reader_task.cancel()
        if not end_task.done():
            end_task.cancel()
        await agent_rpc.cancel_stream(rpc_id)
