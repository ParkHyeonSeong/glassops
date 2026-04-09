"""Runtime settings API — manage server config from web UI."""

import ipaddress
import os
import re
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from app.database import get_runtime_config, set_runtime_configs
from app.services.supervisor_service import restart_service, get_service_status

USERNAME_PATTERN = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")

router = APIRouter(prefix="/api/settings", tags=["settings"])

# Defaults match .env defaults
DEFAULTS = {
    "enable_gpu": os.getenv("GLASSOPS_ENABLE_GPU", "false"),
    "enable_docker": os.getenv("GLASSOPS_ENABLE_DOCKER", "true"),
    "collect_interval": os.getenv("GLASSOPS_COLLECT_INTERVAL", "1"),
    "terminal_user": os.getenv("GLASSOPS_TERMINAL_USER", ""),
    "allowed_ips": os.getenv("GLASSOPS_ALLOWED_IPS", ""),
}


class RuntimeConfigUpdate(BaseModel):
    enable_gpu: str | None = None
    enable_docker: str | None = None
    collect_interval: str | None = None
    terminal_user: str | None = None
    allowed_ips: str | None = None


class RestartRequest(BaseModel):
    service: Literal["agent", "nginx", "all"]


@router.get("/runtime")
async def get_config():
    db_config = await get_runtime_config()
    # Merge: DB overrides defaults
    merged = {**DEFAULTS, **db_config}
    return {"config": merged}


@router.post("/runtime")
async def update_config(body: RuntimeConfigUpdate, request: Request):
    updates = {}
    for key, value in body.model_dump(exclude_none=True).items():
        # Validate each field
        if key == "collect_interval":
            try:
                val = int(value)
                if val < 1 or val > 60:
                    raise HTTPException(400, "Interval must be 1-60")
            except ValueError:
                raise HTTPException(400, "Interval must be a number")

        elif key in ("enable_gpu", "enable_docker"):
            if value not in ("true", "false"):
                raise HTTPException(400, f"{key} must be 'true' or 'false'")

        elif key == "terminal_user":
            if value and not USERNAME_PATTERN.match(value):
                raise HTTPException(400, "Invalid username format")

        elif key == "allowed_ips":
            if value:
                # Validate CIDR + self-lockout check
                client_ip = request.headers.get("x-real-ip", "") or (request.client.host if request.client else "")
                entries = [e.strip() for e in value.split(",") if e.strip()]
                for entry in entries:
                    try:
                        ipaddress.ip_network(entry, strict=False)
                    except ValueError:
                        raise HTTPException(400, f"Invalid CIDR: {entry}")

                # Check if current client IP would be locked out
                if client_ip and entries:
                    client_addr = ipaddress.ip_address(client_ip)
                    allowed = any(
                        client_addr in ipaddress.ip_network(e, strict=False)
                        for e in entries
                    )
                    if not allowed:
                        raise HTTPException(400, f"Your IP ({client_ip}) would be blocked. Add it to the whitelist.")

        updates[key] = value

    if updates:
        await set_runtime_configs(updates)

    return {"ok": True, "updated": list(updates.keys())}


@router.post("/restart")
async def restart(body: RestartRequest):
    result = await restart_service(body.service)
    if not result.get("ok"):
        raise HTTPException(500, result.get("error", "Restart failed"))
    return result


@router.get("/status")
async def status():
    return await get_service_status()
