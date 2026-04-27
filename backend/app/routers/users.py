"""Admin user management + per-user host-account mapping."""

import re
from typing import Literal

import bcrypt
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, EmailStr, Field

from app.database import (
    count_active_admins,
    create_user,
    delete_user,
    get_user,
    get_user_host_accounts,
    list_users,
    set_user_host_accounts,
    update_user,
)
from app.routers.auth import get_current_user
from app.services.auth_service import validate_password

router = APIRouter(prefix="/api/users", tags=["users"])

HOST_USER_PATTERN = re.compile(r"^[a-zA-Z0-9_][a-zA-Z0-9_-]{0,31}$")
AGENT_ID_PATTERN = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")


async def require_admin(email: str = Depends(get_current_user)) -> str:
    user = await get_user(email)
    if not user or user.get("role") != "admin" or not user.get("is_active", True):
        raise HTTPException(403, "Admin access required")
    return email


class CreateUserRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8)
    role: Literal["admin", "user"] = "user"


class UpdateUserRequest(BaseModel):
    role: Literal["admin", "user"] | None = None
    is_active: bool | None = None
    new_password: str | None = Field(default=None, min_length=8)


class HostMappingRequest(BaseModel):
    # agent_id -> host_user. Empty values are treated as "remove".
    accounts: dict[str, str]


# ── User CRUD ─────────────────────────────────────────────────────────


@router.get("")
async def get_users(_: str = Depends(require_admin)):
    return {"users": await list_users()}


@router.post("")
async def post_user(body: CreateUserRequest, _: str = Depends(require_admin)):
    if await get_user(body.email):
        raise HTTPException(409, "User already exists")
    pw_check = validate_password(body.password)
    if not pw_check["valid"]:
        raise HTTPException(400, {"error": "Password does not meet requirements", "checks": pw_check["checks"]})
    pw_hash = bcrypt.hashpw(body.password.encode(), bcrypt.gensalt()).decode()
    ok = await create_user(body.email, pw_hash, role=body.role, must_change_password=True)
    if not ok:
        raise HTTPException(500, "Failed to create user")
    return {"ok": True, "email": body.email}


@router.patch("/{target_email}")
async def patch_user(
    target_email: str,
    body: UpdateUserRequest,
    actor: str = Depends(require_admin),
):
    target = await get_user(target_email)
    if not target:
        raise HTTPException(404, "User not found")

    fields: dict = {}

    if body.role is not None and body.role != target["role"]:
        # Block demoting the last active admin.
        if target["role"] == "admin" and body.role != "admin":
            if await count_active_admins() <= 1:
                raise HTTPException(400, "Cannot demote the last active admin")
        fields["role"] = body.role

    if body.is_active is not None and bool(body.is_active) != target["is_active"]:
        if not body.is_active:
            if target["role"] == "admin" and await count_active_admins() <= 1:
                raise HTTPException(400, "Cannot deactivate the last active admin")
            if target_email == actor:
                raise HTTPException(400, "Cannot deactivate yourself")
        fields["is_active"] = 1 if body.is_active else 0

    if body.new_password is not None:
        pw_check = validate_password(body.new_password)
        if not pw_check["valid"]:
            raise HTTPException(400, {"error": "Password does not meet requirements", "checks": pw_check["checks"]})
        fields["password_hash"] = bcrypt.hashpw(body.new_password.encode(), bcrypt.gensalt()).decode()
        fields["must_change_password"] = 1

    if not fields:
        return {"ok": True, "noop": True}

    await update_user(target_email, **fields)
    return {"ok": True}


@router.delete("/{target_email}")
async def delete_user_route(target_email: str, actor: str = Depends(require_admin)):
    target = await get_user(target_email)
    if not target:
        raise HTTPException(404, "User not found")
    if target_email == actor:
        raise HTTPException(400, "Cannot delete yourself")
    if target["role"] == "admin" and await count_active_admins() <= 1:
        raise HTTPException(400, "Cannot delete the last active admin")
    await delete_user(target_email)
    return {"ok": True}


# ── Host account mapping ──────────────────────────────────────────────


@router.get("/{target_email}/hosts")
async def get_hosts(target_email: str, _: str = Depends(require_admin)):
    if not await get_user(target_email):
        raise HTTPException(404, "User not found")
    return {"accounts": await get_user_host_accounts(target_email)}


@router.put("/{target_email}/hosts")
async def put_hosts(target_email: str, body: HostMappingRequest, _: str = Depends(require_admin)):
    if not await get_user(target_email):
        raise HTTPException(404, "User not found")
    cleaned: dict[str, str] = {}
    for agent_id, host_user in body.accounts.items():
        if not AGENT_ID_PATTERN.match(agent_id):
            raise HTTPException(400, f"Invalid agent_id: {agent_id}")
        if host_user and not HOST_USER_PATTERN.match(host_user):
            raise HTTPException(400, f"Invalid host_user for {agent_id}")
        cleaned[agent_id] = host_user
    await set_user_host_accounts(target_email, cleaned)
    return {"ok": True}
