"""Supervisord service control — restart individual services."""

import asyncio
import logging

logger = logging.getLogger("glassops.supervisor")

ALLOWED_SERVICES = {"agent", "nginx", "all"}


async def restart_service(service: str) -> dict:
    """Restart a supervisord-managed service. Returns {"ok": bool, "output"?: str}."""
    if service not in ALLOWED_SERVICES:
        return {"ok": False, "error": f"Invalid service: {service}"}

    cmd = ["supervisorctl", "restart", service]

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
        return {"ok": False, "error": "Restart timed out"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


async def get_service_status() -> dict:
    """Get supervisord status for all services."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "supervisorctl", "status",
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
    except Exception as e:
        return {"ok": False, "error": str(e)}
