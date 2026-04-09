"""Server-side alert service — SMTP email notifications."""

import base64
import hashlib
import json
import logging
import time
from email.mime.text import MIMEText

import aiosmtplib
from cryptography.fernet import Fernet, InvalidToken

from app.config import settings
from app.database import get_db

logger = logging.getLogger("glassops.alerts")

_last_sent: dict[str, float] = {}
COOLDOWN_SECONDS = 300


def _get_fernet() -> Fernet:
    """Derive Fernet key from GLASSOPS_SECRET_KEY."""
    key = hashlib.sha256(settings.secret_key.encode()).digest()
    return Fernet(base64.urlsafe_b64encode(key))


def _encrypt(value: str) -> str:
    if not value:
        return ""
    return _get_fernet().encrypt(value.encode()).decode()


def _decrypt(value: str) -> str:
    if not value:
        return ""
    try:
        return _get_fernet().decrypt(value.encode()).decode()
    except InvalidToken:
        logger.warning("Failed to decrypt value — secret key may have changed")
        return ""


async def get_smtp_config() -> dict | None:
    db = await get_db()
    cursor = await db.execute("SELECT config FROM alert_config WHERE id = 1")
    row = await cursor.fetchone()
    if not row:
        return None
    config = json.loads(row["config"])
    # Decrypt password — keep encrypted value if decryption fails (key rotation)
    if config.get("password_enc"):
        decrypted = _decrypt(config["password_enc"])
        if decrypted:
            config["password"] = decrypted
        else:
            config["password"] = ""
            config["_decrypt_failed"] = True
        del config["password_enc"]
    return config


async def save_smtp_config(config: dict) -> None:
    # Merge with existing config to preserve encrypted password if not provided
    existing_raw = None
    db = await get_db()
    cursor = await db.execute("SELECT config FROM alert_config WHERE id = 1")
    row = await cursor.fetchone()
    if row:
        existing_raw = json.loads(row["config"])

    store = {**config}
    password = store.pop("password", "")

    if password and password != "********":
        # New password provided — encrypt it
        store["password_enc"] = _encrypt(password)
    elif existing_raw and existing_raw.get("password_enc"):
        # No new password — preserve existing encrypted value
        store["password_enc"] = existing_raw["password_enc"]
    # else: no password at all

    await db.execute(
        "INSERT OR REPLACE INTO alert_config (id, config) VALUES (1, ?)",
        (json.dumps(store),),
    )
    await db.commit()


async def send_alert_email(subject: str, body: str, key: str | None = None) -> dict:
    """Send alert email via SMTP. Returns {"ok": bool, "error"?: str}."""
    # Cooldown check
    if key:
        now = time.time()
        if _last_sent.get(key, 0) + COOLDOWN_SECONDS > now:
            return {"ok": False, "error": "Cooldown active"}
        _last_sent[key] = now

    config = await get_smtp_config()
    if not config or not config.get("host"):
        return {"ok": False, "error": "SMTP not configured"}

    try:
        msg = MIMEText(body, "plain", "utf-8")
        msg["Subject"] = subject
        msg["From"] = config.get("from_email", config.get("username", "glassops@localhost"))
        msg["To"] = config["to_email"]

        await aiosmtplib.send(
            msg,
            hostname=config["host"],
            port=int(config.get("port", 587)),
            username=config.get("username"),
            password=config.get("password"),
            use_tls=config.get("use_tls", False),
            start_tls=config.get("start_tls", True),
            timeout=10,
        )
        logger.info("Alert email sent: %s", subject)
        return {"ok": True}
    except Exception as e:
        logger.error("Failed to send alert email: %s", e)
        return {"ok": False, "error": str(e)}


async def check_and_alert(agent_id: str, metrics: dict) -> None:
    """Check metrics against thresholds and send email alerts."""
    config = await get_smtp_config()
    if not config or not config.get("host") or not config.get("to_email"):
        return

    thresholds = config.get("thresholds", {})
    cpu = metrics.get("cpu", {}).get("percent_total", 0)
    mem = metrics.get("memory", {}).get("percent", 0)
    disk = metrics.get("disk", {}).get("percent", 0)

    alerts = []
    if cpu > thresholds.get("cpu_crit", 90):
        alerts.append(f"CPU critical: {cpu:.1f}%")
    if mem > thresholds.get("mem_crit", 90):
        alerts.append(f"Memory critical: {mem:.1f}%")
    if disk > thresholds.get("disk_crit", 95):
        alerts.append(f"Disk critical: {disk:.1f}%")

    if alerts:
        body = f"Agent: {agent_id}\n\n" + "\n".join(alerts)
        await send_alert_email(
            f"[GlassOps] Alert — {agent_id}",
            body,
            key=f"alert-{agent_id}",
        )
