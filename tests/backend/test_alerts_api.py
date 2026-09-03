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
    db._closed = False  # close_db latches "closed" for the process; a test reopens a fresh DB
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
        db._closed = False  # close_db latches "closed" for the process; a test reopens a fresh DB


async def stored() -> dict:
    conn = await db._get_conn()
    cursor = await conn.execute("SELECT config FROM alert_config WHERE id = 1")
    row = await cursor.fetchone()
    return json.loads(row["config"]) if row else {}


async def _write_legacy_row(**flags) -> None:
    """Write a row in the pre-`security` shape, straight to the DB."""
    conn = await db._get_conn()
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
    db._closed = False  # close_db latches "closed" for the process; a test reopens a fresh DB
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
        db._closed = False  # close_db latches "closed" for the process; a test reopens a fresh DB


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

    # Contract under test: a VALID request DTO carrying the mask back keeps the
    # stored credential. This is NOT a full GET->POST round-trip: the GET response
    # also carries response-only fields (configured, password_decrypt_failed,
    # security_ambiguous) that extra="forbid" rejects, so a UI that reposts the raw
    # GET body 422s. Projecting the GET into a request DTO is Task 6's job; see the
    # final report's note that stock UI save is broken while Task 5 stands alone.
    assert loaded["password"] == "********"
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


# --- strict typing: no JSON coercion -------------------------------------

@pytest.mark.parametrize("overrides", [
    {"clear_password": "true"},              # -> True would delete the stored password
    {"clear_password": "false"},
    {"clear_password": 0},
    {"clear_password": 1},
    {"use_tls": "true", "security": None},
    {"use_tls": "false", "security": None},
    {"start_tls": "false", "security": None},
    {"use_tls": 0, "start_tls": 1, "security": None},
    {"port": "587"},
    {"port": True},
    {"port": 587.0},
])
async def test_coerced_scalar_types_are_rejected(client, overrides):
    """extra='forbid' alone still lets pydantic coerce '"true"' -> True, '"587"' ->
    587 and true -> 1. clear_password='true' would delete a stored credential, and a
    boolean port silently becomes 1."""
    r = await client.post("/api/alerts/config", json=valid_body(**overrides))

    assert r.status_code == 422
    assert await stored() == {}


async def test_string_legacy_flags_do_not_normalise_to_plaintext(client):
    # The sharpest case: security=null with stringy 'false' flags coerced to False
    # would derive security='none' — plaintext SMTP — bypassing Task 4's
    # "non-bool legacy flag is ambiguous" contract at the schema edge.
    body = valid_body()
    body.update(security=None, use_tls="false", start_tls="false")

    r = await client.post("/api/alerts/config", json=body)

    assert r.status_code == 422
    assert await stored() == {}


async def test_valid_typed_payload_is_still_accepted(client):
    # Guards that strict does not break the normal Task 6 JSON shape: int port, real
    # booleans, string security, int thresholds.
    r = await client.post("/api/alerts/config", json=valid_body(
        port=465, security="implicit_tls", use_tls=True, start_tls=False,
        thresholds={"cpu_crit": 88, "mem_crit": 70, "disk_crit": 60}))

    assert r.status_code == 200
    row = await stored()
    assert row["port"] == 465
    assert row["thresholds"] == {"cpu_crit": 88, "mem_crit": 70, "disk_crit": 60}


# --- every error path is a scrubbed 422 ----------------------------------

@pytest.fixture
def no_dns(monkeypatch):
    """Spy that FAILS if the DNS/SSRF validator is called at all. Schema and parser
    errors must be refused before it — moving DNS validation earlier is otherwise
    invisible, because the client fixture makes the real validator always succeed."""
    called = {"n": 0}

    async def spy(host, port):
        called["n"] += 1
        raise AssertionError("validate_smtp_target_async called before schema validation")

    monkeypatch.setattr("app.routers.alerts.validate_smtp_target_async", spy)
    return called


@pytest.mark.parametrize("kw", [
    {"json": None},                                                    # JSON null
    {"content": b""},                                                  # empty body
    {"content": b'{"password": "pw-under-test", ',                     # malformed JSON
     "headers": {"Content-Type": "application/json"}},
    {"content": b'\xff\xfe{"password": "pw-under-test"}',              # invalid UTF-8
     "headers": {"Content-Type": "application/json"}},
    {"json": [{"password": "pw-under-test"}]},                         # top-level array
    {"json": "password=pw-under-test"},                               # top-level string
    {"json": 42},                                                     # top-level number
    {"json": _drop("to_email")},                                      # missing required
    {"json": valid_body(password="pw-under-test", port="not-a-port")},  # field type error
    {"json": {**valid_body(password="pw-under-test"), "password_enc": "x"}},  # extra field
])
async def test_every_error_response_is_a_scrubbed_422(client, no_dns, kw):
    r = await client.post("/api/alerts/config", **kw)

    assert r.status_code == 422
    assert "pw-under-test" not in r.text
    detail = r.json()["detail"]
    assert isinstance(detail, list) and detail
    for entry in detail:
        assert set(entry) == {"loc", "msg", "type"}
    assert await stored() == {}
    assert no_dns["n"] == 0


# --- ordering: DNS runs only after schema + sender validation ------------

@pytest.mark.parametrize("bad", [
    {"thresholds": {"cpu_crit": 999, "mem_crit": 90, "disk_crit": 95}},  # schema
    {"to_email": "not-an-address"},                                      # schema
    {"password_enc": "injected"},                                        # extra=forbid
    {"port": "587"},                                                     # coercion
    {"from_email": "", "username": "relay-login"},                       # sender gate
])
async def test_dns_validator_is_not_called_for_invalid_requests(client, no_dns, bad):
    r = await client.post("/api/alerts/config", json=valid_body(**bad))

    assert r.status_code in (400, 422)
    assert no_dns["n"] == 0
    assert await stored() == {}


async def test_dns_validator_runs_for_a_valid_request(client, monkeypatch):
    seen = {"host": None, "port": None}

    async def spy(host, port):
        seen["host"], seen["port"] = host, port

    monkeypatch.setattr("app.routers.alerts.validate_smtp_target_async", spy)

    r = await client.post("/api/alerts/config", json=valid_body(host="  relay.example.com  "))

    assert r.status_code == 200
    # It ran, and after the host was stripped by the model.
    assert seen == {"host": "relay.example.com", "port": 587}


# --- completeness: no error path escapes the scrubbed 422 ----------------

async def test_deeply_nested_json_is_a_scrubbed_422_not_a_500(client, no_dns):
    """json.loads raises RecursionError (not a ValueError) on pathologically nested
    input, which would otherwise 500 — the one error path that escaped the fixed
    loc/msg/type contract."""
    payload = ("[" * 20000 + "]" * 20000).encode()

    r = await client.post("/api/alerts/config", content=payload,
                          headers={"Content-Type": "application/json"})

    assert r.status_code == 422
    # Full-body equality, not just the key set: an exception string leaking into
    # `msg` would pass a keys-only check but is exactly what must not happen.
    assert r.json() == {"detail": [{
        "loc": ["body"],
        "msg": "Request body must be a valid JSON object",
        "type": "json_invalid",
    }]}
    assert no_dns["n"] == 0
    assert await stored() == {}


def _resolve_ref(ref: str, doc: dict):
    """Follow a local JSON Pointer ('#/a/b/c') from the document root, or raise."""
    assert ref.startswith("#/"), f"non-local $ref: {ref}"
    node = doc
    for part in ref[2:].split("/"):
        part = part.replace("~1", "/").replace("~0", "~")
        node = node[part]   # KeyError here == a dangling $ref
    return node


def test_post_config_request_body_schema_is_fully_resolvable():
    """Manual Request parsing drops FastAPI's inferred requestBody. It is re-attached
    from SmtpConfig, but model_json_schema() emits '#/$defs/...' refs that do not
    exist at the OpenAPI document root — so every local $ref in the request schema
    must resolve from the root, recursively, or Swagger/codegen breaks on
    thresholds."""
    from fastapi import FastAPI
    app = FastAPI()
    app.include_router(alerts_router)
    doc = app.openapi()

    post = doc["paths"]["/api/alerts/config"]["post"]
    assert "requestBody" in post
    schema = post["requestBody"]["content"]["application/json"]["schema"]

    # Walk the whole schema; every local $ref must resolve from the document root.
    resolved_refs = []

    def walk(node):
        if isinstance(node, dict):
            if "$ref" in node:
                resolved_refs.append(node["$ref"])
                walk(_resolve_ref(node["$ref"], doc))   # raises KeyError if dangling
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(schema)

    # The body must reach both a top-level field and the nested threshold fields —
    # the nested ones only exist if the EmailThresholds reference actually resolved.
    def properties(node):
        if isinstance(node, dict):
            if "properties" in node:
                yield from node["properties"]
            if "$ref" in node:
                yield from properties(_resolve_ref(node["$ref"], doc))
            for v in node.values():
                yield from properties(v)
        elif isinstance(node, list):
            for v in node:
                yield from properties(v)

    names = set(properties(schema))
    assert {"host", "to_email", "cpu_crit", "disk_crit"} <= names
