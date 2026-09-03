"""Readiness must reveal a stopped database; liveness may not.

A fail-stopped backend still answers /health with 200 and still broadcasts
ephemeral samples over the websocket, so nothing an operator or an orchestrator
looks at says "this process stopped storing data". /ready is that signal.
"""

import asyncio

import pytest
from httpx import ASGITransport, AsyncClient

import app.database as db
from app.main import app


@pytest.fixture
async def client(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "_db_path", str(tmp_path / "ready.db"))
    monkeypatch.setattr(db, "_conn", None)
    monkeypatch.setattr(db, "_metric_conn", None, raising=False)
    monkeypatch.setattr(db, "_op_lock", asyncio.Lock(), raising=False)
    monkeypatch.setattr(db, "_fail_stop", None, raising=False)
    await db.init_db()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    if not db._closed:
        await db.close_db()
    db._closed = False


async def test_ready_is_200_when_the_database_is_healthy(client):
    resp = await client.get("/ready")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ready"
    assert body["database"] == "ok"


async def test_health_stays_a_liveness_probe(client):
    # /health is deliberately unchanged: it answers as long as the process is
    # alive. Changing it would turn a storage fault into a container restart
    # loop, which is a deployment decision, not this slice's.
    db._fail_stop = "test: storage stopped"
    resp = await client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


async def test_ready_is_503_when_fail_stopped(client):
    db._fail_stop = "database is restart-required: rollback outcome unknown"
    resp = await client.get("/ready")
    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "not_ready"
    assert body["database"] == "fail_stop"
    assert body["restart_required"] is True


async def test_ready_never_serves_the_reason_text(client, caplog):
    """The latch is public; the sentence explaining it is not.

    /ready is outside the /api/ auth gate and is proxied at the edge, and the
    reason is built from repr() of driver exceptions — it carries table and
    column names and statement context. An operator still needs it, so it goes
    to the log instead of the response."""
    secret = "no such column: users.totp_secret"
    db._fail_stop = f"database is restart-required: OperationalError('{secret}')"
    with caplog.at_level("ERROR", logger="glassops"):
        resp = await client.get("/ready")

    assert resp.status_code == 503
    body = resp.json()
    assert "reason" not in body, "the free-text reason was served to an unauthenticated caller"
    assert secret not in resp.text
    # Still actionable without it.
    assert body["database"] == "fail_stop"
    assert body["restart_required"] is True
    # And not lost: the operator can still find out why.
    assert any(secret in r.getMessage() for r in caplog.records), (
        "the reason was withheld from the response AND never logged")


async def test_ready_is_503_while_closing(client):
    db._closing = True
    try:
        resp = await client.get("/ready")
        assert resp.status_code == 503
        assert resp.json()["database"] == "closing"
    finally:
        db._closing = False


async def test_ready_is_503_after_close(client):
    db._closed = True
    try:
        resp = await client.get("/ready")
        assert resp.status_code == 503
        assert resp.json()["database"] == "closed"
    finally:
        db._closed = False


async def test_ready_reports_unresolved_workers(client):
    async def never():
        await asyncio.Event().wait()

    task = asyncio.ensure_future(never())
    db._unresolved.append(db._Unresolved("commit", task, db._Disposal.NONE))
    try:
        resp = await client.get("/ready")
        assert resp.status_code == 503
        body = resp.json()
        assert body["unresolved_workers"] == 1
        assert body["restart_required"] is True
    finally:
        db._unresolved.clear()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_ready_reports_unclosed_connections(client):
    db._unclosed.append(object())
    try:
        resp = await client.get("/ready")
        assert resp.status_code == 503
        assert resp.json()["unclosed_connections"] == 1
    finally:
        db._unclosed.clear()


async def test_ready_does_not_touch_the_database(client):
    # The probe must not itself submit work to a connection that may be wedged;
    # otherwise the readiness check hangs exactly when it is needed.
    conn = await db._get_conn()
    submitted = []
    real_execute = conn.execute

    async def counting(sql, *a, **k):
        submitted.append(sql)
        return await real_execute(sql, *a, **k)

    conn.execute = counting
    try:
        resp = await client.get("/ready")
        assert resp.status_code == 200
        assert submitted == [], f"/ready ran SQL: {submitted}"
    finally:
        del conn.execute
