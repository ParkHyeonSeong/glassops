"""Rate limiting middleware — login brute-force protection + API rate limit."""

import json
import time

from starlette.types import ASGIApp, Receive, Scope, Send

# Login: 5 failures per IP → 5min lockout
LOGIN_MAX_FAILURES = 5
LOGIN_LOCKOUT_SECONDS = 300

# API: 100 requests per minute per IP
API_RPM = 100
API_WINDOW = 60

# In-memory stores
_login_failures: dict[str, list[float]] = {}  # ip -> [timestamps of failures]
_api_requests: dict[str, list[float]] = {}    # ip -> [timestamps]


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

        # Extract client IP — trust X-Real-IP from nginx, fallback to socket IP
        headers = dict(scope.get("headers", []))
        real_ip = headers.get(b"x-real-ip", b"").decode().strip()
        client = scope.get("client")
        ip = real_ip or (client[0] if client else "unknown")

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
