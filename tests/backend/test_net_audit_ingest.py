import pytest
import app.database as db
from app.services.net_audit_ingest import extract_and_store_net_audit


@pytest.fixture
async def fresh_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "_db_path", str(tmp_path / "t.db"))
    monkeypatch.setattr(db, "_conn", None)
    await db.init_db()
    yield db
    await db.close_db()


def _mk(ev):
    return {"net_audit": {"events": [ev], "rollups": []}}


async def test_extracts_and_removes_field(fresh_db):
    data = {"timestamp": 1000.0, "cpu": 1,
            "net_audit": {"events": [{"event": "open", "ts": 1000.0, "proto": "tcp",
                                      "laddr": "a", "lport": 1, "raddr": "b", "rport": 2,
                                      "status": "ESTABLISHED", "pid": 5, "pname": "x",
                                      "duration": None}],
                          "rollups": []}}
    await extract_and_store_net_audit("a1", 1000.0, data)
    assert "net_audit" not in data           # removed before broadcast/store_metric
    assert len(await db.get_net_conn_events("a1")) == 1


async def test_missing_field_is_noop(fresh_db):
    data = {"timestamp": 1.0, "cpu": 1}
    await extract_and_store_net_audit("a1", 1.0, data)  # must not raise
    assert await db.get_net_conn_events("a1") == []


async def test_malformed_field_ignored(fresh_db):
    data = {"net_audit": "not-a-dict"}
    await extract_and_store_net_audit("a1", 1.0, data)
    assert "net_audit" not in data
    assert await db.get_net_conn_events("a1") == []


async def test_long_strings_are_capped(fresh_db):
    # A compromised agent stuffing a 10 KB "payload" into pname must be truncated.
    ev = {"event": "open", "ts": 1.0, "proto": "tcp", "laddr": "a", "lport": 1,
          "raddr": "b", "rport": 2, "status": "E", "pid": 1, "pname": "P" * 10000,
          "duration": None}
    await extract_and_store_net_audit("a1", 1.0, _mk(ev))
    rows = await db.get_net_conn_events("a1")
    assert len(rows) == 1
    assert len(rows[0]["pname"]) == 64


async def test_invalid_enum_rows_dropped(fresh_db):
    bad_event = {"event": "PWNED", "ts": 1.0, "proto": "tcp", "laddr": "a", "lport": 1,
                 "raddr": "b", "rport": 2, "status": "E", "pid": 1, "pname": "x", "duration": None}
    bad_proto = {**bad_event, "event": "open", "proto": "rawsock"}
    await extract_and_store_net_audit("a1", 1.0,
                                      {"net_audit": {"events": [bad_event, bad_proto], "rollups": []}})
    assert await db.get_net_conn_events("a1") == []


async def test_non_finite_and_out_of_range_numbers_rejected(fresh_db):
    ev = {"event": "open", "ts": 1.0, "proto": "tcp", "laddr": "a", "lport": "not-int",
          "raddr": "b", "rport": 99999, "status": "E", "pid": -5, "pname": "x", "duration": -3}
    await extract_and_store_net_audit("a1", 1.0, _mk(ev))
    row = (await db.get_net_conn_events("a1"))[0]
    assert row["lport"] is None      # non-int rejected
    assert row["rport"] is None      # out of 0..65535
    assert row["pid"] is None        # negative rejected
    assert row["duration"] is None   # negative rejected


async def test_event_count_capped(fresh_db):
    ev = {"event": "open", "ts": 1.0, "proto": "tcp", "laddr": "a", "lport": 1,
          "raddr": "b", "rport": 2, "status": "E", "pid": 1, "pname": "x", "duration": None}
    await extract_and_store_net_audit("a1", 1.0,
                                      {"net_audit": {"events": [ev] * 600, "rollups": []}})
    assert len(await db.get_net_conn_events("a1", limit=1000)) <= 500


async def test_rollup_nested_lists_capped(fresh_db):
    roll = {"ts": 60.0,
            "interfaces": [{"name": "N" * 999, "bytes_in": 1, "bytes_out": 2}] * 200,
            "top_talkers": [{"raddr": "1.1.1.1", "conns": 1}] * 500}
    await extract_and_store_net_audit("a1", 1.0, {"net_audit": {"events": [], "rollups": [roll]}})
    got = await db.get_net_flow_rollup("a1", 0, 1000)
    assert len(got) == 1
    assert len(got[0]["interfaces"]) <= 64
    assert len(got[0]["interfaces"][0]["name"]) <= 32
    assert len(got[0]["top_talkers"]) <= 100


async def test_addresses_validated_as_ip(fresh_db):
    # Review P2: address fields must hold a real IP, not an arbitrary payload string.
    good = {"event": "open", "ts": 1.0, "proto": "tcp", "laddr": "10.0.0.9", "lport": 22,
            "raddr": "8.8.8.8", "rport": 443, "status": "E", "pid": 1, "pname": "x", "duration": None}
    bad = {**good, "raddr": "not-an-ip; DROP TABLE"}
    await extract_and_store_net_audit("a1", 5.0,
                                      {"net_audit": {"events": [good, bad], "rollups": []}})
    raddrs = sorted(r["raddr"] for r in await db.get_net_conn_events("a1"))
    assert raddrs == ["", "8.8.8.8"]   # invalid address blanked, valid kept


async def test_event_ts_pinned_to_server_timestamp(fresh_db):
    # Review P2: the agent's per-event ts is not trusted; the server timestamp wins.
    ev = {"event": "open", "ts": 9_999_999_999.0, "proto": "tcp", "laddr": "10.0.0.9",
          "lport": 22, "raddr": "8.8.8.8", "rport": 443, "status": "E", "pid": 1,
          "pname": "x", "duration": None}
    await extract_and_store_net_audit("a1", 5.0, _mk(ev))
    row = (await db.get_net_conn_events("a1"))[0]
    assert row["ts"] == 5.0
