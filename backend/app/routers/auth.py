"""Authentication REST API — login, refresh, 2FA setup, password management."""

from fastapi import APIRouter, HTTPException, Depends, Request, Response
from pydantic import BaseModel

from app.database import audit
from app.dependencies import get_current_user
from app.net import resolve_client_ip, request_is_secure
from app.services.auth_service import (
    verify_password,
    must_change_password,
    is_totp_enabled,
    verify_totp,
    setup_totp,
    confirm_totp,
    create_access_token,
    create_refresh_token,
    verify_refresh_token,
    revoke_refresh_token,
    revoke_access_token,
    change_password,
    force_change_password,
    validate_password,
)

router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginRequest(BaseModel):
    email: str
    password: str
    totp_code: str | None = None


class RefreshRequest(BaseModel):
    refresh_token: str


class PasswordChangeRequest(BaseModel):
    old_password: str
    new_password: str


class ForcePasswordRequest(BaseModel):
    new_password: str


class TotpConfirmRequest(BaseModel):
    code: str


def _is_secure(request: Request) -> bool:
    # Trust X-Forwarded-Proto only from a configured trusted proxy (or force flag).
    return request_is_secure(request.scope)


def _set_auth_cookies(response: Response, access_token: str, refresh_token: str, secure: bool):
    response.set_cookie(
        "access_token", access_token,
        httponly=True, secure=secure, samesite="strict",
        max_age=900, path="/",
    )
    response.set_cookie(
        "refresh_token", refresh_token,
        httponly=True, secure=secure, samesite="strict",
        max_age=604800, path="/api/auth/refresh",
    )


@router.post("/login")
async def login(body: LoginRequest, request: Request, response: Response):
    from app.middleware.rate_limit import record_login_failure, clear_login_failures

    client_ip = resolve_client_ip(request.scope)

    if not await verify_password(body.email, body.password):
        record_login_failure(client_ip)
        await audit(body.email, "auth.login_failed", detail={"ip": client_ip, "reason": "password"})
        raise HTTPException(401, "Invalid credentials")

    # Block disabled accounts after credentials check (avoids leaking which accounts exist).
    from app.database import get_user as _get_user
    user_row = await _get_user(body.email)
    if user_row and not user_row.get("is_active", True):
        raise HTTPException(403, "Account disabled")

    if await is_totp_enabled(body.email):
        if not body.totp_code:
            return {"requires_totp": True}
        if not await verify_totp(body.email, body.totp_code):
            record_login_failure(client_ip)
            await audit(body.email, "auth.login_failed", detail={"ip": client_ip, "reason": "totp"})
            raise HTTPException(401, "Invalid TOTP code")

    clear_login_failures(client_ip)
    await audit(body.email, "auth.login", detail={"ip": client_ip})

    access = create_access_token(body.email)
    refresh = create_refresh_token(body.email)
    secure = _is_secure(request)

    _set_auth_cookies(response, access, refresh, secure)

    from app.database import get_user as _get_user, get_user_host_accounts
    row = await _get_user(body.email)
    return {
        "access_token": access,
        "refresh_token": refresh,
        "email": body.email,
        "must_change_password": await must_change_password(body.email),
        "cookie_mode": secure,
        "role": row.get("role", "user") if row else "user",
        "host_accounts": await get_user_host_accounts(body.email),
    }


class LogoutRequest(BaseModel):
    refresh_token: str = ""


@router.post("/logout")
async def logout(request: Request, response: Response, body: LogoutRequest | None = None):
    # Revoke refresh token if provided
    if body and body.refresh_token:
        await revoke_refresh_token(body.refresh_token)

    # Single-device logout: blacklist the current access token so it can't be
    # reused for the rest of its lifetime. (This route is public — read the token
    # directly from the cookie or Authorization header.)
    access = request.cookies.get("access_token", "")
    if not access:
        auth = request.headers.get("authorization", "")
        if auth.startswith("Bearer "):
            access = auth[7:]
    if access:
        await revoke_access_token(access)

    response.delete_cookie("access_token", path="/")
    response.delete_cookie("refresh_token", path="/api/auth/refresh")
    return {"ok": True}


@router.post("/force-password")
async def force_password(
    body: ForcePasswordRequest,
    email: str = Depends(get_current_user),
):
    result = await force_change_password(email, body.new_password)
    if not result.get("ok"):
        raise HTTPException(400, result)
    return {"ok": True}


@router.get("/password-policy")
async def password_policy():
    return {
        "min_length": 8,
        "max_length": 256,
        "requires": ["uppercase", "lowercase", "digit", "special"],
    }


@router.post("/validate-password")
async def check_password(body: ForcePasswordRequest):
    return validate_password(body.new_password)


@router.post("/refresh")
async def refresh(request: Request, response: Response, body: RefreshRequest):
    # Fall back to cookie when body token is empty (cookie mode)
    token = body.refresh_token or request.cookies.get("refresh_token") or ""
    email = await verify_refresh_token(token)
    if not email:
        raise HTTPException(401, "Invalid refresh token")

    # Revoke immediately after verify — race window is minimal
    # Second concurrent request will fail at verify (blacklisted)
    await revoke_refresh_token(token)

    new_access = create_access_token(email)
    new_refresh = create_refresh_token(email)

    # Re-set cookies so httpOnly cookie mode stays usable past access token expiry
    _set_auth_cookies(response, new_access, new_refresh, _is_secure(request))

    return {
        "access_token": new_access,
        "refresh_token": new_refresh,
    }


@router.get("/me")
async def me(email: str = Depends(get_current_user)):
    from app.database import get_user as _get_user, get_user_host_accounts
    row = await _get_user(email)
    return {
        "email": email,
        "totp_enabled": await is_totp_enabled(email),
        "must_change_password": await must_change_password(email),
        "role": row.get("role", "user") if row else "user",
        "is_active": row.get("is_active", True) if row else True,
        "host_accounts": await get_user_host_accounts(email),
    }


@router.post("/password")
async def update_password(
    body: PasswordChangeRequest,
    email: str = Depends(get_current_user),
):
    result = await change_password(email, body.old_password, body.new_password)
    if not result.get("ok"):
        raise HTTPException(400, result.get("error", "Failed"))
    return {"ok": True}


@router.post("/totp/setup")
async def totp_setup(email: str = Depends(get_current_user)):
    result = await setup_totp(email)
    if not result.get("ok"):
        raise HTTPException(400, result.get("error", "Setup failed"))
    return result


@router.post("/totp/confirm")
async def totp_confirm(
    body: TotpConfirmRequest,
    email: str = Depends(get_current_user),
):
    if not await confirm_totp(email, body.code):
        raise HTTPException(400, "Invalid TOTP code")
    return {"ok": True, "totp_enabled": True}
