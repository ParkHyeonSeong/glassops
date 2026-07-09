import time

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
import app.database as db
from app.routers.net_audit import router as net_audit_router
from app.dependencies import require_admin, get_current_user


# Positive tests mount ONLY the router on a bare app so the JWTAuthMiddleware
# (which 401s any tokenless /api request regardless of a require_admin override)
# is out of the path — we exercise the route logic with an injected admin.
@pytest.fixture
async def client(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "_db_path", str(tmp_path / "t.db"))
    monkeypatch.setattr(db, "_conn", None)
    await db.init_db()
    test_app = FastAPI()
    test_app.include_router(net_audit_router)
    test_app.dependency_overrides[require_admin] = lambda: "admin@glassops.local"
    transport = ASGITransport(app=test_app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        yield c
    await db.close_db()


async def test_events_endpoint_returns_stored(client):
    await db.store_net_audit("a1", 1000.0, [{
        "event": "open", "ts": 1000.0, "proto": "tcp", "laddr": "10.0.0.9",
        "lport": 5500, "raddr": "8.8.8.8", "rport": 443, "status": "ESTABLISHED",
        "pid": 7, "pname": "curl", "duration": None}], [])
    r = await client.get("/api/net-audit/a1/events")
    assert r.status_code == 200
    body = r.json()
    assert body["agent_id"] == "a1"
    assert body["events"][0]["raddr"] == "8.8.8.8"


async def test_events_filter_by_raddr(client):
    await db.store_net_audit("a1", 1000.0, [
        {"event": "open", "ts": 1000.0, "proto": "tcp", "laddr": "x", "lport": 1,
         "raddr": "8.8.8.8", "rport": 443, "status": "E", "pid": 1, "pname": "a", "duration": None},
        {"event": "open", "ts": 1001.0, "proto": "tcp", "laddr": "x", "lport": 2,
         "raddr": "1.1.1.1", "rport": 443, "status": "E", "pid": 2, "pname": "b", "duration": None},
    ], [])
    r = await client.get("/api/net-audit/a1/events", params={"raddr": "1.1.1.1"})
    assert len(r.json()["events"]) == 1


async def test_rollup_endpoint(client):
    # ts must fall inside the router's "last 24h" wall-clock window (it computes
    # now - seconds .. now via time.time(), matching the metrics_range convention
    # in main.py) — a fixed epoch-adjacent literal would never match.
    recent = time.time() - 60
    await db.store_net_audit("a1", 100.0, [], [{
        "ts": recent, "interfaces": [{"name": "eth0", "bytes_in": 1, "bytes_out": 2}],
        "top_talkers": []}])
    r = await client.get("/api/net-audit/a1/rollup", params={"duration": "24h"})
    assert r.status_code == 200
    assert r.json()["rollups"][0]["interfaces"][0]["name"] == "eth0"


async def test_invalid_agent_id_rejected(client):
    # Path pattern (mirrors _AGENT_ID on the metrics routes) rejects malformed ids
    # at the routing layer -> 422, before the handler/DB (review P2).
    r = await client.get("/api/net-audit/bad$id!/events")
    assert r.status_code == 422


async def test_out_of_range_limit_rejected(client):
    # Review P2: Query(ge=1, le=1000) rejects a negative or oversized limit at the
    # boundary (no unbounded LIMIT -1, no 7-day dump in one request).
    assert (await client.get("/api/net-audit/a1/events", params={"limit": -1})).status_code == 422
    assert (await client.get("/api/net-audit/a1/events", params={"limit": 5000})).status_code == 422


async def test_non_admin_gets_403(tmp_path, monkeypatch):
    # Override get_current_user (not require_admin) so the REAL admin gate runs:
    # a seeded non-admin user must get 403 (review note).
    monkeypatch.setattr(db, "_db_path", str(tmp_path / "na.db"))
    monkeypatch.setattr(db, "_conn", None)
    await db.init_db()
    await db.create_user("user@x", "hash", role="user", must_change_password=False)
    non_admin_app = FastAPI()
    non_admin_app.include_router(net_audit_router)
    non_admin_app.dependency_overrides[get_current_user] = lambda: "user@x"
    transport = ASGITransport(app=non_admin_app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        r = await c.get("/api/net-audit/a1/events")
    assert r.status_code == 403
    await db.close_db()


async def test_tokenless_request_is_rejected_on_real_app():
    # The real app (with JWTAuthMiddleware) must reject an unauthenticated
    # request BEFORE the route — proving net-audit is not an open IDOR like the
    # metrics endpoints. No DB / token needed: the middleware rejects first.
    from app.main import app as real_app
    transport = ASGITransport(app=real_app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        r = await c.get("/api/net-audit/a1/events")
    assert r.status_code in (401, 403)
