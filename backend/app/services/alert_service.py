"""Server-side alert service — SMTP email notifications."""

import asyncio
import base64
import copy
import json
import logging
import time
from email.mime.text import MIMEText

import aiosmtplib
from cryptography.fernet import Fernet, InvalidToken
from pydantic import EmailStr, TypeAdapter, ValidationError

from app.config import smtp_fernet_key
from app.database import get_db
from app.services.smtp_validate import validate_smtp_target

logger = logging.getLogger("glassops.alerts")

_last_sent: dict[str, float] = {}
COOLDOWN_SECONDS = 300

# A failed send must not suppress the next real alert for the full cooldown, but it
# must still be throttled: check_and_alert() is awaited inline in the ingest loop and
# agents collect once per second by default, so an unthrottled failure path means one
# blocking SMTP attempt per second per agent. Operator-approved policy.
FAILURE_BACKOFF_SECONDS = 60
_last_attempt: dict[str, float] = {}

# Hard ceiling on the SSRF/DNS check. validate_smtp_target calls socket.getaddrinfo,
# which can hang indefinitely on a black-holed resolver. Off-loading it to a thread
# frees the event loop, but the awaiting coroutine (an agent's ingest, or a config
# POST) would still wait forever without this bound. Note the worker thread itself is
# NOT cancellable — it runs to its own completion; only the caller is released.
DNS_TIMEOUT = 5.0

# What GET /api/alerts/config returns in place of the stored password, and what a
# client may POST back to mean "keep the password you already have".
MASKED_PASSWORD = "********"

# Canonical transport security modes -> (use_tls, start_tls). aiosmtplib 3.0.2
# raises ValueError("The start_tls and use_tls options are not compatible.") from
# SMTP.__init__ when both are set, so exactly one may ever be true.
SECURITY_FLAGS: dict[str, tuple[bool, bool]] = {
    "starttls": (False, True),
    "implicit_tls": (True, False),
    "none": (False, False),
}

_EMAIL_ADAPTER = TypeAdapter(EmailStr)

# Distinguishes "key absent" from "key present but empty" when reading a stored row.
_ABSENT = object()

# Fixed, non-reflective messages. An SMTP exception's str() carries whatever the
# server echoed — the envelope sender and recipients (SMTPSenderRefused /
# SMTPRecipientsRefused put real addresses in .args) and, for a server that rejects
# AUTH, our own base64 AUTH PLAIN blob. Scrubbing known substrings out of that text
# is a denylist and cannot be complete, so nothing derived from the exception text is
# ever returned or logged. Order matters: SMTPConnectTimeoutError subclasses BOTH
# SMTPTimeoutError and SMTPConnectError, and both subclass OSError.
_SAFE_SMTP_ERRORS: tuple[tuple[type[BaseException], str], ...] = (
    (aiosmtplib.SMTPAuthenticationError, "SMTP authentication failed"),
    (aiosmtplib.SMTPSenderRefused, "SMTP sender rejected"),
    (aiosmtplib.SMTPRecipientsRefused, "SMTP recipient rejected"),
    (aiosmtplib.SMTPTimeoutError, "SMTP connection timed out"),
    (aiosmtplib.SMTPConnectError, "SMTP connection failed"),
    (aiosmtplib.SMTPServerDisconnected, "SMTP server disconnected"),
)


def safe_smtp_error(exc: BaseException) -> str:
    """Map an SMTP failure to a fixed message that can never carry credentials."""
    for exc_type, message in _SAFE_SMTP_ERRORS:
        if isinstance(exc, exc_type):
            return message
    return "SMTP send failed"


def _reset_alert_state_for_test() -> None:
    """Clear the module-level cooldown tables and config cache (tests only)."""
    _last_sent.clear()
    _last_attempt.clear()
    _invalidate_config_cache()


def _now() -> float:
    """The single clock for every duration policy here.

    The cooldown, the failure backoff and the config-cache TTL are all *elapsed
    time* contracts, so they must not read a clock an operator or NTP can step.
    A backward correction would stretch a 5-minute cooldown into hours and drop
    real alerts; a forward jump would expire it early and let a flood through.
    time.monotonic() cannot be set and never goes backwards.
    """
    return time.monotonic()


def _clean(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _is_email(value: str) -> bool:
    try:
        _EMAIL_ADAPTER.validate_python(value)
    except ValidationError:
        return False
    return True


def security_mode(config: dict) -> str | None:
    """Canonical mode, back-filling rows written before `security` existed.

    Returns None whenever the stored transport cannot be determined *unambiguously*.
    Callers must treat None as "the operator has to re-pick the mode" and refuse to
    send — never as a default. Three ways to land there:

      * `security` is present but not a supported mode. Falling back to the legacy
        flags here would be fail-OPEN: a row saying `security: "ssl"` with both
        flags false resolves to "none", i.e. plaintext SMTP for a config that
        explicitly asked for TLS.
      * the legacy flags are not real booleans. `bool("false")` is True, so a
        stringly-typed row would otherwise be read as the opposite of its intent.
      * both legacy flags are true — a combination aiosmtplib refuses outright.
    """
    if config.get("security") is not None:      # explicit; null/absent means legacy
        mode = config["security"]
        return mode if mode in SECURITY_FLAGS else None

    use_tls = config.get("use_tls", False)
    start_tls = config.get("start_tls", True)
    if not isinstance(use_tls, bool) or not isinstance(start_tls, bool):
        return None
    if use_tls and start_tls:
        return None
    if use_tls:
        return "implicit_tls"
    return "starttls" if start_tls else "none"


def resolve_sender(config: dict) -> str | None:
    """The From address: explicit from_email, else username when it is itself an
    address. A username is an SMTP login identifier and need not be an email, so it
    is only borrowed when it actually validates as one — otherwise we would emit
    MAIL FROM:<relay-login> or, worse, the null reverse-path MAIL FROM:<>.

    from_email is re-validated here rather than trusted: rows written before the API
    validated it are still in the database, and this is the last gate before the
    value reaches the wire.
    """
    from_email = _clean(config.get("from_email"))
    if from_email:
        return from_email if _is_email(from_email) else None
    username = _clean(config.get("username"))
    return username if _is_email(username) else None


async def validate_smtp_target_async(host, port) -> None:
    """Bounded, off-loop SSRF/DNS validation. Both the send path and the config POST
    use this — never validate_smtp_target directly — so getaddrinfo can neither block
    the event loop nor hang the caller. Raises ValueError on a disallowed target OR
    on timeout; the message is fixed and carries no credential (the host is not a
    secret). validate_smtp_target is looked up as a module global at call time, so a
    test that patches it here is honoured through this wrapper.
    """
    try:
        await asyncio.wait_for(
            asyncio.to_thread(validate_smtp_target, host, port), timeout=DNS_TIMEOUT
        )
    except asyncio.TimeoutError:
        raise ValueError("SMTP host validation timed out")

# The SMTP config changes rarely but check_and_alert() runs on every metric message
# from every agent — cache it so we don't hit the DB + Fernet-decrypt per message.
_cfg_cache: dict = {"value": None, "ts": 0.0, "cached": False}
_CFG_TTL = 30.0


def _invalidate_config_cache() -> None:
    _cfg_cache["cached"] = False


def _get_fernet() -> Fernet:
    """Derive Fernet key from the master secret (domain-separated)."""
    key = smtp_fernet_key()
    return Fernet(base64.urlsafe_b64encode(key))


def _encrypt(value: str) -> str:
    if not value:
        return ""
    return _get_fernet().encrypt(value.encode()).decode()


def _decrypt(value: object) -> str:
    """Return the plaintext, or "" for anything that cannot be decrypted.

    Deliberately fail-closed on every bad input rather than raising: a row whose
    password_enc is a non-string (hand-edited JSON, a schema change) would
    otherwise raise AttributeError out of get_smtp_config and 500 the admin API.
    Callers distinguish "no password" from "could not decrypt" via _decrypt_failed.
    """
    if not isinstance(value, str) or not value:
        return ""
    try:
        return _get_fernet().decrypt(value.encode()).decode()
    except (InvalidToken, ValueError, TypeError):
        logger.warning("Failed to decrypt value — secret key may have changed")
        return ""


async def get_smtp_config() -> dict | None:
    now = _now()
    if _cfg_cache["cached"] and now - _cfg_cache["ts"] < _CFG_TTL:
        cached = _cfg_cache["value"]
        return copy.deepcopy(cached) if cached is not None else None  # deep copy so callers can't mutate the cache

    db = await get_db()
    cursor = await db.execute("SELECT config FROM alert_config WHERE id = 1")
    row = await cursor.fetchone()
    config: dict | None
    if not row:
        config = None
    else:
        config = json.loads(row["config"])
        # Always strip the ciphertext, whatever shape it is in. pop() rather than a
        # del inside the branch: an empty or non-string password_enc would otherwise
        # survive into the returned config, and the API only filters keys starting
        # with "_", so it would be served to the client.
        password_enc = config.pop("password_enc", _ABSENT)
        # Sentinel, not truthiness: the key being PRESENT means a password was meant
        # to be stored, so a blank or unusable value is a failure to recover it — not
        # "no password". save_smtp_config never writes an empty password_enc, so an
        # empty one is a malformed row, and refusing beats silently downgrading to an
        # anonymous session.
        if password_enc is not _ABSENT:
            decrypted = _decrypt(password_enc)
            config["password"] = decrypted
            if not decrypted:
                # Wrong key (secret rotated) or corrupt ciphertext. Surfaced so the
                # admin is told to re-enter it, and the send path refuses rather
                # than silently authenticating with a blank password.
                config["_decrypt_failed"] = True

    _cfg_cache.update(value=config, ts=now, cached=True)
    return copy.deepcopy(config) if config is not None else None


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
    clear_password = bool(store.pop("clear_password", False))

    if clear_password:
        # Explicit removal — write no password_enc at all. This is the only way to
        # reach a no-password-stored state; "" and the mask both mean "keep".
        pass
    elif password and password != MASKED_PASSWORD:
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
    _invalidate_config_cache()  # next read reflects the change immediately


async def send_alert_email(subject: str, body: str, key: str | None = None) -> dict:
    """Send an alert email via SMTP. Returns {"ok": bool, "error"?: str}.

    ok=True means the SMTP server accepted the message for delivery — it is not
    proof it reached the inbox.

    The full cooldown is recorded only after that acceptance, so a failed attempt
    never suppresses the next one for five minutes — it only backs off for
    FAILURE_BACKOFF_SECONDS, which keeps a dead relay from being retried once per
    collection tick. Both are keyed per agent (see check_and_alert), not per
    resource. A keyless call — the admin's manual /api/alerts/test — bypasses both.
    """
    if key:
        now = _now()
        if _last_sent.get(key, 0) + COOLDOWN_SECONDS > now:
            return {"ok": False, "error": "Cooldown active"}
        if _last_attempt.get(key, 0) + FAILURE_BACKOFF_SECONDS > now:
            return {"ok": False, "error": "Backing off after a failed send"}

    config = await get_smtp_config()
    if not config or not config.get("host"):
        return {"ok": False, "error": "SMTP not configured"}
    if config.get("_decrypt_failed"):
        return {"ok": False, "error": "Stored SMTP password could not be decrypted — re-enter it"}

    mode = security_mode(config)
    if mode is None:
        return {"ok": False, "error": "SMTP security mode is ambiguous — re-select it"}

    sender = resolve_sender(config)
    if not sender:
        return {"ok": False, "error": "No valid From Email is configured"}
    recipient = _clean(config.get("to_email"))
    if not _is_email(recipient):
        return {"ok": False, "error": "No valid recipient is configured"}

    # Credentials are all-or-nothing. aiosmtplib authenticates whenever username is
    # not None and turns a None password into "" (smtp.py:530-534), so a username
    # left behind by clear_password would retry AUTH PLAIN with a blank secret —
    # repeated auth failures, or a lockout on the relay. The mirror case is quieter
    # but worse: a password with no username is ignored entirely, so the operator
    # believes the relay is authenticated when the session is anonymous.
    username = _clean(config.get("username")) or None
    password = config.get("password") or None
    if (username is None) != (password is None):
        return {"ok": False, "error":
                "SMTP credentials are incomplete — set both a username and a password, or neither"}

    # Record the attempt BEFORE the first thing that can block on the network. DNS
    # resolution is a network call and can hang; recording after it would let a
    # failing resolver be re-invoked once per collection tick. Everything above this
    # line is a cheap in-memory check, so throttling those would only delay recovery
    # once the operator fixes the configuration.
    if key:
        _last_attempt[key] = _now()

    # Defense-in-depth: re-validate at send time so a config written before this
    # check (or by another code path) can't be used as an SSRF primitive. Bounded
    # and off-loop — see validate_smtp_target_async.
    try:
        await validate_smtp_target_async(config["host"], config.get("port", 587))
    except ValueError as e:
        logger.warning("Refusing to send via disallowed/unresolvable SMTP target: %s", e)
        return {"ok": False, "error": "SMTP host not allowed"}

    use_tls, start_tls = SECURITY_FLAGS[mode]

    try:
        msg = MIMEText(body, "plain", "utf-8")
        msg["Subject"] = subject
        msg["From"] = sender
        msg["To"] = recipient

        await aiosmtplib.send(
            msg,
            hostname=_clean(config["host"]),
            port=int(config.get("port", 587)),
            # Both None (anonymous relay) or both set — enforced above.
            username=username,
            password=password,
            use_tls=use_tls,
            start_tls=start_tls,
            timeout=10,
        )
    except Exception as e:
        # NEVER put str(e) in the response or the log — see _SAFE_SMTP_ERRORS.
        # Only the exception TYPE informs the message; the class name is safe.
        logger.error(
            "Failed to send alert email (%s): %s", type(e).__name__, safe_smtp_error(e),
        )
        return {"ok": False, "error": safe_smtp_error(e)}

    logger.info("SMTP server accepted alert email: %s", subject)
    if key:
        _last_sent[key] = _now()
    return {"ok": True}


async def check_and_alert(agent_id: str, metrics: dict) -> None:
    """Check aggregate metrics against the email thresholds and send one message.

    Per-core CPU is deliberately not examined — only cpu.percent_total can raise an
    email alert, matching deriveAlerts() in frontend/src/lib/alerts.ts. Comparison
    is >= so the configured value itself fires, matching severityFor().
    """
    config = await get_smtp_config()
    if not config or not config.get("host") or not config.get("to_email"):
        return

    thresholds = config.get("thresholds") or {}
    cpu = metrics.get("cpu", {}).get("percent_total", 0)
    mem = metrics.get("memory", {}).get("percent", 0)
    disk = metrics.get("disk", {}).get("percent", 0)

    alerts = []
    if cpu >= thresholds.get("cpu_crit", 90):
        alerts.append(f"CPU critical: {cpu:.1f}%")
    if mem >= thresholds.get("mem_crit", 90):
        alerts.append(f"Memory critical: {mem:.1f}%")
    if disk >= thresholds.get("disk_crit", 95):
        alerts.append(f"Disk critical: {disk:.1f}%")

    if alerts:
        body = f"Agent: {agent_id}\n\n" + "\n".join(alerts)
        await send_alert_email(
            f"[GlassOps] Alert — {agent_id}",
            body,
            key=f"alert-{agent_id}",
        )
