"""Authentication service — JWT + optional TOTP 2FA, backed by SQLite."""

import logging
import re
import time

import bcrypt
import pyotp
from jose import jwt, JWTError

from app.config import settings
from app.database import get_user, update_user

logger = logging.getLogger("glassops.auth")

ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE = 900
REFRESH_TOKEN_EXPIRE = 604800

# Password policy
PW_MIN = 8
PW_MAX = 256
_PW_HAS_UPPER = re.compile(r"[A-Z]")
_PW_HAS_LOWER = re.compile(r"[a-z]")
_PW_HAS_DIGIT = re.compile(r"[0-9]")
_PW_HAS_SPECIAL = re.compile(r"[^A-Za-z0-9]")


def validate_password(password: str) -> dict:
    checks = {
        "length": PW_MIN <= len(password) <= PW_MAX,
        "uppercase": bool(_PW_HAS_UPPER.search(password)),
        "lowercase": bool(_PW_HAS_LOWER.search(password)),
        "digit": bool(_PW_HAS_DIGIT.search(password)),
        "special": bool(_PW_HAS_SPECIAL.search(password)),
    }
    return {"valid": all(checks.values()), "checks": checks}


async def verify_password(email: str, password: str) -> bool:
    user = await get_user(email)
    if not user:
        return False
    return bcrypt.checkpw(password.encode(), user["password_hash"].encode())


async def must_change_password(email: str) -> bool:
    user = await get_user(email)
    return bool(user and user.get("must_change_password"))


async def is_totp_enabled(email: str) -> bool:
    user = await get_user(email)
    return bool(user and user.get("totp_enabled"))


async def verify_totp(email: str, code: str) -> bool:
    user = await get_user(email)
    if not user or not user.get("totp_secret"):
        return False
    return pyotp.TOTP(user["totp_secret"]).verify(code)


async def setup_totp(email: str) -> dict:
    user = await get_user(email)
    if not user:
        return {"ok": False, "error": "User not found"}
    secret = pyotp.random_base32()
    await update_user(email, totp_secret=secret)
    uri = pyotp.TOTP(secret).provisioning_uri(email, issuer_name="GlassOps")
    return {"ok": True, "secret": secret, "uri": uri}


async def confirm_totp(email: str, code: str) -> bool:
    if await verify_totp(email, code):
        await update_user(email, totp_enabled=1)
        return True
    return False


def create_access_token(email: str) -> str:
    return jwt.encode(
        {"sub": email, "exp": time.time() + ACCESS_TOKEN_EXPIRE, "type": "access"},
        settings.secret_key, algorithm=ALGORITHM,
    )


def create_refresh_token(email: str) -> str:
    return jwt.encode(
        {"sub": email, "exp": time.time() + REFRESH_TOKEN_EXPIRE, "type": "refresh"},
        settings.secret_key, algorithm=ALGORITHM,
    )


def verify_token(token: str, token_type: str = "access") -> str | None:
    try:
        payload = jwt.decode(
            token, settings.secret_key, algorithms=[ALGORITHM],
            options={"verify_exp": False},
        )
        if payload.get("type") != token_type:
            return None
        if payload.get("exp", 0) < time.time():
            return None
        return payload.get("sub")
    except JWTError:
        return None


async def change_password(email: str, old_password: str, new_password: str) -> dict:
    if not await verify_password(email, old_password):
        return {"ok": False, "error": "Invalid current password"}
    validation = validate_password(new_password)
    if not validation["valid"]:
        return {"ok": False, "error": "Password does not meet requirements", "checks": validation["checks"]}
    pw_hash = bcrypt.hashpw(new_password.encode(), bcrypt.gensalt()).decode()
    await update_user(email, password_hash=pw_hash, must_change_password=0)
    return {"ok": True}


async def force_change_password(email: str, new_password: str) -> dict:
    user = await get_user(email)
    if not user:
        return {"ok": False, "error": "User not found"}
    if not user.get("must_change_password"):
        return {"ok": False, "error": "Password change not required"}
    validation = validate_password(new_password)
    if not validation["valid"]:
        return {"ok": False, "error": "Password does not meet requirements", "checks": validation["checks"]}
    pw_hash = bcrypt.hashpw(new_password.encode(), bcrypt.gensalt()).decode()
    await update_user(email, password_hash=pw_hash, must_change_password=0)
    return {"ok": True}
