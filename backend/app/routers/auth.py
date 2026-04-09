"""Authentication REST API — login, refresh, 2FA setup, password management."""

from fastapi import APIRouter, HTTPException, Depends, Request, Response
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel

from app.services.auth_service import (
    verify_password,
    must_change_password,
    is_totp_enabled,
    verify_totp,
    setup_totp,
    confirm_totp,
    create_access_token,
    create_refresh_token,
    verify_token,
    verify_refresh_token,
    revoke_refresh_token,
    change_password,
    force_change_password,
    validate_password,
)

router = APIRouter(prefix="/api/auth", tags=["auth"])
bearer = HTTPBearer(auto_error=False)


def get_current_user(request: Request, creds: HTTPAuthorizationCredentials | None = Depends(bearer)) -> str:
    # Try Authorization header first, then cookie fallback
    token = None
    if creds:
        token = creds.credentials
    else:
        token = request.cookies.get("access_token")

    if not token:
        raise HTTPException(401, "Not authenticated")
    email = verify_token(token)
    if not email:
        raise HTTPException(401, "Invalid or expired token")
    return email


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
    return request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"


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

    client_ip = request.headers.get("x-real-ip", "") or (request.client.host if request.client else "unknown")

    if not await verify_password(body.email, body.password):
        record_login_failure(client_ip)
        raise HTTPException(401, "Invalid credentials")

    if await is_totp_enabled(body.email):
        if not body.totp_code:
            return {"requires_totp": True}
        if not await verify_totp(body.email, body.totp_code):
            record_login_failure(client_ip)
            raise HTTPException(401, "Invalid TOTP code")

    clear_login_failures(client_ip)

    access = create_access_token(body.email)
    refresh = create_refresh_token(body.email)
    secure = _is_secure(request)

    _set_auth_cookies(response, access, refresh, secure)

    return {
        "access_token": access,
        "refresh_token": refresh,
        "email": body.email,
        "must_change_password": await must_change_password(body.email),
        "cookie_mode": secure,
    }


class LogoutRequest(BaseModel):
    refresh_token: str = ""


@router.post("/logout")
async def logout(response: Response, body: LogoutRequest | None = None):
    # Revoke refresh token if provided
    if body and body.refresh_token:
        await revoke_refresh_token(body.refresh_token)

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
async def refresh(body: RefreshRequest):
    email = await verify_refresh_token(body.refresh_token)
    if not email:
        raise HTTPException(401, "Invalid refresh token")

    # Revoke immediately after verify — race window is minimal
    # Second concurrent request will fail at verify (blacklisted)
    await revoke_refresh_token(body.refresh_token)

    return {
        "access_token": create_access_token(email),
        "refresh_token": create_refresh_token(email),
    }


@router.get("/me")
async def me(email: str = Depends(get_current_user)):
    return {
        "email": email,
        "totp_enabled": await is_totp_enabled(email),
        "must_change_password": await must_change_password(email),
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
