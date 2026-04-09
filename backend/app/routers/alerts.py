"""Alert configuration API — SMTP settings."""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, EmailStr

from app.services.alert_service import get_smtp_config, save_smtp_config, send_alert_email

router = APIRouter(prefix="/api/alerts", tags=["alerts"])

class SmtpConfig(BaseModel):
    host: str
    port: int = 587
    username: str = ""
    password: str = ""
    from_email: str = ""
    to_email: EmailStr
    use_tls: bool = False
    start_tls: bool = True
    thresholds: dict = {"cpu_crit": 90, "mem_crit": 90, "disk_crit": 95}


@router.get("/config")
async def get_config():
    config = await get_smtp_config()
    if not config:
        return {"configured": False}
    # Mask password
    safe = {**config, "password": "********" if config.get("password") else ""}
    return {"configured": True, **safe}


@router.post("/config")
async def set_config(body: SmtpConfig):
    config = body.model_dump()
    # "********" means "keep existing" — save_smtp_config handles preservation
    await save_smtp_config(config)
    return {"ok": True}


@router.post("/test")
async def test_email():
    result = await send_alert_email(
        "[GlassOps] Test Alert",
        "This is a test email from GlassOps. If you received this, SMTP is configured correctly.",
    )
    if not result.get("ok"):
        raise HTTPException(400, result.get("error", "Send failed"))
    return {"ok": True}
