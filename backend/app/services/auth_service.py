"""Authentication service — JWT + optional TOTP 2FA."""

import logging
import os
import time

import bcrypt
import pyotp
from jose import jwt, JWTError

from app.config import settings

logger = logging.getLogger("glassops.auth")

ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE = 900  # 15 min
REFRESH_TOKEN_EXPIRE = 604800  # 7 days

# Default admin user (created on first run, should be changed)
_users: dict[str, dict] = {}


def _ensure_admin():
    """Create default admin if no users exist."""
    if _users:
        return
    default_pw = os.getenv("GLASSOPS_ADMIN_PASSWORD", "admin")
    _users["admin@glassops.local"] = {
        "email": "admin@glassops.local",
        "password_hash": bcrypt.hashpw(default_pw.encode(), bcrypt.gensalt()).decode(),
        "totp_secret": None,
        "totp_enabled": False,
        "must_change_password": default_pw == "admin",
    }
    logger.info("Default admin user created: admin@glassops.local")


def verify_password(email: str, password: str) -> bool:
    _ensure_admin()
    user = _users.get(email)
    if not user:
        return False
    return bcrypt.checkpw(password.encode(), user["password_hash"].encode())


def is_totp_enabled(email: str) -> bool:
    _ensure_admin()
    user = _users.get(email)
    return bool(user and user.get("totp_enabled"))


def verify_totp(email: str, code: str) -> bool:
    user = _users.get(email)
    if not user or not user.get("totp_secret"):
        return False
    totp = pyotp.TOTP(user["totp_secret"])
    return totp.verify(code)


def setup_totp(email: str) -> dict:
    """Generate TOTP secret for user. Returns provisioning URI."""
    _ensure_admin()
    user = _users.get(email)
    if not user:
        return {"ok": False, "error": "User not found"}
    secret = pyotp.random_base32()
    user["totp_secret"] = secret
    uri = pyotp.TOTP(secret).provisioning_uri(email, issuer_name="GlassOps")
    return {"ok": True, "secret": secret, "uri": uri}


def confirm_totp(email: str, code: str) -> bool:
    """Confirm TOTP setup with first code."""
    user = _users.get(email)
    if not user or not user.get("totp_secret"):
        return False
    if verify_totp(email, code):
        user["totp_enabled"] = True
        return True
    return False


def create_access_token(email: str) -> str:
    payload = {
        "sub": email,
        "exp": time.time() + ACCESS_TOKEN_EXPIRE,
        "type": "access",
    }
    return jwt.encode(payload, settings.secret_key, algorithm=ALGORITHM)


def create_refresh_token(email: str) -> str:
    payload = {
        "sub": email,
        "exp": time.time() + REFRESH_TOKEN_EXPIRE,
        "type": "refresh",
    }
    return jwt.encode(payload, settings.secret_key, algorithm=ALGORITHM)


def verify_token(token: str, token_type: str = "access") -> str | None:
    """Returns email if valid, None otherwise."""
    try:
        payload = jwt.decode(token, settings.secret_key, algorithms=[ALGORITHM])
        if payload.get("type") != token_type:
            return None
        if payload.get("exp", 0) < time.time():
            return None
        return payload.get("sub")
    except JWTError:
        return None


def change_password(email: str, old_password: str, new_password: str) -> bool:
    if not verify_password(email, old_password):
        return False
    user = _users.get(email)
    if not user:
        return False
    user["password_hash"] = bcrypt.hashpw(new_password.encode(), bcrypt.gensalt()).decode()
    return True
