"""Rate limiting middleware — login brute-force protection + API rate limit."""

import json
import time

from starlette.types import ASGIApp, Receive, Scope, Send

from app.net import resolve_client_ip

# Login: 5 failures per IP → 5min lockout
LOGIN_MAX_FAILURES = 5
LOGIN_LOCKOUT_SECONDS = 300

# API: 100 requests per minute per IP
API_RPM = 100
API_WINDOW = 60

# Agent WS (/ws/agent): bad-key lockout (mirrors login) + connection-rate cap per IP.
AGENT_KEY_MAX_FAILURES = 5
AGENT_KEY_LOCKOUT_SECONDS = 300
AGENT_CONN_MAX = 30
AGENT_CONN_WINDOW = 60

# In-memory stores
_login_failures: dict[str, list[float]] = {}  # ip -> [timestamps of failures]
_api_requests: dict[str, list[float]] = {}    # ip -> [timestamps]
_agent_key_failures: dict[str, list[float]] = {}  # ip -> [bad-key attempt timestamps]
_agent_conns: dict[str, list[float]] = {}         # ip -> [/ws/agent handshake timestamps]


def _cleanup_old(entries: list[float], window: float) -> list[float]:
    cutoff = time.time() - window
    return [t for t in entries if t > cutoff]


def record_login_failure(ip: str) -> None:
    now = time.time()
    _login_failures[ip] = _cleanup_old(_login_failures.get(ip, []), LOGIN_LOCKOUT_SECONDS) + [now]


def clear_login_failures(ip: str) -> None:
    _login_failures.pop(ip, None)


def is_login_locked(ip: str) -> bool:
    failures = _cleanup_old(_login_failures.get(ip, []), LOGIN_LOCKOUT_SECONDS)
    _login_failures[ip] = failures
    return len(failures) >= LOGIN_MAX_FAILURES


def get_lockout_remaining(ip: str) -> int:
    failures = _login_failures.get(ip, [])
    if len(failures) < LOGIN_MAX_FAILURES:
        return 0
    oldest_relevant = failures[-LOGIN_MAX_FAILURES]
    remaining = int(LOGIN_LOCKOUT_SECONDS - (time.time() - oldest_relevant))
    return max(0, remaining)


# ── /ws/agent abuse guards (AGENT-06) ────────────────────────────────────


def record_agent_key_failure(ip: str) -> None:
    """Count a rejected /ws/agent handshake (bad agent id or key) for this IP."""
    now = time.time()
    _agent_key_failures[ip] = _cleanup_old(
        _agent_key_failures.get(ip, []), AGENT_KEY_LOCKOUT_SECONDS) + [now]


def is_agent_key_locked(ip: str) -> bool:
    failures = _cleanup_old(_agent_key_failures.get(ip, []), AGENT_KEY_LOCKOUT_SECONDS)
    _agent_key_failures[ip] = failures
    return len(failures) >= AGENT_KEY_MAX_FAILURES


def agent_conn_allowed(ip: str) -> bool:
    """Record a /ws/agent handshake from this IP; False if it exceeds the rate.
    Bounds reconnect floods even when the agent key is correct."""
    conns = _cleanup_old(_agent_conns.get(ip, []), AGENT_CONN_WINDOW)
    if len(conns) >= AGENT_CONN_MAX:
        _agent_conns[ip] = conns
        return False
    _agent_conns[ip] = conns + [time.time()]
    return True


class RateLimitMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")

        # Only rate-limit API paths
        if not path.startswith("/api/"):
            await self.app(scope, receive, send)
            return

        # Real client IP via the trusted-proxy model (can't be spoofed by clients).
        ip = resolve_client_ip(scope)

        # Login lockout check
        if path == "/api/auth/login" and is_login_locked(ip):
            remaining = get_lockout_remaining(ip)
            body = json.dumps({
                "detail": f"Too many login attempts. Try again in {remaining}s",
            }).encode()
            await send({
                "type": "http.response.start",
                "status": 429,
                "headers": [
                    [b"content-type", b"application/json"],
                    [b"retry-after", str(remaining).encode()],
                ],
            })
            await send({"type": "http.response.body", "body": body})
            return

        # General API rate limit
        now = time.time()
        reqs = _cleanup_old(_api_requests.get(ip, []), API_WINDOW)
        if len(reqs) >= API_RPM:
            body = json.dumps({"detail": "Rate limit exceeded"}).encode()
            await send({
                "type": "http.response.start",
                "status": 429,
                "headers": [[b"content-type", b"application/json"]],
            })
            await send({"type": "http.response.body", "body": body})
            return

        _api_requests[ip] = reqs + [now]

        # Periodic cleanup (every ~1000 requests)
        if len(_api_requests) > 1000:
            cutoff = now - API_WINDOW
            for k in list(_api_requests):
                _api_requests[k] = [t for t in _api_requests[k] if t > cutoff]
                if not _api_requests[k]:
                    del _api_requests[k]

        await self.app(scope, receive, send)
