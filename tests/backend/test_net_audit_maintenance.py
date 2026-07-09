import os
import time
import pytest
import app.database as db


@pytest.fixture
async def fresh_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "_db_path", str(tmp_path / "t.db"))
    monkeypatch.setattr(db, "_conn", None)
    await db.init_db()
    yield db
    await db.close_db()


async def test_env_configured_retention(fresh_db, monkeypatch):
    # Prove the maintenance helper reads env floors. 3-day event cutoff.
    now = time.time()
    old = {"event": "open", "ts": now - 4 * 86400, "proto": "tcp", "laddr": "a",
           "lport": 1, "raddr": "b", "rport": 2, "status": "E", "pid": 1,
           "pname": "x", "duration": None}
    await db.store_net_audit("a1", now, [old], [])
    from app.main import _net_audit_retention  # helper added in Step 3
    ev_days, roll_days = _net_audit_retention()
    monkeypatch.setenv("GLASSOPS_NET_AUDIT_EVENT_DAYS", "3")
    ev_days, roll_days = _net_audit_retention()
    assert ev_days == 3
    deleted = await db.cleanup_net_audit(event_days=ev_days, rollup_days=roll_days)
    assert deleted == 1
