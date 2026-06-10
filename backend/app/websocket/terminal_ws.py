"""WebSocket endpoint for terminal sessions — JWT-authenticated, multi-host.

For the local agent the backend spawns the PTY directly. For remote agents the
backend opens a bidirectional RPC stream (`terminal.open`) over the existing
agent WebSocket and forwards bytes / control messages between browser and agent.

Each user's per-host shell account comes from the `user_host_accounts` table.
A missing mapping means the user has no terminal access on that host.
"""

import asyncio
import base64
import json
import logging
import time

from fastapi import WebSocket, WebSocketDisconnect

from app.config import settings
from app.database import get_user, get_user_host_accounts, audit
from app.net import resolve_client_ip
from app.services import agent_rpc
from app.services.auth_service import verify_token, access_revoked
from app.websocket.ws_auth import accept_subprotocol, origin_ok, ws_token

logger = logging.getLogger("glassops.terminal")


async def handle_terminal_ws(ws: WebSocket) -> None:
    if not origin_ok(ws):
        await ws.close(code=4003, reason="Origin mismatch")
        return

    token = ws_token(ws)
    email = verify_token(token)
    if not email:
        await ws.close(code=4003, reason="Authentication required")
        return

    # Terminal access is admin-only — a shell here is host-root-equivalent under
    # the privileged container. Verify role, active status, and that the account
    # is not pending a forced password change.
    user = await get_user(email)
    if not user or user.get("role") != "admin" or not user.get("is_active", True):
        await audit(email, "terminal.denied",
                    ws.query_params.get("agent_id", settings.local_agent_id),
                    {"reason": "not_admin", "ip": resolve_client_ip(ws.scope)})
        await ws.close(code=4403, reason="Admin access required")
        return
    if user.get("must_change_password"):
        await ws.close(code=4403, reason="Password change required")
        return
    if await access_revoked(token, user):
        await ws.close(code=4401, reason="Token revoked")
        return

    agent_id = ws.query_params.get("agent_id", settings.local_agent_id)
    accounts = await get_user_host_accounts(email)
    host_user = accounts.get(agent_id, "").strip()

    # For the local agent we allow falling back to the GLASSOPS_TERMINAL_USER env var
    # (preserves single-user installs that haven't configured per-user mappings yet).
    if not host_user and agent_id != settings.local_agent_id:
        await ws.close(code=4003, reason=f"No shell access on host '{agent_id}'")
        return

    await ws.accept(subprotocol=accept_subprotocol(ws))
    logger.info("Terminal WebSocket: user=%s agent=%s host_user=%s", email, agent_id, host_user or "(env default)")
    started = time.monotonic()
    await audit(email, "terminal.open", agent_id,
                {"host_user": host_user or "(env default)", "ip": resolve_client_ip(ws.scope)})

    try:
        # Both local and remote hosts go through the agent RPC stream; the bundled
        # local agent serves agent_id=local. The backend spawns no PTY itself.
        await _bridge_remote(ws, agent_id, host_user)
    except WebSocketDisconnect:
        pass
    except Exception:
        logger.exception("Terminal session failed")
    finally:
        await audit(email, "terminal.close", agent_id,
                    {"host_user": host_user or "(env default)",
                     "duration_s": round(time.monotonic() - started, 1)})


# ── agent RPC bridge (local + remote) ────────────────────────────────


async def _bridge_remote(ws: WebSocket, agent_id: str, host_user: str) -> None:
    async def on_chunk(b64: str) -> None:
        try:
            await ws.send_bytes(base64.b64decode(b64))
        except Exception:
            # send failure means the client is gone — let the outer cleanup handle it
            pass

    try:
        rpc_id = await agent_rpc.start_stream(
            agent_id,
            "terminal.open",
            {"host_user": host_user, "rows": 24, "cols": 80},
            on_chunk,
        )
    except agent_rpc.AgentNotConnected as e:
        await ws.send_text(json.dumps({"type": "timeout", "message": str(e)}))
        await ws.close(code=4503, reason="Agent not connected")
        return

    end_task = asyncio.create_task(agent_rpc.await_stream_end(rpc_id))

    async def reader() -> None:
        try:
            while True:
                msg = await ws.receive()
                if msg.get("type") == "websocket.disconnect":
                    return
                if "text" in msg:
                    try:
                        ctrl = json.loads(msg["text"])
                    except (json.JSONDecodeError, ValueError):
                        continue
                    if ctrl.get("type") == "resize":
                        await agent_rpc.send_control(rpc_id, {
                            "type": "rpc.resize",
                            "rows": int(ctrl.get("rows", 24)),
                            "cols": int(ctrl.get("cols", 80)),
                        })
                elif "bytes" in msg:
                    data: bytes = msg["bytes"]
                    await agent_rpc.send_control(rpc_id, {
                        "type": "rpc.input",
                        "data": base64.b64encode(data).decode("ascii"),
                    })
        except WebSocketDisconnect:
            return
        except (agent_rpc.RpcError, agent_rpc.AgentNotConnected):
            return

    reader_task = asyncio.create_task(reader())

    try:
        done, pending = await asyncio.wait(
            {reader_task, end_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        for t in pending:
            t.cancel()
        if end_task in done:
            try:
                end_task.result()
            except agent_rpc.RpcError as e:
                logger.warning("terminal RPC error for agent %s: %s", agent_id, e)
                try:
                    await ws.send_text(json.dumps({"type": "timeout", "message": "Terminal session error"}))
                except Exception:
                    pass
    finally:
        await agent_rpc.cancel_stream(rpc_id)
