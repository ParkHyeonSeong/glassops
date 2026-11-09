"""Runtime settings API — manage server config from web UI."""

import asyncio
import ipaddress
import os
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.database import get_runtime_config, set_runtime_configs, audit
from app.dependencies import require_admin
from app.net import resolve_client_ip
from app.runtime_config_validate import validate_config_value, normalize_ip_rules
from app.services.supervisor_service import restart_service, get_service_status, get_ip_rules_state, apply_ip_rules

router = APIRouter(prefix="/api/settings", tags=["settings"])

# Defaults match .env defaults
DEFAULTS = {
    "enable_gpu": "true" if os.getenv("GLASSOPS_ENABLE_GPU", "false").lower() == "true" else "false",
    "enable_docker": "true" if os.getenv("GLASSOPS_ENABLE_DOCKER", "false").lower() == "true" else "false",
    "collect_interval": str(max(1, int(os.getenv("GLASSOPS_COLLECT_INTERVAL", "1")))),
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


class IpRulesApply(BaseModel):
    allowed_ips: str


def current_client_ip(request: Request) -> str | None:
    try:
        return str(ipaddress.ip_address(resolve_client_ip(request.scope)))
    except ValueError:
        return None


def check_ip_access(value: str, request: Request) -> None:
    client_ip = current_client_ip(request)
    if client_ip is None:
        raise HTTPException(400, "Could not determine your IP. IP rules were not applied.")
    normalized = normalize_ip_rules(value)
    if not normalized or client_ip in ("127.0.0.1", "::1"):
        return
    client_addr = ipaddress.ip_address(client_ip)
    if not any(client_addr in ipaddress.ip_network(entry) for entry in normalized.split(",")):
        raise HTTPException(400, f"Your IP ({client_ip}) would be blocked. Add it to the whitelist.")


def gpu_access_available() -> bool:
    """Probe the dashboard container, even when collection is switched off."""
    try:
        import pynvml
        pynvml.nvmlInit()
        try:
            return pynvml.nvmlDeviceGetCount() > 0
        finally:
            pynvml.nvmlShutdown()
    except Exception:
        return False


@router.get("/runtime")
async def get_config(request: Request, _: str = Depends(require_admin)):
    db_config = await get_runtime_config()
    # Merge: DB overrides defaults
    merged = {key: db_config.get(key, value) for key, value in DEFAULTS.items()}
    ip_state = await get_ip_rules_state()
    has_draft = "allowed_ips" in db_config
    pending = has_draft
    if has_draft and ip_state["active"] is not None:
        try:
            pending = normalize_ip_rules(db_config["allowed_ips"]) != ip_state["active"]
        except ValueError:
            pass  # An invalid legacy draft is never interpreted as allow-all.
    return {
        "config": merged, "gpu_available": await asyncio.to_thread(gpu_access_available),
        "ip_rules": {**ip_state, "pending": pending, "has_saved_draft": has_draft,
                     "client_ip": current_client_ip(request)},
    }


@router.post("/runtime")
async def update_config(body: RuntimeConfigUpdate, request: Request,
                        actor: str = Depends(require_admin)):
    updates = {}
    for key, value in body.model_dump(exclude_none=True).items():
        # Shared format validation (same checks the DB setter enforces).
        try:
            validate_config_value(key, value)
        except ValueError as e:
            raise HTTPException(400, str(e))

        # allowed_ips: additional request-context self-lockout guard.
        if key == "allowed_ips" and value:
            check_ip_access(value, request)

        updates[key] = value

    if updates.get("enable_gpu") == "true" and not await asyncio.to_thread(gpu_access_available):
        raise HTTPException(409, "GPU access is unavailable in the dashboard container. "
                            "Check the host NVIDIA driver and Container Toolkit, then recreate "
                            "the container with make up-gpu.")

    if updates:
        await set_runtime_configs(updates)
        await audit(actor, "settings.update", detail={"updated": updates})

    return {"ok": True, "updated": list(updates.keys())}


@router.post("/ip-rules/apply")
async def confirm_ip_rules(body: IpRulesApply, request: Request, actor: str = Depends(require_admin)):
    saved = await get_runtime_config()
    if "allowed_ips" not in saved or body.allowed_ips != saved["allowed_ips"]:
        raise HTTPException(409, "The saved IP draft changed or does not exist. Save and review it before applying.")
    try:
        validate_config_value("allowed_ips", body.allowed_ips)
    except ValueError as error:
        raise HTTPException(400, str(error)) from None
    check_ip_access(body.allowed_ips, request)
    result = await apply_ip_rules(body.allowed_ips)
    await audit(actor, "settings.ip_rules.apply", detail={"allowed_ips": body.allowed_ips, **result})
    if result["outcome"] == "restored":
        raise HTTPException(500, result)
    return JSONResponse(result, status_code=200 if result["ok"] else 202)


@router.post("/restart")
async def restart(body: RestartRequest, actor: str = Depends(require_admin)):
    result = await restart_service(body.service)
    if not result.get("ok"):
        raise HTTPException(500, result.get("error", "Restart failed"))
    await audit(actor, "service.restart", detail={"service": body.service})
    return result


@router.get("/status")
async def status(_: str = Depends(require_admin)):
    return await get_service_status()
