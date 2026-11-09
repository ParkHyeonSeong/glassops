"""Save -> persisted configuration -> actual next built-in snapshot."""
import importlib

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

import app.database as db
import app.routers.settings as routes
import agent.main as agent
from app.dependencies import require_admin


@pytest.fixture
async def client(tmp_path, monkeypatch):
    path = str(tmp_path / "settings.db")
    monkeypatch.setattr(db, "_db_path", path)
    monkeypatch.setenv("GLASSOPS_DB_PATH", path)
    await db.init_db()
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[require_admin] = lambda: "admin@example.com"
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client
    await db.close_db()


async def test_save_switches_real_docker_collection_and_remote_keeps_env(client, monkeypatch):
    monkeypatch.setattr(agent, "ENABLE_GPU", False)
    monkeypatch.setattr(agent, "ENABLE_DOCKER", True)
    monkeypatch.setattr(agent, "ENABLE_NET_AUDIT", False)
    monkeypatch.setattr(agent, "collect_system", lambda: {})
    monkeypatch.setattr(agent, "collect_network", lambda: {})
    monkeypatch.setattr(agent, "collect_processes", lambda: [])
    monkeypatch.setattr(agent.pm2, "collect", lambda: [])
    def forbidden(*_):
        raise AssertionError("disabled Docker collector ran")
    monkeypatch.setattr(agent, "collect_containers", forbidden)
    assert (await client.post("/api/settings/runtime", json={"enable_docker": "false"})).status_code == 200
    assert "containers" not in await agent.collect_metrics()
    monkeypatch.setattr(agent, "collect_containers", lambda *_: [{"id": "running-container"}])
    assert (await client.post("/api/settings/runtime", json={"enable_docker": "true"})).status_code == 200
    assert (await agent.collect_metrics())["containers"] == [{"id": "running-container"}]
    await client.post("/api/settings/runtime", json={"enable_docker": "false"})
    monkeypatch.delenv("GLASSOPS_DB_PATH")
    assert (await agent.collect_metrics())["containers"] == [{"id": "running-container"}]


@pytest.mark.parametrize("docker,interval,want_docker,want_interval", [
    (None, "1", "false", "1"), ("TRUE", "0", "true", "1"), ("", "90", "false", "90"),
])
async def test_displayed_environment_defaults_match_agent(client, monkeypatch, docker, interval, want_docker, want_interval):
    original = routes.DEFAULTS
    if docker is None:
        monkeypatch.delenv("GLASSOPS_ENABLE_DOCKER", raising=False)
    else:
        monkeypatch.setenv("GLASSOPS_ENABLE_DOCKER", docker)
    monkeypatch.setenv("GLASSOPS_COLLECT_INTERVAL", interval)
    try:
        importlib.reload(routes)
        result = (await client.get("/api/settings/runtime")).json()["config"]
        assert result["enable_docker"] == want_docker
        assert result["collect_interval"] == want_interval
    finally:
        routes.DEFAULTS = original
