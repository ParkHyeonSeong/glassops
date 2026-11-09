"""Settings save -> shared DB -> next built-in-agent snapshot, without a restart."""

import importlib
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

import app.database as db
import agent.config as agent_config
import agent.main as agent
import app.routers.settings as settings_routes
from app.dependencies import require_admin
from app.routers.settings import router


@pytest.fixture
async def client(tmp_path, monkeypatch):
    path = tmp_path / "settings.db"
    monkeypatch.setattr(db, "_db_path", str(path))
    monkeypatch.setenv("GLASSOPS_DB_PATH", str(path))
    monkeypatch.setattr(agent, "ENABLE_GPU", True)
    monkeypatch.setattr(agent, "ENABLE_DOCKER", False)
    monkeypatch.setattr(agent, "ENABLE_NET_AUDIT", False)
    monkeypatch.setattr(agent, "collect_system", lambda: {})
    monkeypatch.setattr(agent, "collect_network", lambda: {})
    monkeypatch.setattr(agent, "collect_processes", lambda: [])
    monkeypatch.setattr(agent.pm2, "collect", lambda: [])
    monkeypatch.setattr(agent, "collect_gpu", lambda: [{"index": i} for i in range(4)])
    # Only hardware is simulated; persistence, HTTP routes and the collection loop are real.
    monkeypatch.setitem(sys.modules, "pynvml", SimpleNamespace(
        nvmlInit=lambda: None, nvmlShutdown=lambda: None,
        nvmlDeviceGetCount=lambda: 4,
    ))
    await db.init_db()
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[require_admin] = lambda: "admin@example.com"
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c
    await db.close_db()


async def test_save_controls_next_snapshot_and_survives_agent_reload(client):
    assert len((await agent.collect_metrics())["gpu"]) == 4
    assert (await client.post("/api/settings/runtime", json={"enable_gpu": "false"})).status_code == 200
    assert "gpu" not in await agent.collect_metrics()
    # A fresh agent must honor persisted OFF even when its startup default is ON.
    importlib.reload(agent_config)
    assert "gpu" not in await agent.collect_metrics()
    assert (await client.get("/api/settings/runtime")).json()["config"]["enable_gpu"] == "false"
    assert (await client.post("/api/settings/runtime", json={"enable_gpu": "true"})).status_code == 200
    assert len((await agent.collect_metrics())["gpu"]) == 4


async def test_unavailable_gpu_rejects_enable_without_saving_other_fields(client, monkeypatch):
    monkeypatch.setattr(sys.modules["pynvml"], "nvmlDeviceGetCount", lambda: 0)
    status = await client.get("/api/settings/runtime")
    assert status.json()["gpu_available"] is False
    before = await db.get_runtime_config()
    response = await client.post("/api/settings/runtime", json={"enable_gpu": "true", "collect_interval": "3"})
    assert response.status_code == 409
    assert "make up-gpu" in response.json()["detail"]
    assert await db.get_runtime_config() == before
    # A stale ON value must still be switchable OFF without GPU hardware.
    assert (await client.post("/api/settings/runtime", json={"enable_gpu": "false"})).status_code == 200
    assert "gpu" not in await agent.collect_metrics()


async def test_driver_initialization_failure_is_reported_as_unavailable(client, monkeypatch):
    def unavailable():
        raise RuntimeError("Driver Not Loaded")
    monkeypatch.setattr(sys.modules["pynvml"], "nvmlInit", unavailable)
    assert (await client.get("/api/settings/runtime")).json()["gpu_available"] is False
    response = await client.post("/api/settings/runtime", json={"enable_gpu": "true"})
    assert response.status_code == 409
    assert "make up-gpu" in response.json()["detail"]


@pytest.mark.parametrize("value", ["TRUE", "True", "true"])
async def test_settings_default_matches_agent_case_insensitive_environment(client, monkeypatch, value):
    original = settings_routes.DEFAULTS
    monkeypatch.setenv("GLASSOPS_ENABLE_GPU", value)
    try:
        importlib.reload(settings_routes)
        assert (await client.get("/api/settings/runtime")).json()["config"]["enable_gpu"] == "true"
    finally:
        settings_routes.DEFAULTS = original


async def test_off_does_not_call_gpu_collector(client, monkeypatch):
    await client.post("/api/settings/runtime", json={"enable_gpu": "false"})
    def unexpected_gpu_read():
        raise AssertionError("disabled GPU collection reached NVML")
    monkeypatch.setattr(agent, "collect_gpu", unexpected_gpu_read)
    assert "gpu" not in await agent.collect_metrics()


async def test_standalone_agent_keeps_environment_setting(client, monkeypatch):
    await client.post("/api/settings/runtime", json={"enable_gpu": "false"})
    monkeypatch.delenv("GLASSOPS_DB_PATH")
    assert len((await agent.collect_metrics())["gpu"]) == 4


async def test_saved_choice_is_read_from_existing_file_uri_paths(client, monkeypatch):
    await client.post("/api/settings/runtime", json={"enable_gpu": "false"})
    monkeypatch.setenv("GLASSOPS_DB_PATH", Path(db._db_path).as_uri() + "?mode=rwc&cache=private")
    importlib.reload(agent_config)
    assert "gpu" not in await agent.collect_metrics()


@pytest.mark.parametrize("file_uri", [False, True])
async def test_unreadable_settings_keep_last_applied_value_without_creating_db(client, monkeypatch, tmp_path, file_uri):
    await client.post("/api/settings/runtime", json={"enable_gpu": "false"})
    assert "gpu" not in await agent.collect_metrics()
    missing = tmp_path / "missing.db"
    monkeypatch.setenv("GLASSOPS_DB_PATH", missing.as_uri() + "?mode=rwc" if file_uri else str(missing))
    assert "gpu" not in await agent.collect_metrics()
    assert not missing.exists()
