"""Shared FastAPI auth dependencies — single source for routers.

`get_current_user` resolves the caller's email from the bearer header or the
`access_token` cookie. `require_admin` additionally enforces the admin role and
active status. Centralised here so every router reuses the same gate instead of
each defining its own (which is how the authorization gap arose).
"""

from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

from app.database import get_user
from app.services.auth_service import verify_token

_bearer = HTTPBearer(auto_error=False)


def get_current_user(
    request: Request,
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> str:
    # Authorization header first, then cookie fallback.
    token = creds.credentials if creds else request.cookies.get("access_token")
    if not token:
        raise HTTPException(401, "Not authenticated")
    email = verify_token(token)
    if not email:
        raise HTTPException(401, "Invalid or expired token")
    return email


async def require_admin(email: str = Depends(get_current_user)) -> str:
    user = await get_user(email)
    if not user or user.get("role") != "admin" or not user.get("is_active", True):
        raise HTTPException(403, "Admin access required")
    return email
