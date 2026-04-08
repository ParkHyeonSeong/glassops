"""Authentication REST API — login, refresh, 2FA setup."""

from fastapi import APIRouter, HTTPException, Depends
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel

from app.services.auth_service import (
    verify_password,
    is_totp_enabled,
    verify_totp,
    setup_totp,
    confirm_totp,
    create_access_token,
    create_refresh_token,
    verify_token,
    change_password,
)

router = APIRouter(prefix="/api/auth", tags=["auth"])
bearer = HTTPBearer(auto_error=False)


def get_current_user(creds: HTTPAuthorizationCredentials | None = Depends(bearer)) -> str:
    if not creds:
        raise HTTPException(401, "Not authenticated")
    email = verify_token(creds.credentials)
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


class TotpConfirmRequest(BaseModel):
    code: str


@router.post("/login")
async def login(body: LoginRequest):
    if not verify_password(body.email, body.password):
        raise HTTPException(401, "Invalid credentials")

    if is_totp_enabled(body.email):
        if not body.totp_code:
            return {"requires_totp": True}
        if not verify_totp(body.email, body.totp_code):
            raise HTTPException(401, "Invalid TOTP code")

    return {
        "access_token": create_access_token(body.email),
        "refresh_token": create_refresh_token(body.email),
        "email": body.email,
    }


@router.post("/refresh")
async def refresh(body: RefreshRequest):
    email = verify_token(body.refresh_token, token_type="refresh")
    if not email:
        raise HTTPException(401, "Invalid refresh token")
    return {
        "access_token": create_access_token(email),
    }


@router.get("/me")
async def me(email: str = Depends(get_current_user)):
    return {
        "email": email,
        "totp_enabled": is_totp_enabled(email),
    }


@router.post("/password")
async def update_password(
    body: PasswordChangeRequest,
    email: str = Depends(get_current_user),
):
    if not change_password(email, body.old_password, body.new_password):
        raise HTTPException(400, "Invalid old password")
    return {"ok": True}


@router.post("/totp/setup")
async def totp_setup(email: str = Depends(get_current_user)):
    result = setup_totp(email)
    if not result.get("ok"):
        raise HTTPException(400, result.get("error", "Setup failed"))
    return result


@router.post("/totp/confirm")
async def totp_confirm(
    body: TotpConfirmRequest,
    email: str = Depends(get_current_user),
):
    if not confirm_totp(email, body.code):
        raise HTTPException(400, "Invalid TOTP code")
    return {"ok": True, "totp_enabled": True}
