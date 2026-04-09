"""JWT authentication middleware — ASGI-level to properly handle WebSocket."""

import json
import logging

from starlette.types import ASGIApp, Receive, Scope, Send

logger = logging.getLogger("glassops.auth_mw")

from app.services.auth_service import verify_token

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

        # Skip public paths
        if path in PUBLIC_PATHS:
            await self.app(scope, receive, send)
            return

        for prefix in PUBLIC_PREFIXES:
            if path.startswith(prefix):
                await self.app(scope, receive, send)
                return

        # Extract token from headers
        # ASGI headers are list of (name, value) byte tuples
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

        # Attach to scope state
        if "state" not in scope:
            scope["state"] = {}
        scope["state"]["user_email"] = email

        await self.app(scope, receive, send)

    @staticmethod
    async def _send_401(send: Send, detail: str) -> None:
        body = json.dumps({"detail": detail}).encode()
        await send({
            "type": "http.response.start",
            "status": 401,
            "headers": [
                [b"content-type", b"application/json"],
                [b"content-length", str(len(body)).encode()],
            ],
        })
        await send({"type": "http.response.body", "body": body})
