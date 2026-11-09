"""Supervisord service control — restart individual services."""

import asyncio
import logging
import os

from urllib.error import URLError
from urllib.request import ProxyHandler, build_opener

from app.nginx_config import read_active_ip_rules
from app.runtime_config_validate import normalize_ip_rules

logger = logging.getLogger("glassops.supervisor")

ALLOWED_SERVICES = {"agent", "nginx", "all"}


async def restart_service(service: str) -> dict:
    """Restart a supervisord-managed service. Returns {"ok": bool, "output"?: str}."""
    if service not in ALLOWED_SERVICES:
        return {"ok": False, "error": f"Invalid service: {service}"}

    cmd = ["supervisorctl", "-c", "/etc/supervisor/conf.d/glassops.conf", "restart", service]

    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=15)
        output = (stdout or b"").decode().strip()
        error = (stderr or b"").decode().strip()

        if proc.returncode == 0:
            logger.info("Restarted service: %s — %s", service, output)
            return {"ok": True, "output": output}
        else:
            logger.error("Failed to restart %s: %s", service, error or output)
            return {"ok": False, "error": error or output}
    except asyncio.TimeoutError:
        if proc is not None:
            await _reap_client(proc)
        # Killing supervisorctl does NOT cancel the supervisor-side operation.
        return {"ok": False, "timed_out": True, "error": "Restart timed out; the operation may still be running"}
    except Exception as e:
        return {"ok": False, "uncertain": proc is not None, "error": str(e)}


async def _reap_client(proc) -> None:
    try:
        proc.kill()
    except ProcessLookupError:
        pass
    await proc.communicate()


async def get_service_status() -> dict:
    """Get supervisord status for all services."""
    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            "supervisorctl", "-c", "/etc/supervisor/conf.d/glassops.conf", "status",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
        lines = (stdout or b"").decode().strip().split("\n")

        services = {}
        for line in lines:
            parts = line.split()
            if len(parts) >= 2:
                services[parts[0]] = parts[1]  # e.g. "agent" -> "RUNNING"

        return {"ok": True, "services": services}
    except asyncio.TimeoutError:
        if proc is not None:
            await _reap_client(proc)
        return {"ok": False, "error": "Status check timed out"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


async def nginx_ready() -> bool:
    status = await get_service_status()
    if status.get("services", {}).get("nginx") != "RUNNING":
        return False
    # RUNNING may still be the Python startup wrapper. Probe the actual listener.
    return await asyncio.to_thread(_nginx_responding)


def _nginx_responding() -> bool:
    try:
        # Standard library only: httpx is a test dependency, not in the image.
        with build_opener(ProxyHandler({})).open("http://127.0.0.1:7440/health", timeout=1) as response:
            return response.status == 200
    except (OSError, URLError):
        return False


async def get_ip_rules_state() -> dict:
    ready = await nginx_ready()
    return {"active": read_active_ip_rules() if ready else None, "nginx_ready": ready}


async def apply_ip_rules(value: str) -> dict:
    from app import database as db

    value = normalize_ip_rules(value)
    previous = await db.get_confirmed_ip_rules()
    await db.set_confirmed_ip_rules(value)  # A failed write must never restart nginx.
    result = await restart_service("nginx")
    unknown = {
        "ok": False, "outcome": "unknown",
        "error": "Apply status needs verification. Confirmed rules may take effect on the next nginx start.",
    }
    if result.get("timed_out") or result.get("uncertain"):
        return unknown
    if result.get("ok"):
        state = await get_ip_rules_state()
        if state["nginx_ready"] and state["active"] == value:
            return {"ok": True, "outcome": "applied", **state}
        return unknown

    # Only a completed failure may start recovery; no competing restart on timeout.
    try:
        await db.set_confirmed_ip_rules(previous)
        recovered = await restart_service("nginx")
        state = await get_ip_rules_state()
        old_rules = normalize_ip_rules(previous if previous is not None else os.getenv("GLASSOPS_ALLOWED_IPS", ""))
        if recovered.get("ok") and state["nginx_ready"] and state["active"] == old_rules:
            return {"ok": False, "outcome": "restored", "error": "IP apply failed. Previous rules were restored."}
    except Exception:
        logger.exception("Could not restore the previous confirmed IP rules")
    return {**unknown, "error": "IP apply failed and recovery could not be verified. Check the current rules or use host recovery."}
