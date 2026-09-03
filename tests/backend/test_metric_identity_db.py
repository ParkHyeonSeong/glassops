"""Raw metric sample identity — DB layer (store/read paths).

The ingest-layer tests live in test_metric_ingest.py: importing ingest_metric
there must not break collection of this file while Task 2 is unimplemented.
"""

import asyncio
import json
import threading

import aiosqlite
import pytest

import app.database as db


@pytest.fixture
async def fresh_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "_db_path", str(tmp_path / "t.db"))
    monkeypatch.setattr(db, "_conn", None)
    monkeypatch.setattr(db, "_metric_conn", None, raising=False)
    # A fresh lock per test: an asyncio.Lock binds to the event loop on its
    # first contended acquire, and pytest-asyncio gives each test its own
    # function-scoped loop — reusing a bound lock across tests would raise
    # "bound to a different event loop".
    monkeypatch.setattr(db, "_op_lock", asyncio.Lock(), raising=False)
    # Fail-stop latches for the life of the process by design; clear it per test.
    monkeypatch.setattr(db, "_fail_stop", None, raising=False)
    await db.init_db()
    yield db
    await db.close_db()
    db._closed = False  # close_db latches "closed" for the process; a test reopens a fresh DB


def _metric(cpu=10.0):
    return {"timestamp": 0.0, "cpu": {"percent_total": cpu}}


async def test_store_metric_returns_rowid(fresh_db):
    first = await db.store_metric("a1", 100.0, _metric())
    second = await db.store_metric("a1", 101.0, _metric())
    assert isinstance(first, int)
    assert isinstance(second, int)
    assert second > first


async def test_store_metric_rowids_increase_across_clock_rollback(fresh_db):
    # Arrival order must be derivable from ids even when the measured
    # timestamp steps backwards (NTP correction).
    newer_wall_clock = await db.store_metric("a1", 200.0, _metric(20))
    ntp_corrected = await db.store_metric("a1", 100.0, _metric(90))
    assert ntp_corrected > newer_wall_clock


async def test_concurrent_stores_all_persist_with_distinct_ids(fresh_db, monkeypatch):
    # Lazy-init concurrency: 10 gathered first-calls must open exactly ONE
    # metric connection, not one each (the old check-then-act race leaked 9
    # non-daemon connections and hung interpreter exit). The connect counter
    # catches the leak deterministically; wait_for bounds a regression so it
    # fails instead of hanging the whole suite. NOTE this does NOT prove
    # INSERT+commit atomicity — even lock-free, aiosqlite serializes the
    # queued INSERTs and one commit flushes the rest, so ids 1..10/count 10
    # still hold. The serialization contract is pinned by the next test.
    opened = []  # collect EVERY connection created during the gather so a
    real_connect = aiosqlite.connect  # regression that opens 10 doesn't leak

    def tracking_connect(*args, **kwargs):
        conn = real_connect(*args, **kwargs)
        opened.append(conn)
        return conn

    monkeypatch.setattr(aiosqlite, "connect", tracking_connect)  # shared conn already open
    try:
        ids = await asyncio.wait_for(
            asyncio.gather(*[db.store_metric(f"a{i % 2}", 100.0 + i, _metric(i)) for i in range(10)]),
            timeout=5,
        )
        assert len(opened) == 1  # one metric connection for all 10 stores, not ten
        assert sorted(ids) == list(range(1, 11))
        conn = await db._get_conn()
        cursor = await conn.execute("SELECT COUNT(*) FROM metrics")
        assert (await cursor.fetchone())[0] == 10
    finally:
        # Close every connection a regression may have leaked. Skip the live
        # _metric_conn — fresh_db's teardown close_db owns it (avoids a
        # double close); a leaked one is closed here so no worker survives.
        for conn in opened:
            if conn is db._metric_conn:
                continue
            try:
                await conn.close()
            except Exception:
                pass


async def test_store_serializes_insert_commit_under_lock(fresh_db):
    # Directly pins the write lock: stall store A after its INSERT, before
    # commit, and prove store B cannot run its INSERT until A's transaction
    # completes. Without the lock B's INSERT interleaves (verified: the
    # all-success test above passes lock-free; this one fails).
    conn = await db._get_metric_db()

    inserts: list[str] = []
    real_execute = conn.execute

    async def recording_execute(sql, *args, **kwargs):
        if isinstance(sql, str) and sql.startswith("INSERT INTO metrics"):
            inserts.append(sql)
        return await real_execute(sql, *args, **kwargs)

    conn.execute = recording_execute

    in_commit = asyncio.Event()
    release = asyncio.Event()
    real_commit = conn.commit

    async def stalled_commit():
        in_commit.set()
        await release.wait()
        return await real_commit()

    conn.commit = stalled_commit
    a = b = None
    try:
        a = asyncio.create_task(db.store_metric("a1", 100.0, _metric(1)))
        await asyncio.wait_for(in_commit.wait(), timeout=5)  # A parked in commit
        b = asyncio.create_task(db.store_metric("a1", 101.0, _metric(2)))
        for _ in range(5):                   # give B every chance to run its INSERT
            await asyncio.sleep(0)
        # B is blocked on the DB-file operation lock — only A's INSERT has run.
        assert inserts == ["INSERT INTO metrics (agent_id, timestamp, data) VALUES (?, ?, ?)"]
    finally:
        conn.execute = real_execute
        conn.commit = real_commit
        release.set()
        # Always drain both tasks so a failed assertion can't leak them;
        # return_exceptions keeps cleanup from masking the real failure.
        results = await asyncio.wait_for(
            asyncio.gather(*(t for t in (a, b) if t is not None), return_exceptions=True),
            timeout=5,
        )

    assert results == [1, 2]
    cursor = await conn.execute("SELECT COUNT(*) FROM metrics")
    assert (await cursor.fetchone())[0] == 2


async def test_store_metric_rolls_back_failed_commit(fresh_db):
    # Without a rollback, the failed INSERT stays pending on the metric
    # connection and the NEXT commit there persists it — a row already
    # reported (and broadcast) as unpersisted would reappear in History.
    conn = await db._get_metric_db()

    async def failing_commit():
        raise RuntimeError("disk full at commit")

    original_commit = conn.commit
    conn.commit = failing_commit
    try:
        with pytest.raises(RuntimeError):
            await db.store_metric("a1", 100.0, _metric(50))
    finally:
        conn.commit = original_commit

    survivor = await db.store_metric("a1", 101.0, _metric(60))
    assert isinstance(survivor, int)
    cursor = await conn.execute("SELECT COUNT(*) FROM metrics WHERE agent_id = 'a1'")
    assert (await cursor.fetchone())[0] == 1


async def test_store_metric_rolls_back_when_cancelled_after_worker_submission(fresh_db):
    # Cancellation while the INSERT is genuinely owned by the aiosqlite WORKER —
    # not a wrapper parked before it ever called the driver. The worker finishes
    # the statement either way, so the boundary must recover its outcome, roll
    # the pending INSERT back (no row), and only then hand back the
    # CancelledError. `except Exception` would miss CancelledError; this pins
    # the BaseException handler.
    #
    # The COMMIT-cancellation outcomes (commit-wins, and unresolved -> fail-stop)
    # are pinned separately in test_db_write_transaction.py.
    conn = await db._get_metric_db()

    # Occupy the worker thread with a real statement so the write's next
    # submission is truly queued to it.
    entered, release = threading.Event(), threading.Event()

    def occupy(x):
        entered.set()
        release.wait(timeout=30)
        return x

    await conn.create_function("occupy", 1, occupy)
    holder = asyncio.create_task(conn.execute("SELECT occupy(1)"))
    await asyncio.wait_for(asyncio.to_thread(entered.wait, 5), timeout=10)
    assert entered.is_set(), "worker never entered the occupying statement"

    task = asyncio.create_task(db.store_metric("a1", 100.0, _metric(50)))
    for _ in range(50):                      # let BEGIN reach the worker queue
        if conn._tx.qsize() >= 1:
            break
        await asyncio.sleep(0)
    assert conn._tx.qsize() >= 1, "the write never reached the aiosqlite worker"

    task.cancel()
    release.set()                            # the worker drains its queue
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=15)
    cursor = await asyncio.wait_for(holder, timeout=10)
    await cursor.close()

    assert db._fail_stop is None, db._fail_stop  # a clean rollback is not fail-stop
    assert conn.in_transaction is False, "transaction left open"
    survivor = await db.store_metric("a1", 101.0, _metric(60))
    assert isinstance(survivor, int)
    rows = await db.get_recent_metrics("a1", limit=10)
    assert len(rows) == 1, rows              # only the survivor persisted


@pytest.mark.expect_db_leftovers
async def test_close_db_reports_restart_required_when_a_close_is_cancelled(fresh_db):
    # Contract change: a connection close that comes back CANCELLED proves
    # nothing about whether the connection actually closed, so close_db can no
    # longer treat it as "handled" and carry on to the next connection as if the
    # first were done. It detaches both globals (no stale global survives) and
    # reports RESTART_REQUIRED instead of propagating a bare CancelledError.
    shared = await db._get_conn()
    metric = await db._get_metric_db()
    real_shared_close, real_metric_close = shared.close, metric.close

    async def cancelled_close():
        raise asyncio.CancelledError()

    shared.close = cancelled_close
    try:
        verdict = await asyncio.wait_for(db.close_db(timeout=5), timeout=15)
        assert verdict is db.CloseVerdict.RESTART_REQUIRED, verdict
        assert db._conn is None and db._metric_conn is None  # globals detached
        assert db._fail_stop is not None
    finally:
        # The patched close was a no-op — really close both, or their non-daemon
        # aiosqlite workers outlive the test.
        shared.close, metric.close = real_shared_close, real_metric_close
        await shared.close()
        await metric.close()


async def test_store_metric_fails_stop_when_rollback_cannot_be_confirmed(fresh_db):
    # Contract change (storage-wedge slice): when commit fails AND rollback
    # fails, this used to detach the connection so the next store rebuilt a
    # fresh one. That is no longer done — with the rollback outcome unknown,
    # the pending INSERT's fate is unknown too, so a replacement connection
    # would keep writing the same file next to a transaction that may still be
    # open. The DB now latches restart-required and refuses every later write,
    # on BOTH connections, until the process restarts.
    conn = await db._get_metric_db()

    async def failing_commit():
        raise RuntimeError("commit failed")

    async def failing_rollback():
        raise RuntimeError("rollback failed")

    conn.commit = failing_commit
    conn.rollback = failing_rollback
    try:
        with pytest.raises(RuntimeError):
            await asyncio.wait_for(db.store_metric("a1", 100.0, _metric(50)), timeout=5)

        # The connection is kept, NOT swapped, and writes are refused outright.
        assert db._metric_conn is conn
        assert db._fail_stop is not None
        with pytest.raises(db.DatabaseFailStop):
            await asyncio.wait_for(db.store_metric("a1", 101.0, _metric(60)), timeout=5)
        # One DB file, one verdict: the shared connection is refused too.
        with pytest.raises(db.DatabaseFailStop):
            await asyncio.wait_for(db.blacklist_token("t", 1.0), timeout=5)
    finally:
        del conn.commit
        del conn.rollback


def _container_metric(cpu=10.0, name="worker"):
    return {
        "timestamp": 0.0,
        "cpu": {"percent_total": cpu},
        "containers": [{
            "name": name, "cpu_percent": cpu,
            "mem_usage": 256.0, "mem_limit": 1024.0,
        }],
    }


async def test_recent_metrics_selects_and_orders_by_id(fresh_db):
    # Selection AND ordering by id: the future-skewed t=200 row must not
    # displace the genuinely newer arrival (t=50, NTP-corrected) from the
    # "recent 2" window, and output is oldest-arrival-first.
    await db.store_metric("a1", 100.0, _metric(1))   # id 1
    await db.store_metric("a1", 200.0, _metric(2))   # id 2 (future-skewed)
    await db.store_metric("a1", 50.0, _metric(3))    # id 3 (NTP-corrected)

    recent = await db.get_recent_metrics("a1", limit=2)

    assert [e["timestamp"] for e in recent] == [200.0, 50.0]
    assert [e["sample_id"] for e in recent] == ["raw:2", "raw:3"]
    assert [e["arrival_seq"] for e in recent] == [2, 3]


async def test_recent_metrics_clamps_nonpositive_limit(fresh_db):
    # main.py passes min(limit, 300): a negative query param would reach
    # SQLite as LIMIT -1 (unbounded). The DB layer must floor it.
    for i in range(3):
        await db.store_metric("a1", 100.0 + i, _metric(i))

    assert len(await db.get_recent_metrics("a1", limit=-1)) == 1
    assert len(await db.get_recent_metrics("a1", limit=0)) == 1


async def test_metrics_range_keeps_time_order_and_carries_identity(fresh_db):
    # Response order stays TIME-based (SystemMonitor/GpuMonitor draw it on a
    # category axis verbatim); the arrival order travels as arrival_seq and
    # the container frontend sorts by it.
    await db.store_metric("a1", 100.0, _metric(1))   # id 1
    await db.store_metric("a1", 200.0, _metric(2))   # id 2 (future-skewed)
    await db.store_metric("a1", 150.0, _metric(3))   # id 3 (NTP rollback, arrives last)

    rows = await db.get_metrics_range("a1", 0.0, 3600.0)

    assert [r["timestamp"] for r in rows] == [100.0, 150.0, 200.0]
    assert [r["arrival_seq"] for r in rows] == [1, 3, 2]
    assert [r["sample_id"] for r in rows] == ["raw:1", "raw:3", "raw:2"]


async def test_metrics_range_same_timestamp_rows_are_distinct_ordered(fresh_db):
    # The ", id" tie-break makes equal-t rows deterministic: arrival order.
    await db.store_metric("a1", 100.0, _metric(20))
    await db.store_metric("a1", 100.0, _metric(70))

    rows = await db.get_metrics_range("a1", 0.0, 3600.0)

    assert len(rows) == 2
    assert rows[0]["arrival_seq"] < rows[1]["arrival_seq"]
    assert rows[0]["sample_id"] != rows[1]["sample_id"]
    assert [r["cpu"]["percent_total"] for r in rows] == [20, 70]


async def test_metrics_range_strips_forged_reserved_keys_from_raw_rows(fresh_db):
    # A row stored before the ingest strip landed can carry ALL four forged
    # reserved keys in its data JSON; the read path must never serve them.
    forged = _metric(10)
    forged.update({"sample_id": "raw:424242", "arrival_seq": 424242,
                   "persisted": False, "after_seq": 424241})
    await db.store_metric("a1", 100.0, forged)

    rows = await db.get_metrics_range("a1", 0.0, 3600.0)

    assert rows[0]["sample_id"] == "raw:1"
    assert rows[0]["arrival_seq"] == 1
    assert "persisted" not in rows[0]
    assert "after_seq" not in rows[0]


async def test_metrics_range_downsampled_rows_carry_no_raw_identity(fresh_db):
    conn = await db._get_conn()
    poisoned = json.dumps({"cpu": {"percent_total": 5},
                           "sample_id": "raw:31337", "arrival_seq": 31337,
                           "persisted": False, "after_seq": 31336})
    await conn.execute(
        "INSERT INTO metrics_downsampled (agent_id, timestamp, resolution, data) "
        "VALUES ('a1', 600.0, '1m', ?)", (poisoned,),
    )
    await conn.commit()

    rows = await db.get_metrics_range("a1", 0.0, 4000.0)  # >1h -> 1m table

    assert len(rows) == 1
    assert "sample_id" not in rows[0]
    assert "arrival_seq" not in rows[0]
    assert "persisted" not in rows[0]
    assert "after_seq" not in rows[0]


async def test_container_history_points_carry_identity(fresh_db):
    await db.store_metric("a1", 100.0, _container_metric(20))
    await db.store_metric("a1", 100.0, _container_metric(70))

    points = await db.get_container_history("a1", "worker", 0.0, 3600.0)

    assert len(points) == 2
    assert [p["arrival_seq"] for p in points] == [1, 2]
    assert points[0]["sample_id"] == "raw:1"
    assert points[1]["sample_id"] == "raw:2"
    assert [p["cpu"] for p in points] == [20.0, 70.0]


async def test_preexisting_rows_gain_identity_on_read(fresh_db):
    # Legacy rows (stored by the pre-identity code) gain identity purely at
    # read time from the id column — no migration, init_db untouched.
    conn = await db._get_conn()
    legacy = json.dumps({"cpu": {"percent_total": 5}, "persisted": False})
    await conn.execute(
        "INSERT INTO metrics (agent_id, timestamp, data) VALUES ('a1', 100.0, ?)",
        (legacy,),
    )
    await conn.commit()

    recent = await db.get_recent_metrics("a1", limit=10)

    assert recent[0]["sample_id"] == "raw:1"
    assert recent[0]["arrival_seq"] == 1
    assert "persisted" not in recent[0]


async def test_get_max_metric_id_uses_last_issued_not_max(fresh_db):
    # r3.2 #1: cleanup deletes the highest row but sqlite_sequence keeps the
    # last ISSUED id, so the anchor is 3 (last issued), not MAX(id)=2.
    for i in range(3):
        await db.store_metric("a1", 100.0 + i, _metric(i))
    conn = await db._get_conn()
    await conn.execute("DELETE FROM metrics WHERE id = 3")
    await conn.commit()

    cursor = await conn.execute("SELECT MAX(id) FROM metrics")
    assert (await cursor.fetchone())[0] == 2       # MAX(id) under-counts
    assert await db.get_max_metric_id() == 3       # sqlite_sequence is correct


async def test_memory_db_path_shares_schema_across_connections(monkeypatch, tmp_path):
    # GLASSOPS_DB_PATH=":memory:" (env-configurable, GLASSOPS_ env_prefix)
    # must not silently split the shared and metric connections into two
    # PRIVATE in-memory databases. A bare ":memory:" gives EVERY
    # aiosqlite.connect() call its own database, so init_db() (which uses
    # get_db) creates the schema in one database while store_metric (which
    # uses _get_metric_db) writes to a different, schema-less one — the
    # server still starts, but every metric would silently become
    # ephemeral and History would stay empty (OperationalError: no such
    # table: metrics, swallowed by store_metric's except clause).
    monkeypatch.setattr(db, "_db_path", ":memory:")
    monkeypatch.setattr(db, "_conn", None)
    monkeypatch.setattr(db, "_metric_conn", None, raising=False)
    monkeypatch.setattr(db, "_op_lock", asyncio.Lock(), raising=False)
    # Fail-stop latches for the life of the process by design; clear it per test.
    monkeypatch.setattr(db, "_fail_stop", None, raising=False)
    # init_db bootstraps a default admin when no users exist, writing a
    # one-time password file relative to os.path.dirname(_db_path) — empty
    # for a bare ":memory:", i.e. cwd. Unrelated to this test's subject;
    # chdir into tmp_path so that stray file doesn't land in the repo root.
    monkeypatch.chdir(tmp_path)
    try:
        await db.init_db()  # schema created via get_db's connection

        row_id = await db.store_metric("a1", 100.0, _metric(42))  # via _get_metric_db
        assert isinstance(row_id, int)

        recent = await db.get_recent_metrics("a1", limit=10)  # read via get_db
        assert [e["sample_id"] for e in recent] == ["raw:1"]
        assert recent[0]["cpu"]["percent_total"] == 42
    finally:
        await db.close_db()
        db._closed = False  # close_db latches "closed" for the process; a test reopens a fresh DB
