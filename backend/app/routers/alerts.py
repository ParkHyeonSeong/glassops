"""Alert configuration API — SMTP settings. Admin-only."""

import json
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import (
    BaseModel, ConfigDict, EmailStr, Field, TypeAdapter,
    ValidationError, ValidationInfo, field_validator, model_validator,
)

from app.dependencies import require_admin
from app.services.alert_service import (
    MASKED_PASSWORD,
    SECURITY_FLAGS,
    get_smtp_config,
    resolve_sender,
    save_smtp_config,
    security_mode,
    send_alert_email,
    validate_smtp_target_async,
)
# The router does not import validate_smtp_target directly — it goes through the
# service's bounded, off-loop validate_smtp_target_async. A test therefore patches
# svc.validate_smtp_target (one binding), not a router-local name.

router = APIRouter(prefix="/api/alerts", tags=["alerts"])

SecurityMode = Literal["starttls", "implicit_tls", "none"]

_EMAIL = TypeAdapter(EmailStr)


def _safe_errors(exc: ValidationError) -> list[dict]:
    """Validation errors stripped to loc/msg/type only.

    Pydantic attaches the offending value to every error as `input`, and for the
    errors it raises itself — a missing required field, a wrong top-level type — that
    value is the ENTIRE request body. FastAPI's default 422 handler serialises it
    verbatim, so a request that merely omits `to_email` would echo the plaintext SMTP
    password back to the client and into any response log. `ctx` can carry it too.
    Neither is ever included; `hide_input_in_errors` is not sufficient because the
    value still reaches errors() on some paths, so this allowlist is the control.
    """
    return [
        {"loc": [str(part) for part in err["loc"]], "msg": err["msg"], "type": err["type"]}
        for err in exc.errors()
    ]


class EmailThresholds(BaseModel):
    """Server-side critical thresholds that gate SMTP alerts, in percent.

    Separate from the browser thresholds in useThresholdsStore, which only drive the
    in-page banner/feed/toasts. Keys are fixed so a typo cannot silently create a
    threshold that is never read.
    """

    # strict=True as well as extra="forbid": without strict, pydantic coerces
    # cpu_crit=true -> 1.0 and cpu_crit="90" -> 90.0. A boolean stored as 1% would
    # re-open the alert flood this change closes. strict still accepts a JSON int
    # (90 -> 90.0), the normal case; it rejects bool and str.
    model_config = ConfigDict(extra="forbid", strict=True)

    cpu_crit: float = Field(90, ge=0, le=100)
    mem_crit: float = Field(90, ge=0, le=100)
    disk_crit: float = Field(95, ge=0, le=100)


class SmtpConfig(BaseModel):
    # strict=True as well as extra="forbid". Without strict, pydantic coerces JSON
    # scalars: clear_password="true" -> True (would delete a stored credential),
    # use_tls="false" -> False (derives security="none" = plaintext, bypassing Task
    # 4's "non-bool legacy flag is ambiguous" contract), port=true -> 1, port="587"
    # -> 587. strict still accepts the normal Task 6 JSON shape — a JSON int for
    # port, real booleans for the flags, strings for security/emails, and a nested
    # object for thresholds — so nothing legitimate breaks.
    model_config = ConfigDict(extra="forbid", strict=True)

    host: str
    port: int = 587
    username: str = ""          # SMTP login identifier — not necessarily an email
    password: str = ""          # "" or MASKED_PASSWORD means "keep the stored one"
    clear_password: bool = False
    from_email: str = ""
    to_email: EmailStr
    security: SecurityMode | None = None
    # Legacy flags, kept so older clients keep working; `security` wins when given.
    use_tls: bool = False
    start_tls: bool = True
    thresholds: EmailThresholds = Field(default_factory=EmailThresholds)

    @field_validator("host")
    @classmethod
    def _strip_host(cls, v: str) -> str:
        return v.strip()

    # The two rejections below are FIELD validators, not a model validator, and that
    # is load-bearing: a model-level raise attaches the whole request object to the
    # error as `input` — password included — which _safe_errors would then have to
    # scrub. Field-scoped, `input` is just the one flag. Both fields are declared
    # after the values they read, so `info.data` is populated.
    @field_validator("clear_password")
    @classmethod
    def _no_clear_with_value(cls, v: bool, info: ValidationInfo) -> bool:
        if v and info.data.get("password"):
            raise ValueError("clear_password cannot be combined with a password value")
        return v

    @field_validator("start_tls")
    @classmethod
    def _tls_flags_exclusive(cls, v: bool, info: ValidationInfo) -> bool:
        # aiosmtplib raises ValueError when both are set, which would surface as a
        # silent non-delivery. Refuse it at the edge — only when no explicit
        # `security` was given, since that field wins over the legacy flags.
        if info.data.get("security") is None and v and info.data.get("use_tls"):
            raise ValueError(
                "use_tls and start_tls cannot both be true — send "
                "security='starttls' | 'implicit_tls' | 'none' instead"
            )
        return v

    @model_validator(mode="after")
    def _normalise(self) -> "SmtpConfig":
        # An explicit security wins; otherwise derive from the (already-validated,
        # not-both-true) legacy flags. Either way the persisted row carries a
        # consistent security + use_tls + start_tls triple.
        if self.security is None:
            self.security = "implicit_tls" if self.use_tls else (
                "starttls" if self.start_tls else "none"
            )
        self.use_tls, self.start_tls = SECURITY_FLAGS[self.security]
        self.from_email = self.from_email.strip()
        if self.from_email:
            _EMAIL.validate_python(self.from_email)
        return self


@router.get("/config")
async def get_config(_: str = Depends(require_admin)):
    config = await get_smtp_config()
    if not config:
        return {"configured": False}
    # Mask the password and drop internal keys (those starting with "_") from the API
    # shape, but report the decrypt failure explicitly so the UI can tell the admin
    # to re-enter it after a master-secret change.
    safe = {k: v for k, v in config.items() if not k.startswith("_")}
    safe["password"] = MASKED_PASSWORD if config.get("password") else ""
    safe["password_decrypt_failed"] = bool(config.get("_decrypt_failed"))

    # Canonicalise the transport for rows written before `security` existed, which
    # carry only use_tls/start_tls. Without this the client sees no `security`, falls
    # back to its own default, and an operator who opens and saves a 465 implicit-TLS
    # config silently converts it to STARTTLS. An ambiguous or corrupt row (both
    # flags true, a non-bool flag, an unsupported value) reports security=None /
    # security_ambiguous=true rather than being guessed, and blocks sending until the
    # operator re-picks.
    mode = security_mode(config)
    safe["security"] = mode
    safe["security_ambiguous"] = mode is None
    if mode is not None:
        safe["use_tls"], safe["start_tls"] = SECURITY_FLAGS[mode]
    return {"configured": True, **safe}


def _scrubbed_422(detail: list[dict]) -> HTTPException:
    return HTTPException(422, detail=detail)


@router.post("/config")
async def set_config(request: Request, _: str = Depends(require_admin)):
    # Parse the body inside the route, not via a typed parameter. A declared body
    # parameter lets FastAPI run its own validation BEFORE the route — and its 422
    # for a parse failure, a top-level non-object or a missing field embeds the raw
    # value as `input` (and `ctx`), which the loc/msg/type contract forbids and which
    # would echo the plaintext password on some paths. Everything below is scrubbed
    # to loc/msg/type; the raw body, `input`, `ctx` and exception text never appear
    # in a response or a log. This is route-local — no global handler is touched, so
    # other APIs keep FastAPI's default behaviour.
    #
    # Order is fixed: auth (dependency) -> parse -> object check -> strict schema ->
    # sender -> bounded DNS/SSRF -> save. DNS is a network call, so it runs only
    # after everything cheap and local has passed.
    try:
        raw = json.loads(await request.body())
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
        raise _scrubbed_422(
            [{"loc": ["body"], "msg": "Request body must be a valid JSON object",
              "type": "json_invalid"}])
    if not isinstance(raw, dict):
        raise _scrubbed_422(
            [{"loc": ["body"], "msg": "Expected a JSON object", "type": "type_error"}])

    try:
        body = SmtpConfig.model_validate(raw)
    except ValidationError as e:
        raise _scrubbed_422(_safe_errors(e))

    config = body.model_dump()
    if not resolve_sender(config):
        raise HTTPException(
            400,
            "A From Email is required (or a username that is itself a valid email address)",
        )

    # Reject SSRF/internal-scan targets before persisting (INJECT-04). Bounded and
    # off-loop: a synchronous getaddrinfo here would block every other request and
    # every agent's ingest, and a hung resolver would hold this POST open. str(e) is
    # a fixed validator message (a host, never a credential).
    try:
        await validate_smtp_target_async(body.host, body.port)
    except ValueError as e:
        raise HTTPException(400, str(e))

    # MASKED_PASSWORD / "" mean "keep existing"; clear_password is the only removal.
    await save_smtp_config(config)
    return {"ok": True}


@router.post("/test")
async def test_email(_: str = Depends(require_admin)):
    # No key — the manual test bypasses the cooldown/backoff entirely.
    result = await send_alert_email(
        "[GlassOps] Test Alert",
        "This is a test email from GlassOps. If you received this, SMTP is configured correctly.",
    )
    if not result.get("ok"):
        raise HTTPException(400, result.get("error", "Send failed"))
    # Acceptance by the relay is not proof of inbox delivery — say so.
    return {"ok": True, "detail": "SMTP server accepted the message"}
