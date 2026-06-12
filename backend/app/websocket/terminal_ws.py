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

# A web terminal is a host shell (nsenter+su on the agent), so bound both the
# concurrent count (resource exhaustion) and the idle lifetime (abandoned root shell).
MAX_TERMINAL_SESSIONS = 5          # per user
IDLE_TIMEOUT = 1800                # seconds of no I/O before the session is closed
_sessions: dict[str, int] = {}     # email -> active terminal count


def _release_session(email: str) -> None:
    if _sessions.get(email, 0) <= 1:
        _sessions.pop(email, None)
    else:
        _sessions[email] -= 1


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
        await audit(email, "terminal.denied",
                    ws.query_params.get("agent_id", settings.local_agent_id),
                    {"reason": "must_change_password", "ip": resolve_client_ip(ws.scope)})
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

    if _sessions.get(email, 0) >= MAX_TERMINAL_SESSIONS:
        await audit(email, "terminal.denied", agent_id,
                    {"reason": "session_limit", "ip": resolve_client_ip(ws.scope)})
        await ws.close(code=4429, reason="Too many terminal sessions")
        return

    # Reserve the slot synchronously (before any await) so two concurrent opens can't
    # both pass the cap check above between the test and the increment.
    _sessions[email] = _sessions.get(email, 0) + 1
    try:
        await ws.accept(subprotocol=accept_subprotocol(ws))
    except Exception:
        _release_session(email)
        raise

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
        _release_session(email)
        await audit(email, "terminal.close", agent_id,
                    {"host_user": host_user or "(env default)",
                     "duration_s": round(time.monotonic() - started, 1)})


# ── agent RPC bridge (local + remote) ────────────────────────────────


async def _bridge_remote(ws: WebSocket, agent_id: str, host_user: str) -> None:
    last_activity = time.monotonic()

    async def on_chunk(b64: str) -> None:
        nonlocal last_activity
        last_activity = time.monotonic()  # output counts as activity (e.g. tail -f)
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
        nonlocal last_activity
        try:
            while True:
                msg = await ws.receive()
                if msg.get("type") == "websocket.disconnect":
                    return
                last_activity = time.monotonic()  # client input is activity
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

    async def idle_watchdog() -> None:
        # Close an abandoned shell after IDLE_TIMEOUT of no input or output.
        while True:
            await asyncio.sleep(30)
            if time.monotonic() - last_activity > IDLE_TIMEOUT:
                try:
                    await ws.send_text(json.dumps({
                        "type": "timeout",
                        "message": "Session timed out due to inactivity",
                    }))
                except Exception:
                    pass
                return

    reader_task = asyncio.create_task(reader())
    idle_task = asyncio.create_task(idle_watchdog())

    try:
        done, pending = await asyncio.wait(
            {reader_task, end_task, idle_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        for t in pending:
            t.cancel()
        if idle_task in done:
            # Idle watchdog fired — explicitly close the browser side so its onclose
            # fires promptly (don't rely on the server closing on handler return).
            try:
                await ws.close(code=1001, reason="Idle timeout")
            except Exception:
                pass
        if end_task in done:
            try:
                end_task.result()
            except agent_rpc.RpcError as e:
                logger.warning("terminal RPC error for agent %s: %s", agent_id, e)
                try:
                    # Surface the agent's message (e.g. the terminal-config guidance).
                    # The terminal is admin-only, so this isn't a public info leak.
                    await ws.send_text(json.dumps({"type": "timeout", "message": str(e)}))
                except Exception:
                    pass
    finally:
        await agent_rpc.cancel_stream(rpc_id)
