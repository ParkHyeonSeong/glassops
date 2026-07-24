import asyncio
import json
import time

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

import app.database as db
from app.dependencies import get_current_user, require_admin
from app.routers.alerts import router as alerts_router
from app.services import alert_service as svc


def _reset_alert_state():
    reset = getattr(svc, "_reset_alert_state_for_test", None)
    if reset is not None:
        reset()
        return
    svc._last_sent.clear()
    getattr(svc, "_last_attempt", {}).clear()
    svc._invalidate_config_cache()


def valid_body(**overrides) -> dict:
    body = {
        "host": "relay.example.com",
        "port": 587,
        "username": "",
        "password": "",
        "from_email": "alerts@example.com",
        "to_email": "ops@example.com",
        "security": "starttls",
        "thresholds": {"cpu_crit": 90, "mem_crit": 90, "disk_crit": 95},
    }
    body.update(overrides)
    return body


# Mount only the router on a bare app so JWTAuthMiddleware is out of the path and an
# injected admin exercises the route logic (same pattern as test_net_audit_api).
@pytest.fixture
async def client(tmp_path, monkeypatch):
    await db.close_db()
    monkeypatch.setattr(db, "_db_path", str(tmp_path / "t.db"))
    monkeypatch.setattr(db, "_conn", None)
    try:
        await db.init_db()
        _reset_alert_state()
        # The router validates via alert_service.validate_smtp_target_async, which
        # looks up validate_smtp_target as an alert_service module global. Patching
        # that ONE binding covers both POST /config and the send path; otherwise the
        # test resolves relay.example.com for real (NXDOMAIN) and 400s.
        monkeypatch.setattr(svc, "validate_smtp_target", lambda host, port: None)
        test_app = FastAPI()
        test_app.include_router(alerts_router)
        test_app.dependency_overrides[require_admin] = lambda: "admin@example.com"
        transport = ASGITransport(app=test_app)
        async with AsyncClient(transport=transport, base_url="http://t") as c:
            yield c
    finally:
        _reset_alert_state()
        await db.close_db()


async def stored() -> dict:
    conn = await db.get_db()
    cursor = await conn.execute("SELECT config FROM alert_config WHERE id = 1")
    row = await cursor.fetchone()
    return json.loads(row["config"]) if row else {}


async def _write_legacy_row(**flags) -> None:
    """Write a row in the pre-`security` shape, straight to the DB."""
    conn = await db.get_db()
    row = {"host": "relay.example.com", "port": 465, "username": "",
           "from_email": "alerts@example.com", "to_email": "ops@example.com",
           "thresholds": {"cpu_crit": 90, "mem_crit": 90, "disk_crit": 95}, **flags}
    await conn.execute(
        "INSERT OR REPLACE INTO alert_config (id, config) VALUES (1, ?)", (json.dumps(row),))
    await conn.commit()
    svc._invalidate_config_cache()


# --- authorization -------------------------------------------------------

@pytest.mark.parametrize("method,path", [
    ("get", "/api/alerts/config"),
    ("post", "/api/alerts/config"),
    ("post", "/api/alerts/test"),
])
async def test_non_admin_is_refused(tmp_path, monkeypatch, method, path):
    # Override get_current_user (not require_admin) so the REAL admin gate runs
    # against a seeded non-admin user.
    await db.close_db()
    monkeypatch.setattr(db, "_db_path", str(tmp_path / "na.db"))
    monkeypatch.setattr(db, "_conn", None)
    await db.init_db()
    await db.create_user("user@example.com", "hash", role="user", must_change_password=False)
    app = FastAPI()
    app.include_router(alerts_router)
    app.dependency_overrides[get_current_user] = lambda: "user@example.com"
    transport = ASGITransport(app=app)
    try:
        async with AsyncClient(transport=transport, base_url="http://t") as c:
            # httpx's .get() has no json/content/data parameter — only POST carries one.
            kwargs = {"json": valid_body()} if method == "post" else {}
            r = await getattr(c, method)(path, **kwargs)
        assert r.status_code == 403
    finally:
        await db.close_db()


# --- GET shape -----------------------------------------------------------

async def test_get_reports_unconfigured(client):
    r = await client.get("/api/alerts/config")

    assert r.status_code == 200
    assert r.json() == {"configured": False}


async def test_get_masks_the_password(client):
    await client.post("/api/alerts/config", json=valid_body(password="pw-under-test"))

    body = (await client.get("/api/alerts/config")).json()

    assert body["password"] == "********"
    assert "pw-under-test" not in json.dumps(body)
    assert "password_enc" not in body
    assert body["password_decrypt_failed"] is False


async def test_get_reports_a_decrypt_failure(client, monkeypatch):
    await client.post("/api/alerts/config", json=valid_body(password="pw-under-test"))
    svc._invalidate_config_cache()
    monkeypatch.setattr(svc, "_decrypt", lambda value: "")

    body = (await client.get("/api/alerts/config")).json()

    assert body["password_decrypt_failed"] is True
    assert body["password"] == ""


# --- threshold schema ----------------------------------------------------

@pytest.mark.parametrize("thresholds", [
    {"cpu_crit": 101, "mem_crit": 90, "disk_crit": 95},      # above range
    {"cpu_crit": -1, "mem_crit": 90, "disk_crit": 95},       # below range
    {"cpu_crit": 90, "mem_crit": 90, "disk_crit": 95, "gpu_crit": 90},  # extra key
    {"cpu_crit": "high", "mem_crit": 90, "disk_crit": 95},   # non-numeric string
    {"cpu_crit": True, "mem_crit": 90, "disk_crit": 95},     # boolean -> would be 1%
    {"cpu_crit": "90", "mem_crit": 90, "disk_crit": 95},     # numeric string
])
async def test_invalid_thresholds_rejected(client, thresholds):
    r = await client.post("/api/alerts/config", json=valid_body(thresholds=thresholds))

    assert r.status_code == 422
    assert await stored() == {}


async def test_thresholds_round_trip(client):
    await client.post("/api/alerts/config", json=valid_body(
        thresholds={"cpu_crit": 75, "mem_crit": 80, "disk_crit": 85}))

    body = (await client.get("/api/alerts/config")).json()

    # A plain JSON int is the normal case and strict still accepts it (90 -> 90.0).
    assert body["thresholds"] == {"cpu_crit": 75, "mem_crit": 80, "disk_crit": 85}


# --- TLS mode ------------------------------------------------------------

async def test_both_tls_flags_true_is_rejected(client):
    body = valid_body()
    del body["security"]
    body.update(use_tls=True, start_tls=True)

    r = await client.post("/api/alerts/config", json=body)

    assert r.status_code == 422
    assert await stored() == {}


@pytest.mark.parametrize("mode,use_tls,start_tls", [
    ("starttls", False, True),
    ("implicit_tls", True, False),
    ("none", False, False),
])
async def test_security_mode_persists_exclusive_flags(client, mode, use_tls, start_tls):
    await client.post("/api/alerts/config", json=valid_body(security=mode))

    row = await stored()

    assert row["security"] == mode
    assert (row["use_tls"], row["start_tls"]) == (use_tls, start_tls)


async def test_legacy_flags_derive_a_security_mode(client):
    body = valid_body()
    del body["security"]
    body.update(use_tls=True, start_tls=False)

    await client.post("/api/alerts/config", json=body)

    assert (await stored())["security"] == "implicit_tls"


@pytest.mark.parametrize("use_tls,start_tls,expected", [
    (False, True, "starttls"),
    (True, False, "implicit_tls"),
    (False, False, "none"),
])
async def test_get_canonicalises_a_legacy_row(client, use_tls, start_tls, expected):
    """Without this, the UI sees no `security`, falls back to its own default, and an
    operator who opens and saves a 465 config silently converts it to STARTTLS."""
    await _write_legacy_row(use_tls=use_tls, start_tls=start_tls)

    body = (await client.get("/api/alerts/config")).json()

    assert body["security"] == expected
    assert (body["use_tls"], body["start_tls"]) == (use_tls, start_tls)
    assert body["security_ambiguous"] is False


async def test_get_reports_an_ambiguous_legacy_row(client):
    # Both flags true is a state aiosmtplib refuses. Guessing a mode would silently
    # up- or downgrade the operator's transport.
    await _write_legacy_row(use_tls=True, start_tls=True)

    body = (await client.get("/api/alerts/config")).json()

    assert body["security"] is None
    assert body["security_ambiguous"] is True


async def test_ambiguous_legacy_row_blocks_sending(client, monkeypatch):
    await _write_legacy_row(use_tls=True, start_tls=True)
    sent = []
    monkeypatch.setattr(svc.aiosmtplib, "send", lambda *a, **k: sent.append(k))

    r = await client.post("/api/alerts/test")

    assert r.status_code == 400
    assert "ambiguous" in r.json()["detail"].lower()
    assert sent == []


async def test_unknown_security_mode_rejected(client):
    r = await client.post("/api/alerts/config", json=valid_body(security="ssl"))

    assert r.status_code == 422
    assert await stored() == {}


# --- address validation --------------------------------------------------

async def test_invalid_to_email_rejected(client):
    r = await client.post("/api/alerts/config", json=valid_body(to_email="not-an-address"))

    assert r.status_code == 422
    assert await stored() == {}


async def test_invalid_from_email_rejected(client):
    r = await client.post("/api/alerts/config", json=valid_body(from_email="not-an-address"))

    assert r.status_code == 422
    assert await stored() == {}


async def test_missing_sender_rejected_with_a_clear_message(client):
    r = await client.post("/api/alerts/config",
                          json=valid_body(from_email="", username="relay-login"))

    assert r.status_code == 400
    assert "From Email" in r.json()["detail"]
    assert await stored() == {}


async def test_email_username_satisfies_the_sender_requirement(client):
    r = await client.post("/api/alerts/config",
                          json=valid_body(from_email="", username="relay@example.com"))

    assert r.status_code == 200


# --- host / port ---------------------------------------------------------

async def test_host_whitespace_is_normalised_before_storage(client):
    await client.post("/api/alerts/config", json=valid_body(host="  relay.example.com  "))

    assert (await stored())["host"] == "relay.example.com"


async def test_disallowed_target_leaves_the_existing_row_intact(client, monkeypatch):
    await client.post("/api/alerts/config", json=valid_body(to_email="ops@example.com"))
    before = await stored()

    def reject(host, port):
        raise ValueError("SMTP port must be one of [25, 465, 587, 2525]")
    # validate_smtp_target_async wraps this alert_service global.
    monkeypatch.setattr(svc, "validate_smtp_target", reject)

    r = await client.post("/api/alerts/config",
                          json=valid_body(port=8025, to_email="attacker@example.com"))

    assert r.status_code == 400
    assert await stored() == before


async def test_config_save_dns_check_is_bounded(client, monkeypatch):
    # A hung resolver must not hold the POST open.
    monkeypatch.setattr(svc, "DNS_TIMEOUT", 0.1)

    def hang(host, port):
        time.sleep(2)
    monkeypatch.setattr(svc, "validate_smtp_target", hang)

    r = await asyncio.wait_for(
        client.post("/api/alerts/config", json=valid_body()), timeout=3)

    assert r.status_code == 400
    assert "timed out" in r.json()["detail"]
    assert await stored() == {}


# --- password contract ---------------------------------------------------

async def test_masked_password_round_trip_preserves_the_credential(client):
    await client.post("/api/alerts/config", json=valid_body(password="pw-under-test"))
    loaded = (await client.get("/api/alerts/config")).json()

    # Exactly what the UI does: post the loaded config straight back.
    r = await client.post("/api/alerts/config", json=valid_body(
        password=loaded["password"], security=loaded["security"]))

    assert r.status_code == 200
    svc._invalidate_config_cache()
    assert (await svc.get_smtp_config())["password"] == "pw-under-test"


async def test_clear_password_removes_the_credential(client):
    await client.post("/api/alerts/config", json=valid_body(password="pw-under-test"))

    r = await client.post("/api/alerts/config",
                          json=valid_body(password="", clear_password=True))

    assert r.status_code == 200
    assert "password_enc" not in await stored()


async def test_clear_password_conflicting_with_a_value_is_rejected(client):
    r = await client.post("/api/alerts/config",
                          json=valid_body(password="pw-new", clear_password=True))

    assert r.status_code == 422


# --- 422 never echoes secrets --------------------------------------------

def _drop(field: str) -> dict:
    return {k: v for k, v in valid_body(password="pw-under-test").items() if k != field}


@pytest.mark.parametrize("body", [
    _drop("to_email"),                                          # pydantic "missing"
    _drop("host"),                                              # pydantic "missing"
    valid_body(password="pw-under-test", port="not-a-port"),    # pydantic type error
    valid_body(password="pw-under-test", clear_password=True),  # our field validator
    {**valid_body(password="pw-under-test"), "security": None,
     "use_tls": True, "start_tls": True},                       # our field validator
    valid_body(password="pw-under-test", to_email="nope"),
    valid_body(password="pw-under-test",
               thresholds={"cpu_crit": 101, "mem_crit": 90, "disk_crit": 95}),
    {**valid_body(password="pw-under-test"), "password_enc": "injected"},  # extra=forbid
])
async def test_validation_errors_never_echo_the_password(client, body):
    """Pydantic attaches the offending value to every error as `input`, and for the
    errors it raises itself that value is the WHOLE request body. Only loc/msg/type
    may reach the client."""
    r = await client.post("/api/alerts/config", json=body)

    assert r.status_code == 422
    assert "pw-under-test" not in r.text
    for err in r.json()["detail"]:
        assert set(err) == {"loc", "msg", "type"}


@pytest.mark.parametrize("body", [
    [{"password": "pw-under-test"}],          # top-level JSON array
    "password=pw-under-test",                 # top-level JSON string
    42,                                       # top-level JSON number
])
async def test_non_object_bodies_are_rejected_without_echoing_them(client, body):
    """A declared `dict` parameter would make FastAPI validate the top-level type
    BEFORE the route runs, and its automatic 422 embeds the whole body."""
    r = await client.post("/api/alerts/config", json=body)

    assert r.status_code == 422
    assert "pw-under-test" not in r.text
    assert await stored() == {}


async def test_malformed_json_is_rejected_without_echoing_it(client):
    r = await client.post(
        "/api/alerts/config",
        content=b'{"password": "pw-under-test", ',
        headers={"Content-Type": "application/json"},
    )

    assert r.status_code in (400, 422)
    assert "pw-under-test" not in r.text


async def test_injected_password_enc_is_rejected_not_ignored(client):
    r = await client.post("/api/alerts/config",
                          json={**valid_body(), "password_enc": "injected"})

    assert r.status_code == 422
    assert await stored() == {}


# --- /test ---------------------------------------------------------------

async def test_test_endpoint_reports_smtp_acceptance(client, monkeypatch):
    await client.post("/api/alerts/config", json=valid_body())
    sent: list[tuple] = []

    async def fake_send(subject, body, key=None):
        sent.append((subject, body, key))
        return {"ok": True}
    monkeypatch.setattr("app.routers.alerts.send_alert_email", fake_send)

    r = await client.post("/api/alerts/test")

    assert r.status_code == 200
    assert r.json() == {"ok": True, "detail": "SMTP server accepted the message"}
    assert sent[0][2] is None  # the manual test bypasses the cooldown


async def test_test_endpoint_surfaces_the_failure_detail(client, monkeypatch):
    async def fake_send(subject, body, key=None):
        return {"ok": False, "error": "Connection refused"}
    monkeypatch.setattr("app.routers.alerts.send_alert_email", fake_send)

    r = await client.post("/api/alerts/test")

    assert r.status_code == 400
    assert r.json()["detail"] == "Connection refused"
