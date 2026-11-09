"""Only explicitly confirmed IP rules may reach nginx on any start."""
import importlib
import sqlite3
import stat
import subprocess
from contextlib import closing
from types import SimpleNamespace

import pytest

from app.runtime_config_validate import validate_config_value


@pytest.fixture
def nginx(tmp_path, monkeypatch):
    module = importlib.import_module("app.nginx_config")
    monkeypatch.setattr(module, "IP_CONF", tmp_path / "ip-whitelist.conf")
    monkeypatch.setenv("GLASSOPS_DB_PATH", str(tmp_path / "settings.db"))
    monkeypatch.setenv("GLASSOPS_ALLOWED_IPS", "10.0.0.0/8")
    monkeypatch.setattr(module.subprocess, "run", lambda *a, **kw: SimpleNamespace(returncode=0, stderr=""))
    return module


def write_db(path, values):
    with closing(sqlite3.connect(path)) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS runtime_config (key TEXT PRIMARY KEY, value TEXT)")
        conn.executemany("INSERT OR REPLACE INTO runtime_config VALUES (?, ?)", values.items())
        conn.commit()


def test_legacy_draft_never_activates_on_repeated_starts(nginx, tmp_path):
    path = tmp_path / "settings.db"
    write_db(path, {"allowed_ips": "192.168.0.0/16"})
    for _ in range(2):
        assert nginx.prepare_ip_rules() == "10.0.0.0/8"
        assert nginx.read_active_ip_rules() == "10.0.0.0/8"
        assert "192.168" not in nginx.IP_CONF.read_text()
    assert stat.S_IMODE(nginx.IP_CONF.stat().st_mode) == 0o644
    with closing(sqlite3.connect(path)) as conn:
        assert conn.execute("SELECT * FROM runtime_config").fetchall() == [("allowed_ips", "192.168.0.0/16")]


@pytest.mark.parametrize("confirmed,want", [("", ""), ("192.168.1.4/24, 192.168.1.0/24,::1", "192.168.1.0/24,::1/128")])
def test_confirmed_rules_override_env_including_explicit_allow_all(nginx, tmp_path, confirmed, want):
    write_db(tmp_path / "settings.db", {"allowed_ips_confirmed": confirmed})
    assert nginx.prepare_ip_rules() == want
    assert nginx.read_active_ip_rules() == want
    if not confirmed:
        assert "default 1;" in nginx.IP_CONF.read_text()


def test_first_boot_does_not_create_database(nginx, tmp_path, monkeypatch):
    path = tmp_path / "settings.db"
    monkeypatch.setenv("GLASSOPS_DB_PATH", path.as_uri() + "?mode=rwc")
    assert nginx.prepare_ip_rules() == "10.0.0.0/8"
    assert not path.exists()
    with closing(sqlite3.connect(path)) as conn:
        conn.execute("CREATE TABLE bootstrap (value TEXT)")
    assert nginx.prepare_ip_rules() == "10.0.0.0/8"


def test_existing_corrupt_database_does_not_replace_rules(nginx, tmp_path):
    nginx.prepare_ip_rules()
    before = nginx.IP_CONF.read_bytes()
    (tmp_path / "settings.db").write_bytes(b"not a database")
    with pytest.raises(sqlite3.DatabaseError):
        nginx.prepare_ip_rules()
    assert nginx.IP_CONF.read_bytes() == before


@pytest.mark.parametrize("failure", ["syntax", "timeout"])
def test_failed_validation_restores_previous_file_and_leaves_no_included_temp(nginx, tmp_path, monkeypatch, failure):
    nginx.prepare_ip_rules()
    before = nginx.IP_CONF.read_bytes()
    write_db(tmp_path / "settings.db", {"allowed_ips_confirmed": "192.168.0.0/16"})
    def reject(*_, **__):
        assert list(tmp_path.glob("*.conf")) == [nginx.IP_CONF]
        if failure == "timeout":
            raise subprocess.TimeoutExpired("nginx", 5)
        return SimpleNamespace(returncode=1, stderr="invalid nginx configuration")
    monkeypatch.setattr(nginx.subprocess, "run", reject)
    with pytest.raises((RuntimeError, subprocess.TimeoutExpired)):
        nginx.prepare_ip_rules()
    assert nginx.IP_CONF.read_bytes() == before


@pytest.mark.parametrize("value", [",", " ", "fe80::1%eth0", "fe80::1%\n}#", "10.0.0.1;deny all"])
def test_values_unsafe_or_ambiguous_for_nginx_are_rejected(value):
    with pytest.raises(ValueError):
        validate_config_value("allowed_ips", value)


def test_invalid_nonempty_environment_never_turns_into_allow_all(nginx, monkeypatch):
    monkeypatch.setenv("GLASSOPS_ALLOWED_IPS", ",")
    with pytest.raises(ValueError):
        nginx.prepare_ip_rules()
    assert not nginx.IP_CONF.exists()
