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


def _ev(raddr="10.0.0.5", event="open", ts=1000.0, pid=42):
    return {"event": event, "ts": ts, "proto": "tcp", "laddr": "10.0.0.9",
            "lport": 5500, "raddr": raddr, "rport": 443, "status": "ESTABLISHED",
            "pid": pid, "pname": "curl", "duration": None}


async def test_store_and_query_events(fresh_db):
    await db.store_net_audit("a1", 1000.0,
                             [_ev(), _ev(raddr="8.8.8.8", pid=99)], [])
    rows = await db.get_net_conn_events("a1")
    assert len(rows) == 2
    got = await db.get_net_conn_events("a1", raddr="8.8.8.8")
    assert len(got) == 1 and got[0]["pid"] == 99


async def test_query_isolates_agents(fresh_db):
    await db.store_net_audit("a1", 1000.0, [_ev()], [])
    await db.store_net_audit("a2", 1000.0, [_ev(raddr="1.1.1.1")], [])
    assert len(await db.get_net_conn_events("a2")) == 1
    assert (await db.get_net_conn_events("a2"))[0]["raddr"] == "1.1.1.1"


async def test_negative_limit_is_clamped(fresh_db):
    # Review P2: a negative limit must not become an unbounded SQLite `LIMIT -1`.
    await db.store_net_audit("a1", 1000.0, [_ev(raddr=f"10.0.0.{i}") for i in range(3)], [])
    rows = await db.get_net_conn_events("a1", limit=-1)
    assert len(rows) == 1   # clamped to max(1, ...), NOT all 3 (which would be unbounded)


async def test_keyset_pagination_no_skip_on_same_ts(fresh_db):
    # Review P2: all 5 events share one server ts (one collect tick). A ts-only cursor
    # would drop the same-ts rows beyond the page; the (ts, id) keyset pages losslessly.
    await db.store_net_audit("a1", 1000.0,
                             [_ev(raddr=f"10.0.0.{i}", ts=1000.0) for i in range(5)], [])
    seen = []
    cursor_ts, cursor_id = None, None
    for _ in range(3):  # 5 rows, page size 2 -> 3 pages
        page = await db.get_net_conn_events("a1", before_ts=cursor_ts,
                                            before_id=cursor_id, limit=2)
        if not page:
            break
        seen.extend(page)
        cursor_ts, cursor_id = page[-1]["ts"], page[-1]["id"]
    assert len(seen) == 5                                   # nothing skipped
    assert len({r["id"] for r in seen}) == 5                # nothing duplicated
    assert {r["raddr"] for r in seen} == {f"10.0.0.{i}" for i in range(5)}


async def test_rollup_upsert(fresh_db):
    roll = {"ts": 60.0, "interfaces": [{"name": "eth0", "bytes_in": 1, "bytes_out": 2}],
            "top_talkers": [{"raddr": "10.0.0.5", "conns": 3}]}
    await db.store_net_audit("a1", 100.0, [], [roll])
    await db.store_net_audit("a1", 100.0, [], [roll])  # same bucket -> replace
    got = await db.get_net_flow_rollup("a1", 0, 1000)
    assert len(got) == 1
    assert got[0]["interfaces"][0]["name"] == "eth0"


async def test_cleanup_tiered_retention(fresh_db):
    now = time.time()
    old_ev = _ev(ts=now - 8 * 86400)
    new_ev = _ev(ts=now - 1 * 86400)
    old_roll = {"ts": now - 31 * 86400, "interfaces": [], "top_talkers": []}
    new_roll = {"ts": now - 10 * 86400, "interfaces": [], "top_talkers": []}
    await db.store_net_audit("a1", now, [old_ev, new_ev], [old_roll, new_roll])
    deleted = await db.cleanup_net_audit(event_days=7, rollup_days=30)
    assert deleted == 1
    assert len(await db.get_net_conn_events("a1")) == 1
    assert len(await db.get_net_flow_rollup("a1", 0, now)) == 1
