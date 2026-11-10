"""The dashboard archive remains readable when its local collector is offline."""
import os
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient

from app import runtime_logs
from app.dependencies import get_current_user, require_admin
from app.routers import logs


@pytest.fixture
def archive(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime_logs, "ROOT", tmp_path)
    monkeypatch.setattr(runtime_logs, "_reopen_writers", lambda: None)
    runtime_logs.prepare_runtime_logs(tmp_path)
    (tmp_path / "backend.log").write_text("first\nerror before restart\n")
    runtime_logs.rotate_runtime_logs(tmp_path)
    (tmp_path / "backend.log").write_text("after restart\n")
    return tmp_path


def test_reader_includes_archived_lines_and_filters_recent_tail(archive):
    result = runtime_logs.read_runtime_logs(archive, tail=3)
    assert [line.split("] ", 1)[1] for line in result["lines"]] == [
        "first", "error before restart", "after restart",
    ]
    assert result["total"] == 3
    assert "error before restart" in runtime_logs.read_runtime_logs(archive, search="ERROR")["lines"][0]
    assert runtime_logs.read_runtime_logs(archive, tail=0)["total"] == 1


def test_reader_excludes_expired_archives_without_deleting_them(archive):
    old = archive / "backend.20000101T000000000000Z.012345abcdef.log"
    old.write_text("expired evidence")
    os.utime(old, (946684800, 946684800))
    (archive / "operator-private.txt").write_text("unmanaged")
    assert "expired" not in str(runtime_logs.read_runtime_logs(archive))
    assert "unmanaged" not in str(runtime_logs.read_runtime_logs(archive))
    assert old.exists()
    with pytest.raises(FileNotFoundError):
        runtime_logs.retained_log_file(archive, old.name)
    with pytest.raises(ValueError):
        runtime_logs.retained_log_file(archive, "../secret.key")


def test_missing_archive_is_an_error_not_empty_success(tmp_path):
    with pytest.raises(FileNotFoundError):
        runtime_logs.read_runtime_logs(tmp_path / "missing")
    with pytest.raises(FileNotFoundError):
        runtime_logs.read_runtime_logs(tmp_path)


def test_reader_import_does_not_bootstrap_database_or_secret(tmp_path):
    env = {"PYTHONPATH": str(Path(__file__).resolve().parents[2] / "backend"),
           "GLASSOPS_DB_PATH": str(tmp_path / "glassops.db")}
    subprocess.run([sys.executable, "-c", "from app.runtime_logs import read_runtime_logs"],
                   cwd=tmp_path, env=env, check=True)
    assert list(tmp_path.iterdir()) == []


@pytest.fixture
async def client(archive, monkeypatch):
    async def offline(*_args, **_kwargs):
        raise HTTPException(503, "Agent disconnected")
    monkeypatch.setattr(logs, "call_remote", offline)
    app = FastAPI()
    app.include_router(logs.router)
    app.dependency_overrides[require_admin] = lambda: "admin@example.com"
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client


async def test_local_app_archive_survives_agent_disconnect_and_does_not_impersonate_remote(client):
    response = await client.get("/api/logs/sources")
    assert response.status_code == 200
    assert response.json()["sources"] == [{"type": "app", "name": "glassops"}]
    response = await client.get("/api/logs/read?source_type=app&name=glassops")
    assert response.status_code == 200
    assert any("before restart" in line for line in response.json()["lines"])
    response = await client.get("/api/logs/read?source_type=app&name=glassops&agent_id=remote")
    assert response.status_code == 404
    response = await client.get("/api/logs/read?source_type=docker&name=glassops")
    assert response.status_code == 503


async def test_read_failure_is_reported(client, archive, monkeypatch):
    def fail(*_args, **_kwargs):
        raise PermissionError("not readable")
    monkeypatch.setattr(runtime_logs, "read_runtime_logs", fail)
    response = await client.get("/api/logs/read?source_type=app&name=glassops")
    assert response.status_code == 403


@pytest.mark.parametrize("authenticated", [False, True])
async def test_archive_requires_admin(archive, monkeypatch, authenticated):
    import app.dependencies as dependencies
    app = FastAPI()
    app.include_router(logs.router)
    if authenticated:
        app.dependency_overrides[get_current_user] = lambda: "user@example.com"
    async def user(_):
        return {"role": "user", "is_active": True}
    monkeypatch.setattr(dependencies, "get_user", user)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/logs/read?source_type=app&name=glassops")
    assert response.status_code == (403 if authenticated else 401)
