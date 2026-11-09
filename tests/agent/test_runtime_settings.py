"""Saved dashboard settings affect the built-in agent, never a standalone agent."""
import asyncio
import sqlite3
from contextlib import closing

import pytest

import agent.config as config
import agent.main as main
import agent.terminal as terminal


@pytest.fixture
def saved(tmp_path, monkeypatch):
    path = tmp_path / "runtime.db"
    monkeypatch.setenv("GLASSOPS_DB_PATH", str(path))
    with closing(sqlite3.connect(path)) as conn:
        conn.execute("CREATE TABLE runtime_config (key TEXT PRIMARY KEY, value TEXT)")
        conn.commit()

    def save(**values):
        with closing(sqlite3.connect(path)) as conn:
            conn.executemany("INSERT OR REPLACE INTO runtime_config VALUES (?, ?)", values.items())
            conn.commit()
    return save


def test_runtime_overrides_validate_and_keep_last_good_read(saved, monkeypatch, tmp_path):
    saved(enable_docker="false", collect_interval="7", terminal_user="", enable_gpu="true")
    assert config.read_runtime_overrides() == {
        "enable_docker": "false", "collect_interval": "7", "terminal_user": "", "enable_gpu": "true",
    }
    missing = tmp_path / "missing.db"
    monkeypatch.setenv("GLASSOPS_DB_PATH", missing.as_uri() + "?mode=rwc")
    assert config.collection_interval_seconds() == 7
    assert not missing.exists()
    monkeypatch.delenv("GLASSOPS_DB_PATH")
    assert config.read_runtime_overrides() == {}
    assert config.collection_interval_seconds() == config.COLLECT_INTERVAL


def test_invalid_saved_values_do_not_override_environment(saved):
    saved(enable_docker="TRUE", collect_interval="0", terminal_user="root;bad", enable_gpu="bogus")
    assert config.read_runtime_overrides() == {}


@pytest.mark.parametrize("connect_timeout", [False, True])
async def test_loop_uses_saved_interval_for_collection_and_connect_timeout(saved, monkeypatch, connect_timeout):
    saved(collect_interval="7")
    stop = asyncio.Event()
    waits = []

    class Pusher:
        connected = not connect_timeout
        async def connect(self):
            pass
        async def send(self, metrics):
            return True
        async def close(self):
            pass

    async def wait_for(awaitable, timeout):
        awaitable.close()
        if timeout == 10:
            raise asyncio.TimeoutError
        waits.append(timeout)
        stop.set()

    async def collect():
        return {}

    async def rpc(*_):
        await stop.wait()

    monkeypatch.setattr(main, "MetricsPusher", Pusher)
    monkeypatch.setattr(main, "collect_metrics", collect)
    monkeypatch.setattr(main, "serve_rpc", rpc)
    monkeypatch.setattr(main, "check_transport_security", lambda: None)
    monkeypatch.setattr(main.asyncio, "Event", lambda: stop)
    monkeypatch.setattr(main.asyncio, "wait_for", wait_for)
    monkeypatch.setattr(asyncio.get_running_loop(), "add_signal_handler", lambda *_: None)
    await main.main()
    assert waits == [7]


@pytest.mark.parametrize("mapping,saved_user,want", [(None, "saveduser", "saveduser"), ("mapped", "saveduser", "mapped")])
def test_new_terminal_uses_mapping_then_saved_user(saved, monkeypatch, mapping, saved_user, want):
    saved(terminal_user=saved_user)
    monkeypatch.setenv("GLASSOPS_TERMINAL_USER", "envuser")
    commands = []
    class ChildExit(BaseException):
        pass
    def execvp(_, command):
        commands.append(command)
        raise ChildExit
    def child_exit(_):
        raise ChildExit
    monkeypatch.setattr(terminal.os.path, "exists", lambda _: True)
    monkeypatch.setattr(terminal.pty, "openpty", lambda: (100, 101))
    monkeypatch.setattr(terminal.os, "fork", lambda: 0)
    for name in ("close", "setsid", "dup2"):
        monkeypatch.setattr(terminal.os, name, lambda *_: None)
    monkeypatch.setattr(terminal.fcntl, "ioctl", lambda *_: None)
    monkeypatch.setattr(terminal.os, "execvp", execvp)
    monkeypatch.setattr(terminal.os, "_exit", child_exit)
    with pytest.raises(ChildExit):
        terminal.TerminalSession().spawn(mapping)
    assert commands[0][-3:] == ["su", "-", want]


def test_saved_empty_terminal_user_does_not_restore_environment_user(saved, monkeypatch):
    saved(terminal_user="")
    monkeypatch.setenv("GLASSOPS_TERMINAL_USER", "envuser")
    monkeypatch.delenv("GLASSOPS_ALLOW_LOGIN_PROMPT", raising=False)
    monkeypatch.setattr(terminal.os.path, "exists", lambda _: True)
    def forbidden_pty():
        raise AssertionError("unconfigured terminal allocated a PTY")
    monkeypatch.setattr(terminal.pty, "openpty", forbidden_pty)
    with pytest.raises(RuntimeError, match="No terminal user"):
        terminal.TerminalSession().spawn()
