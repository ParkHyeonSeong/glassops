"""JWT authentication middleware for FastAPI."""

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from app.services.auth_service import verify_token

# Paths that don't require authentication
PUBLIC_PATHS = {
    "/health",
    "/api/auth/login",
    "/api/auth/refresh",
    "/api/auth/password-policy",
    "/api/time",
}

# Prefixes that don't require authentication
PUBLIC_PREFIXES = (
    "/ws/",       # WebSocket endpoints have their own auth
    "/api/auth/login",
    "/api/auth/refresh",
    "/api/auth/logout",
    "/api/auth/validate-password",
)


class JWTAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path

        # Skip auth for public paths
        if path in PUBLIC_PATHS:
            return await call_next(request)

        for prefix in PUBLIC_PREFIXES:
            if path.startswith(prefix):
                return await call_next(request)

        # Skip for static files (frontend)
        if not path.startswith("/api/"):
            return await call_next(request)

        # Extract token: Authorization header first, then cookie fallback
        auth_header = request.headers.get("authorization", "")
        token = ""
        if auth_header.startswith("Bearer "):
            token = auth_header[7:]
        else:
            token = request.cookies.get("access_token", "")

        if not token:
            return JSONResponse(
                status_code=401,
                content={"detail": "Not authenticated"},
            )
        email = verify_token(token)
        if not email:
            return JSONResponse(
                status_code=401,
                content={"detail": "Invalid or expired token"},
            )

        # Attach user to request state
        request.state.user_email = email
        return await call_next(request)
