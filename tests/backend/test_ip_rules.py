"""Explicit confirmation is the only path from an IP draft to nginx policy."""
import asyncio
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

import app.database as db
import app.routers.settings as routes
import app.services.supervisor_service as supervisor
from app.dependencies import require_admin


@pytest.fixture
async def client(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "_db_path", str(tmp_path / "ip.db"))
    await db.init_db()
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[require_admin] = lambda: "admin@example.com"
    async with AsyncClient(transport=ASGITransport(app=app, client=("10.2.3.4", 1234)), base_url="http://test") as client:
        yield client
    await db.close_db()


@pytest.fixture
def nginx(monkeypatch):
    state = {"active": "10.0.0.0/8", "ready": True, "restarts": []}
    async def restart(service):
        state["restarts"].append(service)
        value = await db.get_confirmed_ip_rules()
        state["active"] = value if value is not None else "10.0.0.0/8"
        return {"ok": True}
    monkeypatch.setenv("GLASSOPS_ALLOWED_IPS", "10.0.0.0/8")
    monkeypatch.setattr(supervisor, "restart_service", restart)
    monkeypatch.setattr(routes, "restart_service", restart)
    monkeypatch.setattr(supervisor, "get_service_status", AsyncMock(return_value={"ok": True, "services": {"nginx": "RUNNING"}}))
    monkeypatch.setattr(supervisor, "nginx_ready", AsyncMock(side_effect=lambda: state["ready"]), raising=False)
    monkeypatch.setattr(supervisor, "read_active_ip_rules", lambda: state["active"], raising=False)
    return state


async def test_save_and_plain_restart_never_confirm_legacy_rules(client, nginx):
    response = await client.post("/api/settings/runtime", json={"allowed_ips": "10.2.3.4"})
    assert response.status_code == 200
    assert await db.get_confirmed_ip_rules() is None
    for service in ("agent", "nginx", "all"):
        assert (await client.post("/api/settings/restart", json={"service": service})).status_code == 200
    assert await db.get_confirmed_ip_rules() is None
    status = (await client.get("/api/settings/runtime")).json()
    assert status["ip_rules"] == {
        "active": "10.0.0.0/8", "pending": True, "has_saved_draft": True,
        "client_ip": "10.2.3.4", "nginx_ready": True,
    }


async def test_only_explicit_apply_promotes_saved_value_and_reports_real_policy(client, nginx):
    await client.post("/api/settings/runtime", json={"allowed_ips": "10.2.3.4, 10.2.3.4/32"})
    response = await client.post("/api/settings/ip-rules/apply", json={"allowed_ips": "10.2.3.4, 10.2.3.4/32"})
    assert response.status_code == 200
    assert await db.get_confirmed_ip_rules() == "10.2.3.4/32"
    assert nginx["restarts"] == ["nginx"]
    status = (await client.get("/api/settings/runtime")).json()
    assert status["ip_rules"]["pending"] is False
    assert status["ip_rules"]["active"] == "10.2.3.4/32"
    assert "allowed_ips_confirmed" not in status["config"]


@pytest.mark.parametrize("draft,submitted,code", [(None, "", 409), ("", "10.2.3.4", 409), ("192.168.0.0/16", "192.168.0.0/16", 400)])
async def test_unconfirmed_or_self_blocking_drafts_are_rejected(client, nginx, draft, submitted, code):
    if draft is not None:
        await db.set_runtime_config("allowed_ips", draft)
    assert (await client.post("/api/settings/ip-rules/apply", json={"allowed_ips": submitted})).status_code == code
    assert await db.get_confirmed_ip_rules() is None
    assert nginx["restarts"] == []


async def test_unknown_client_ip_cannot_apply_even_allow_all(client, nginx, monkeypatch):
    await db.set_runtime_config("allowed_ips", "")
    monkeypatch.setattr(routes, "resolve_client_ip", lambda _: "unknown")
    assert (await client.get("/api/settings/runtime")).json()["ip_rules"]["client_ip"] is None
    assert (await client.post("/api/settings/ip-rules/apply", json={"allowed_ips": ""})).status_code == 400
    assert await db.get_confirmed_ip_rules() is None


async def test_generic_save_cannot_set_internal_confirmation(client, nginx):
    await client.post("/api/settings/runtime", json={"allowed_ips_confirmed": ""})
    await db.set_runtime_config("allowed_ips_confirmed", "")
    assert await db.get_confirmed_ip_rules() is None
    status = (await client.get("/api/settings/runtime")).json()["ip_rules"]
    assert status["has_saved_draft"] is False
    assert status["pending"] is False


async def test_explicit_empty_allows_all(client, nginx):
    await db.set_runtime_config("allowed_ips", "")
    assert (await client.post("/api/settings/ip-rules/apply", json={"allowed_ips": ""})).status_code == 200
    assert await db.get_confirmed_ip_rules() == ""
    assert (await client.get("/api/settings/runtime")).json()["ip_rules"]["active"] == ""


@pytest.mark.parametrize("previous", [None, "10.0.0.0/8"])
async def test_definite_failure_restores_previous_confirmation_once(client, nginx, monkeypatch, previous):
    await db.set_confirmed_ip_rules(previous)
    await db.set_runtime_config("allowed_ips", "10.2.3.4")
    restarts = AsyncMock(side_effect=[{"ok": False, "error": "failed"}, {"ok": True}])
    monkeypatch.setattr(supervisor, "restart_service", restarts)
    response = await client.post("/api/settings/ip-rules/apply", json={"allowed_ips": "10.2.3.4"})
    assert response.status_code == 500
    assert response.json()["detail"]["outcome"] == "restored"
    assert await db.get_confirmed_ip_rules() == previous
    assert restarts.await_count == 2


async def test_timeout_is_unknown_without_rollback_or_second_restart(client, nginx, monkeypatch):
    await db.set_runtime_config("allowed_ips", "10.2.3.4")
    restart = AsyncMock(return_value={"ok": False, "timed_out": True, "error": "timed out"})
    monkeypatch.setattr(supervisor, "restart_service", restart)
    response = await client.post("/api/settings/ip-rules/apply", json={"allowed_ips": "10.2.3.4"})
    assert response.status_code == 202
    assert response.json()["outcome"] == "unknown"
    assert await db.get_confirmed_ip_rules() == "10.2.3.4/32"
    assert restart.await_count == 1


async def test_running_wrapper_without_nginx_response_is_not_success(client, nginx):
    nginx["ready"] = False
    await db.set_runtime_config("allowed_ips", "10.2.3.4")
    response = await client.post("/api/settings/ip-rules/apply", json={"allowed_ips": "10.2.3.4"})
    assert response.status_code == 202
    status = (await client.get("/api/settings/runtime")).json()["ip_rules"]
    assert status["active"] is None
    assert status["nginx_ready"] is False


async def test_database_write_failure_does_not_restart_nginx(client, nginx, monkeypatch):
    await db.set_runtime_config("allowed_ips", "10.2.3.4")
    monkeypatch.setattr(db, "set_confirmed_ip_rules", AsyncMock(side_effect=RuntimeError("db unavailable")))
    with pytest.raises(RuntimeError, match="db unavailable"):
        await client.post("/api/settings/ip-rules/apply", json={"allowed_ips": "10.2.3.4"})
    assert nginx["restarts"] == []


async def test_restart_timeout_is_marked_and_client_process_reaped(monkeypatch):
    proc = AsyncMock()
    proc.kill = lambda: None
    proc.communicate.side_effect = [asyncio.TimeoutError, (b"", b"")]
    monkeypatch.setattr(supervisor.asyncio, "create_subprocess_exec", AsyncMock(return_value=proc))
    result = await supervisor.restart_service("nginx")
    assert result.get("timed_out") is True
    assert proc.communicate.await_count == 2
