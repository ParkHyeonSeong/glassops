"""WebSocket endpoint for terminal sessions — requires JWT auth."""

import asyncio
import json
import logging

from fastapi import WebSocket, WebSocketDisconnect

from app.services.auth_service import verify_token
from app.services.terminal_service import TerminalSession, SESSION_TIMEOUT

logger = logging.getLogger("glassops.terminal")


async def handle_terminal_ws(ws: WebSocket) -> None:
    # Origin check for cookie-based CSRF protection
    origin = ws.headers.get("origin", "")
    host = ws.headers.get("host", "")
    if origin and host:
        from urllib.parse import urlparse
        origin_host = urlparse(origin).netloc
        if origin_host != host:
            await ws.close(code=4003, reason="Origin mismatch")
            return

    # Authenticate via query param token, then cookie fallback
    token = ws.query_params.get("token", "")
    if not token:
        token = ws.cookies.get("access_token", "")
    email = verify_token(token)
    if not email:
        await ws.close(code=4003, reason="Authentication required")
        return

    await ws.accept()
    logger.info("Terminal WebSocket connected: %s", email)

    session = TerminalSession()
    try:
        session.spawn()
    except Exception:
        logger.exception("Failed to spawn terminal")
        await ws.close(code=4000, reason="Failed to spawn terminal")
        return

    async def read_loop():
        while session.is_alive:
            data = await session.read()
            if data:
                try:
                    await ws.send_bytes(data)
                except Exception:
                    break
            if session.idle_seconds > SESSION_TIMEOUT:
                try:
                    await ws.send_text(json.dumps({
                        "type": "timeout",
                        "message": "Session timed out due to inactivity",
                    }))
                except Exception:
                    pass
                break

    read_task = asyncio.create_task(read_loop())

    try:
        while True:
            msg = await ws.receive()

            if msg.get("type") == "websocket.disconnect":
                break

            if "text" in msg:
                try:
                    ctrl = json.loads(msg["text"])
                    if ctrl.get("type") == "resize":
                        session.resize(
                            int(ctrl.get("rows", 24)),
                            int(ctrl.get("cols", 80)),
                        )
                except (json.JSONDecodeError, ValueError):
                    pass

            elif "bytes" in msg:
                session.write(msg["bytes"])

    except WebSocketDisconnect:
        logger.info("Terminal disconnected: %s", email)
    finally:
        read_task.cancel()
        session.kill()
