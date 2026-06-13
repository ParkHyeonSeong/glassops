"""JWT authentication middleware — ASGI-level to properly handle WebSocket."""

import json
import logging

from starlette.types import ASGIApp, Receive, Scope, Send

logger = logging.getLogger("glassops.auth_mw")

from app.services.auth_service import verify_token, access_revoked
from app.websocket.ws_auth import csrf_origin_ok

# State-changing methods get a CSRF Origin check (WEB-07). Safe methods (GET/HEAD/
# OPTIONS) are exempt so CORS preflight and reads are untouched.
CSRF_METHODS = {"POST", "PUT", "DELETE", "PATCH"}

PUBLIC_PATHS = {
    "/health",
    "/api/auth/login",
    "/api/auth/refresh",
    "/api/auth/password-policy",
    "/api/time",
}

PUBLIC_PREFIXES = (
    "/ws/",
    "/api/auth/login",
    "/api/auth/refresh",
    "/api/auth/logout",
    "/api/auth/validate-password",
)

# Defense-in-depth safety net: state-changing requests to these prefixes require
# admin. Per-route Depends(require_admin) is the primary gate; this backstop
# catches any privileged write route that forgets to declare it. Sensitive GETs
# (logs/read, settings/runtime) are still gated only by their route dependency.
ADMIN_WRITE_METHODS = {"POST", "PUT", "DELETE", "PATCH"}
ADMIN_WRITE_PREFIXES = (
    "/api/docker",
    "/api/process",
    "/api/settings",
    "/api/alerts",
    "/api/logs",
    "/api/users",
)

# A user pending a forced password change may only reach these (plus the public
# /api/auth/logout); every other /api path is blocked until they change it.
PWCHANGE_ALLOWED = {"/api/auth/force-password", "/api/auth/password", "/api/auth/me"}


class JWTAuthMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        # Only check HTTP requests to /api/ paths
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")

        # Skip non-API paths (static files, health)
        if not path.startswith("/api/"):
            await self.app(scope, receive, send)
            return

        # CSRF guard (WEB-07): reject a state-changing request whose Origin is
        # cross-site. Runs before the public-path skips so cookie-bearing auth
        # routes (refresh/logout) are covered too. Missing Origin is allowed —
        # see csrf_origin_ok. Header scan is cheap and only for /api writes.
        if scope.get("method", "GET") in CSRF_METHODS:
            origin = host = ""
            for name, value in scope.get("headers", []):
                if name == b"origin":
                    origin = value.decode("latin-1")
                elif name == b"host":
                    host = value.decode("latin-1")
            if not csrf_origin_ok(origin, host):
                await self._send_403(send, "Cross-origin request blocked")
                return

        # Skip public paths
        if path in PUBLIC_PATHS:
            await self.app(scope, receive, send)
            return

        for prefix in PUBLIC_PREFIXES:
            if path.startswith(prefix):
                await self.app(scope, receive, send)
                return

        # Extract token from headers
        raw_headers = scope.get("headers", [])
        auth_header = ""
        cookie_header = ""
        for name, value in raw_headers:
            if name == b"authorization":
                auth_header = value.decode()
            elif name == b"cookie":
                cookie_header = value.decode()

        token = ""
        if auth_header.startswith("Bearer "):
            token = auth_header[7:]
        elif cookie_header:
            for part in cookie_header.split(";"):
                part = part.strip()
                if part.startswith("access_token="):
                    token = part[13:]
                    break

        if not token:
            await self._send_401(send, "Not authenticated")
            return

        email = verify_token(token)
        if not email:
            await self._send_401(send, "Invalid or expired token")
            return

        # Reject tokens that belong to disabled users (deferred import to avoid cycle).
        from app.database import get_user
        try:
            user = await get_user(email)
        except Exception:
            user = None
        if user and not user.get("is_active", True):
            await self._send_403(send, "Account disabled")
            return

        # Reject explicitly logged-out or bulk-invalidated (password change / role
        # change / deactivation) access tokens.
        if await access_revoked(token, user):
            await self._send_401(send, "Token revoked")
            return

        # A forced-password-change account is confined to the password-change flow.
        if user and user.get("must_change_password") and path not in PWCHANGE_ALLOWED:
            await self._send_403(send, "Password change required")
            return

        # Defense-in-depth: deny state-changing requests to privileged routers
        # for non-admins (fail closed if the user lookup failed).
        method = scope.get("method", "GET")
        if method in ADMIN_WRITE_METHODS and path.startswith(ADMIN_WRITE_PREFIXES):
            if not user or user.get("role") != "admin":
                await self._send_403(send, "Admin access required")
                return

        # Attach to scope state
        if "state" not in scope:
            scope["state"] = {}
        scope["state"]["user_email"] = email

        await self.app(scope, receive, send)

    @staticmethod
    async def _send_401(send: Send, detail: str) -> None:
        await JWTAuthMiddleware._send_status(send, 401, detail)

    @staticmethod
    async def _send_403(send: Send, detail: str) -> None:
        await JWTAuthMiddleware._send_status(send, 403, detail)

    @staticmethod
    async def _send_status(send: Send, status: int, detail: str) -> None:
        body = json.dumps({"detail": detail}).encode()
        await send({
            "type": "http.response.start",
            "status": status,
            "headers": [
                [b"content-type", b"application/json"],
                [b"content-length", str(len(body)).encode()],
            ],
        })
        await send({"type": "http.response.body", "body": body})
