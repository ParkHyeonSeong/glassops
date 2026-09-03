"""Regression suite for the DB write-transaction boundary (storage-wedge slice).

Contract under test: every mutating path in `app.database` runs inside one
operation boundary that

  (a) holds a single DB-file-wide lock covering BOTH the shared and the metric
      connection — no second lock, no lock ordering,
  (b) opens the transaction with an explicit `BEGIN IMMEDIATE`,
  (c) closes every cursor it opened AND terminates the transaction before it
      releases the lock — on success, on Exception, on BaseException and on
      CancelledError alike,
  (d) refuses further writes (fail-stop / restart-required) instead of quietly
      swapping the connection when a rollback or close outcome cannot be
      established.

Why (c) needs both halves: Phase 0 (`deploy/contracts/slices/pl1/phase0/`,
cases CP-1 and CP-1-RECOVER-*) measured that an unconsumed cursor's read
snapshot and a failed open transaction each block WAL checkpointing and every
later write INDEPENDENTLY — closing the cursor alone does not recover, rolling
back alone does not recover. That runner is the failure evidence and stays as
it is; this file is the separate suite that must go GREEN with the fix.
"""

import ast
import asyncio
import json
import logging
import os
import subprocess
import sys
import time
import traceback
from pathlib import Path

import aiosqlite
import pytest

import app.database as db

DATABASE_PY = Path(db.__file__)


# ── fixtures ─────────────────────────────────────────


@pytest.fixture
async def fresh_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "_db_path", str(tmp_path / "t.db"))
    monkeypatch.setattr(db, "_conn", None)
    monkeypatch.setattr(db, "_metric_conn", None, raising=False)
    # A fresh lock per test: an asyncio.Lock binds to the loop that first
    # acquires it, and pytest-asyncio gives each test its own loop.
    monkeypatch.setattr(db, "_op_lock", asyncio.Lock(), raising=False)
    # Fail-stop is process-wide and deliberately sticky — clear it per test.
    monkeypatch.setattr(db, "_fail_stop", None, raising=False)
    await db.init_db()
    yield db
    # Guard: a test that restores _db_path mid-run would silently start
    # talking to the real data/ DB. Fail loudly instead.
    assert db._db_path == str(tmp_path / "t.db"), (
        f"a test un-patched _db_path to {db._db_path!r} — never share the "
        "`monkeypatch` fixture with fresh_db; use pytest.MonkeyPatch.context()"
    )
    await db.close_db()
    db._closed = False  # close_db latches "closed" for the process; a test reopens a fresh DB


def _metric(cpu=10.0):
    return {"timestamp": 0.0, "cpu": {"percent_total": cpu}}


def _event(ts=100.0):
    return {"ts": ts, "event": "open", "proto": "tcp", "laddr": "127.0.0.1",
            "lport": 1, "raddr": "10.0.0.1", "rport": 2, "status": "ESTABLISHED",
            "pid": 3, "pname": "x", "duration": 0.0}


def assert_busy_snapshot(excinfo):
    """Assert the EXACT SQLite error, not merely "something raised".

    `pytest.raises(Exception)` would also pass on a typo, a closed connection or
    a fail-stop refusal — none of which is the wedge under test."""
    exc = excinfo.value
    assert isinstance(exc, sqlite3.OperationalError), f"expected OperationalError, got {exc!r}"
    name = getattr(exc, "sqlite_errorname", None)
    assert name == "SQLITE_BUSY_SNAPSHOT", f"expected SQLITE_BUSY_SNAPSHOT, got {name} ({exc})"


async def _wedge(agent="seed"):
    """Reproduce Phase 0 CP-1 against the real product connections: a
    half-consumed cursor on the shared connection pins a read snapshot, then a
    commit on the metric connection advances the WAL past it, so the next
    shared write hits SQLITE_BUSY_SNAPSHOT.

    Returns the abandoned cursor — releasing it is the TEST's job. Production
    is only responsible for not leaving its own cursor or transaction behind.
    """
    for i in range(3):
        await db.store_metric(agent, 100.0 + i, _metric(i))
    shared = await db._get_conn()
    cursor = await shared.execute("SELECT id, agent_id, timestamp, data FROM metrics")
    await cursor.fetchone()                      # partial fetch — snapshot pinned
    await db.store_metric(agent, 104.0, _metric(9))  # other connection commits
    return shared, cursor


# ── 1. the wedge must not leave a transaction open ───


async def test_busy_snapshot_write_leaves_no_open_transaction(fresh_db):
    # Phase 0 CP-1 measured `in_transaction=True` surviving the failure. The
    # boundary must terminate the transaction before it returns.
    shared, cursor = await _wedge()
    try:
        with pytest.raises(sqlite3.OperationalError) as excinfo:
            await db.store_net_audit("seed", 104.0, [_event(104.0)], [])
        assert_busy_snapshot(excinfo)
        assert shared.in_transaction is False
    finally:
        await cursor.close()


async def test_write_recovers_once_the_stale_reader_is_released(fresh_db):
    # Phase 0 CP-1: after the first failure EVERY later shared write failed too,
    # even after the stale cursor was closed, because the failed transaction was
    # still open. With the boundary terminating it, releasing the test's cursor
    # is enough for the next production write to succeed.
    shared, cursor = await _wedge()
    with pytest.raises(sqlite3.OperationalError) as excinfo:
        await db.store_net_audit("seed", 104.0, [_event(104.0)], [])
    assert_busy_snapshot(excinfo)
    await cursor.close()
    await db.store_net_audit("seed", 105.0, [_event(105.0)], [])
    assert len(await db.get_net_conn_events("seed")) == 1


async def test_wal_checkpoint_completes_after_the_failed_write(fresh_db, tmp_path):
    # Phase 0 CP-1: while wedged, a manual PASSIVE checkpoint could not process
    # all frames (`checkpointed < log`) — this is the WAL-growth mechanism. Once
    # the transaction is terminated and the reader released, it must complete.
    shared, cursor = await _wedge()
    with pytest.raises(sqlite3.OperationalError) as excinfo:
        await db.store_net_audit("seed", 104.0, [_event(104.0)], [])
    assert_busy_snapshot(excinfo)
    await cursor.close()

    probe = await aiosqlite.connect(str(tmp_path / "t.db"))
    try:
        cur = await probe.execute("PRAGMA wal_checkpoint(PASSIVE)")
        busy, log, checkpointed = await cur.fetchone()
        await cur.close()
    finally:
        await probe.close()
    assert busy == 0 and checkpointed == log, (busy, log, checkpointed)


# ── 2. both remedies are load-bearing ────────────────


async def test_sqlite_needs_both_remedies(tmp_path):
    # Premise check, on raw connections only — pins the SQLite behaviour the
    # boundary's design rests on (Phase 0 CP-1-RECOVER-*). No product code here.
    path = str(tmp_path / "raw.db")
    a = await aiosqlite.connect(path, isolation_level=None)
    b = await aiosqlite.connect(path, isolation_level=None)
    try:
        for c in (a, b):
            await c.execute("PRAGMA journal_mode=WAL")
            await c.execute("PRAGMA busy_timeout=5000")
        await a.execute("CREATE TABLE t (v INTEGER)")
        for i in range(5):
            await a.execute("INSERT INTO t VALUES (?)", (i,))

        reader = await a.execute("SELECT v FROM t")
        await reader.fetchone()
        await b.execute("BEGIN IMMEDIATE")
        await b.execute("INSERT INTO t VALUES (99)")
        await b.commit()

        with pytest.raises(sqlite3.OperationalError) as excinfo:
            await a.execute("BEGIN IMMEDIATE")   # SQLITE_BUSY_SNAPSHOT

        assert_busy_snapshot(excinfo)
        await a.rollback()                        # rollback alone
        with pytest.raises(sqlite3.OperationalError) as excinfo:
            await a.execute("BEGIN IMMEDIATE")

        assert_busy_snapshot(excinfo)
        await reader.close()                      # …and now the cursor too
        await a.execute("BEGIN IMMEDIATE")        # recovered
        await a.rollback()
    finally:
        await a.close()
        await b.close()


async def test_boundary_closes_every_cursor_it_opens(fresh_db):
    # Regression net for the cursor half of the remedy: if the boundary stops
    # closing its cursors, this fails.
    shared = await db._get_conn()
    opened, closed = [], []
    real_execute = shared.execute

    async def tracking_execute(sql, *args, **kwargs):
        cursor = await real_execute(sql, *args, **kwargs)
        opened.append(cursor)
        real_close = cursor.close

        async def close_and_record():
            closed.append(cursor)
            return await real_close()

        cursor.close = close_and_record
        return cursor

    shared.execute = tracking_execute
    try:
        await db.store_net_audit("a1", 100.0, [_event()], [])
        assert opened, "no statement ran"
        assert set(map(id, opened)) == set(map(id, closed)), "boundary leaked a cursor"

        opened.clear()
        closed.clear()
        with pytest.raises(ValueError):
            async with db.write_transaction() as tx:
                await tx.execute(
                    "INSERT INTO audit_log (timestamp, user_email, action) VALUES (?,?,?)",
                    (1.0, "u", "a"),
                )
                raise ValueError("body blew up")
        assert opened, "no statement ran"
        assert set(map(id, opened)) == set(map(id, closed)), (
            "boundary leaked a cursor on the failure path"
        )
    finally:
        del shared.execute


async def test_boundary_rolls_back_on_body_failure(fresh_db):
    # Regression net for the rollback half of the remedy.
    with pytest.raises(ValueError):
        async with db.write_transaction() as tx:
            await tx.execute(
                "INSERT INTO audit_log (timestamp, user_email, action) VALUES (?,?,?)",
                (1.0, "u", "a"),
            )
            raise ValueError("body blew up")
    shared = await db._get_conn()
    assert shared.in_transaction is False
    assert await db.get_audit_log() == []


# ── 3. every write path, not just store_metric ───────


async def _seed_for_write_paths():
    now = time.time()
    await db.store_metric("a1", now - 600, _metric(5))
    await db.store_net_audit("a1", now - 600, [_event(now - 600)], [])
    await db.create_user("u@x", "hash")
    await db.audit("u@x", "seed")
    await db.blacklist_token("tok", now + 60)
    return now


def _write_path_calls(now):
    return {
        "store_metric": lambda: db.store_metric("a2", now, _metric(1)),
        "store_net_audit": lambda: db.store_net_audit("a2", now, [_event(now)], [{"ts": now}]),
        "downsample_metrics": lambda: db.downsample_metrics(60, "1m"),
        "cleanup_old_metrics": lambda: db.cleanup_old_metrics(1),
        "cleanup_net_audit": lambda: db.cleanup_net_audit(7, 30),
        "cleanup_blacklist": lambda: db.cleanup_blacklist(),
        "cleanup_audit_log": lambda: db.cleanup_audit_log(90, 10),
        "audit": lambda: db.audit("u@x", "act"),
        "create_user": lambda: db.create_user("n@x", "hash"),
        "update_user": lambda: db.update_user("u@x", role="admin"),
        "delete_user": lambda: db.delete_user("u@x"),
        "set_user_host_accounts": lambda: db.set_user_host_accounts("u@x", {"a1": "root"}),
        "set_runtime_config": lambda: db.set_runtime_config("enable_gpu", "true"),
        "blacklist_token": lambda: db.blacklist_token("tok2", now + 60),
        "init_db": lambda: db.init_db(),
        # These two were previously added to the inventory as bare strings; a
        # name in a set proves nothing, so they are driven for real here.
        "set_runtime_configs": lambda: db.set_runtime_configs(
            {"enable_gpu": "true", "enable_docker": "false"}),
        "save_smtp_config": lambda: _save_smtp_config({"host": "h", "port": 25}),
    }


async def _save_smtp_config(config):
    from app.services import alert_service
    return await alert_service.save_smtp_config(config)


# ═══ Canonical write-path inventory ═══
#
# The single source of truth for "every mutating path in the product". A test
# below reconciles it against what the AST actually finds in database.py and
# alert_service.py, so a new write path cannot be added without appearing here.
CANONICAL_WRITE_PATHS = {
    # module          function                    connection
    ("database",      "init_db"):                 "shared",
    ("database",      "store_metric"):            "metric",
    ("database",      "downsample_metrics"):      "shared",
    ("database",      "cleanup_old_metrics"):     "shared",
    ("database",      "store_net_audit"):         "shared",
    ("database",      "cleanup_net_audit"):       "shared",
    ("database",      "create_user"):             "shared",
    ("database",      "delete_user"):             "shared",
    ("database",      "update_user"):             "shared",
    ("database",      "set_user_host_accounts"):  "shared",
    ("database",      "set_runtime_config"):      "shared",
    ("database",      "set_runtime_configs"):     "shared",   # loops the singular
    ("database",      "blacklist_token"):         "shared",
    ("database",      "cleanup_blacklist"):       "shared",
    ("database",      "audit"):                   "shared",
    ("database",      "cleanup_audit_log"):       "shared",
    ("alert_service", "save_smtp_config"):        "shared",   # SMTP read/merge/write
}


def test_canonical_write_path_inventory_matches_the_code():
    # Reconcile the inventory against the code, in BOTH directions: a new
    # mutating function that nobody listed, and a listed name that no longer
    # exists, both fail here.
    found = set()
    for module, path in (("database", DATABASE_PY),
                         ("alert_service", DATABASE_PY.parent / "services" / "alert_service.py")):
        tree = ast.parse(path.read_text())
        for node in tree.body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if node.name in _RAW_CONNECTION_ALLOWLIST or _takes_open_handle(node):
                continue
            calls = {n.func.id for n in ast.walk(node)
                     if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
            sql = [n.value.strip().upper() for n in ast.walk(node)
                   if isinstance(n, ast.Constant) and isinstance(n.value, str)]
            mutates = any(t.startswith(_MUTATING_SQL) for t in sql)
            if mutates or "write_transaction" in calls or "set_runtime_config" in calls:
                found.add((module, node.name))

    listed = set(CANONICAL_WRITE_PATHS)
    assert found == listed, (
        f"write-path inventory drifted.\n  missing from inventory: {sorted(found - listed)}"
        f"\n  listed but not found:   {sorted(listed - found)}"
    )
    # 17, not 16: an earlier count listed the 15 mutating functions in
    # database.py plus save_smtp_config, but omitted set_runtime_configs, which
    # mutates by delegating to set_runtime_config in a loop.
    assert len(listed) == 17, f"expected 17 write paths, inventory has {len(listed)}"
    by_conn = {}
    for (_, name), conn in CANONICAL_WRITE_PATHS.items():
        by_conn.setdefault(conn, []).append(name)
    assert by_conn["metric"] == ["store_metric"], by_conn["metric"]


def test_every_canonical_write_path_is_actually_driven():
    # An inventory nobody drives is documentation, not a net — and a name added
    # to a set by hand is not "driven". Every listed path must have a real
    # callable in _write_path_calls that the fault-injection tests invoke.
    driven = set(_write_path_calls(0.0))
    listed = {name for _, name in CANONICAL_WRITE_PATHS}
    assert listed == driven, (
        f"\n  listed but not driven: {sorted(listed - driven)}"
        f"\n  driven but not listed: {sorted(driven - listed)}"
    )
    assert len(driven) == 17, f"expected 17 driven write paths, got {len(driven)}"


async def test_every_write_path_reaches_its_injected_fault(fresh_db):
    # One consolidated run that proves the fault-injection harness actually
    # reaches EVERY path, with a per-path hit count — not just that the
    # parametrised tests happened to pass.
    hits: dict[str, int] = {}
    for path in WRITE_PATHS:
        monkeypatch = pytest.MonkeyPatch()
        try:
            now = await _seed_for_write_paths()
            shared = await db._get_conn()
            metric = await db._get_metric_db()
            counter = []

            async def failing_commit():
                counter.append(1)
                raise RuntimeError("commit failed")

            monkeypatch.setattr(shared, "commit", failing_commit, raising=False)
            monkeypatch.setattr(metric, "commit", failing_commit, raising=False)
            try:
                await _write_path_calls(now)[path]()
            except BaseException:
                pass
            hits[path] = len(counter)
        finally:
            monkeypatch.undo()
            for name in ("_conn", "_metric_conn"):
                conn = getattr(db, name)
                if conn is not None:
                    try:
                        await asyncio.wait_for(conn.close(), timeout=5)
                    except BaseException:
                        pass
                setattr(db, name, None)
            db._fail_stop = None
            db._unresolved.clear()
            db._unclosed.clear()

    missed = sorted(p for p, n in hits.items() if n == 0)
    print("\nfault-hit per write path:")
    for name in sorted(hits):
        print(f"  {name:<26} {hits[name]}")
    assert not missed, f"these write paths never reached the injected fault: {missed}"
    assert len(hits) == 17, hits
    assert sum(hits.values()) >= 17


WRITE_PATHS = sorted(_write_path_calls(0.0))


@pytest.mark.parametrize("path", WRITE_PATHS)
async def test_write_path_leaves_no_open_transaction_when_commit_fails(fresh_db, path):
    # Commit failure is the generic mid-transaction fault. Whatever a path does
    # with the error (raise, swallow, return False), it must not hand the
    # connection back with a transaction still open — that is the state Phase 0
    # showed cascading into every later write.
    now = await _seed_for_write_paths()
    shared = await db._get_conn()
    metric = await db._get_metric_db()

    hit = []

    async def failing_commit():
        hit.append(1)
        raise RuntimeError("commit failed")

    # A local MonkeyPatch: undoing the shared `monkeypatch` fixture here would
    # also revert fresh_db's _db_path patch and point the rest of the test at
    # the real data/ DB.
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(shared, "commit", failing_commit, raising=False)
        mp.setattr(metric, "commit", failing_commit, raising=False)
        try:
            await _write_path_calls(now)[path]()
        except BaseException:
            pass

    assert hit, f"{path}: the commit fault was never reached"
    assert shared.in_transaction is False, f"{path} left the shared transaction open"
    assert metric.in_transaction is False, f"{path} left the metric transaction open"


@pytest.mark.parametrize("path", ["audit", "create_user"])
async def test_swallowing_write_paths_still_clean_up(fresh_db, path):
    # audit() and create_user() deliberately never raise. That must not mean
    # they never clean up.
    now = await _seed_for_write_paths()
    shared = await db._get_conn()

    hit = []

    async def failing_commit():
        hit.append(1)
        raise RuntimeError("commit failed")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(shared, "commit", failing_commit, raising=False)
        await _write_path_calls(now)[path]()
    assert hit, f"{path}: the commit fault was never reached"
    assert shared.in_transaction is False


@pytest.mark.parametrize("path", WRITE_PATHS)
async def test_write_path_recovers_after_a_failed_commit(fresh_db, path):
    # The point of the cleanup: the NEXT write on the same connection works.
    now = await _seed_for_write_paths()
    shared = await db._get_conn()
    metric = await db._get_metric_db()

    async def failing_commit():
        raise RuntimeError("commit failed")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(shared, "commit", failing_commit, raising=False)
        mp.setattr(metric, "commit", failing_commit, raising=False)
        try:
            await _write_path_calls(now)[path]()
        except BaseException:
            pass

    await db.store_metric("recover", 500.0, _metric(1))
    await db.store_net_audit("recover", 500.0, [_event(500.0)], [])
    assert len(await db.get_net_conn_events("recover")) == 1


# ── 4. one operation lock across both connections ────


async def test_one_lock_covers_shared_and_metric_connections(fresh_db):
    # A metric-connection commit landing between a shared read and a shared
    # write is exactly the CP-1 trigger. One lock across both connections makes
    # that interleaving impossible.
    await db.store_metric("a1", 100.0, _metric())
    metric = await db._get_metric_db()
    shared = await db._get_conn()

    inserts = []
    real_metric_execute = metric.execute

    async def recording_execute(sql, *args, **kwargs):
        if isinstance(sql, str) and sql.startswith("INSERT INTO metrics"):
            inserts.append(sql)
        return await real_metric_execute(sql, *args, **kwargs)

    metric.execute = recording_execute

    in_commit = asyncio.Event()
    release = asyncio.Event()
    real_commit = shared.commit

    async def stalled_commit():
        in_commit.set()
        await release.wait()
        return await real_commit()

    shared.commit = stalled_commit
    writer = storer = None
    try:
        writer = asyncio.create_task(db.store_net_audit("a1", 100.0, [_event()], []))
        await asyncio.wait_for(in_commit.wait(), timeout=5)
        storer = asyncio.create_task(db.store_metric("a1", 101.0, _metric(2)))
        for _ in range(10):
            await asyncio.sleep(0)
        assert inserts == [], "a metric INSERT ran while a shared write held the boundary"
    finally:
        del shared.commit
        del metric.execute
        release.set()
        await asyncio.wait_for(
            asyncio.gather(*(t for t in (writer, storer) if t is not None),
                           return_exceptions=True),
            timeout=5,
        )


async def test_read_then_write_path_is_one_operation(fresh_db):
    # downsample_metrics reads raw rows and then writes buckets derived from
    # them. The whole read→write span must sit inside one boundary, so nothing
    # can commit between the two.
    now = time.time()
    await db.store_metric("a1", now - 600, _metric(5))
    shared = await db._get_conn()

    reached_write = asyncio.Event()
    release = asyncio.Event()
    real_execute = shared.execute

    async def gated_execute(sql, *args, **kwargs):
        if isinstance(sql, str) and sql.startswith("INSERT OR REPLACE INTO metrics_downsampled"):
            reached_write.set()
            await release.wait()
        return await real_execute(sql, *args, **kwargs)

    shared.execute = gated_execute
    metric = await db._get_metric_db()
    inserts = []
    real_metric_execute = metric.execute

    async def recording_execute(sql, *args, **kwargs):
        if isinstance(sql, str) and sql.startswith("INSERT INTO metrics"):
            inserts.append(sql)
        return await real_metric_execute(sql, *args, **kwargs)

    metric.execute = recording_execute
    ds = storer = None
    try:
        ds = asyncio.create_task(db.downsample_metrics(60, "1m"))
        await asyncio.wait_for(reached_write.wait(), timeout=5)
        storer = asyncio.create_task(db.store_metric("a1", now, _metric(6)))
        for _ in range(10):
            await asyncio.sleep(0)
        assert inserts == [], "a metric commit interleaved inside a read-then-write span"
    finally:
        del shared.execute
        del metric.execute
        release.set()
        await asyncio.wait_for(
            asyncio.gather(*(t for t in (ds, storer) if t is not None),
                           return_exceptions=True),
            timeout=5,
        )


async def test_nested_write_transaction_is_rejected_not_deadlocked(fresh_db):
    async def nested():
        async with db.write_transaction():
            async with db.write_transaction():
                pass

    with pytest.raises(RuntimeError):
        await asyncio.wait_for(nested(), timeout=5)


# ── 5. explicit BEGIN IMMEDIATE ──────────────────────


async def test_writes_open_with_begin_immediate(fresh_db):
    statements = []
    shared = await db._get_conn()
    metric = await db._get_metric_db()
    for conn in (shared, metric):
        real = conn.execute

        async def recording(sql, *args, _real=real, **kwargs):
            if isinstance(sql, str):
                statements.append(sql.strip().upper())
            return await _real(sql, *args, **kwargs)

        conn.execute = recording
    try:
        await db.store_net_audit("a1", 100.0, [_event()], [])
        await db.store_metric("a1", 100.0, _metric())
    finally:
        del shared.execute
        del metric.execute

    assert statements.count("BEGIN IMMEDIATE") == 2, statements


# ── 6. cancellation at each of the four points ───────

async def _occupy_worker(conn):
    """Occupy the connection's aiosqlite worker thread with a real statement.

    While it is held, the NEXT operation submitted on this connection is truly
    QUEUED to the worker — `conn._tx` grows — rather than merely wrapped on the
    event loop. That is the only way to reach the failure under test: a caller
    cancelled while the worker still owns its statement, so whatever the worker
    produces afterwards (a cursor, an opened transaction, a COMMIT) is dropped."""
    probe = WorkerProbe(f"occupy_{id(conn)}")
    await probe.install(conn)
    holder = asyncio.create_task(_run_probe(conn, probe.name))
    await probe.wait_entered()
    return probe, holder


async def _run_probe(conn, name):
    cursor = await conn.execute(f"SELECT {name}(1)")
    await cursor.close()


async def _assert_submitted_to_worker(conn, what):
    """Prove the pending operation really reached the worker's queue."""
    for _ in range(50):
        if conn._tx.qsize() >= 1:
            return
        await asyncio.sleep(0)
    pytest.fail(f"{what} was never submitted to the aiosqlite worker")


@pytest.mark.parametrize("point", ["begin", "body", "commit"])
async def test_cancellation_after_worker_submission_terminates_transaction(
    fresh_db, point
):
    # Cancellation AFTER the statement reached the worker. The worker finishes
    # it regardless, so the boundary must recover the outcome, undo it, and only
    # then hand the caller back its CancelledError.
    conn = await db._get_conn()
    if point == "begin":
        probe, holder = await _occupy_worker(conn)
        task = asyncio.create_task(db.store_net_audit("a1", 1.0, [_event()], []))
        await _assert_submitted_to_worker(conn, "BEGIN IMMEDIATE")
    elif point == "body":
        async with db.write_transaction() as warmup:   # keep BEGIN out of the way
            await warmup.execute("SELECT 1")
        probe, holder = await _occupy_worker(conn)
        task = asyncio.create_task(db.store_net_audit("a1", 1.0, [_event()], []))
        await _assert_submitted_to_worker(conn, "the write's first statement")
    else:
        real_commit = conn.commit
        entered = asyncio.Event()

        async def commit_after_worker_started():
            entered.set()
            return await real_commit()

        conn.commit = commit_after_worker_started
        probe, holder = None, None
        task = asyncio.create_task(db.store_net_audit("a1", 1.0, [_event()], []))
        await asyncio.wait_for(entered.wait(), timeout=5)

    task.cancel()
    if probe is not None:
        probe.let_go()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=15)
    if holder is not None:
        await asyncio.gather(holder, return_exceptions=True)
    if point == "commit":
        del conn.commit

    assert conn.in_transaction is False, "lock released over an open transaction"
    assert db._fail_stop is None, db._fail_stop
    await db.store_net_audit("a1", 2.0, [_event(2.0)], [])
    assert await db.get_net_conn_events("a1")


async def test_commit_that_never_resolves_fails_stop(fresh_db, monkeypatch):
    # If the worker's outcome cannot be established at all, the boundary must
    # NOT guess. Submitting a ROLLBACK could discard a durable commit, so it
    # stops the DB instead — and does so within a bound.
    monkeypatch.setattr(db, "_CLEANUP_TIMEOUT", 0.2)
    monkeypatch.setattr(db, "_CLEANUP_CANCEL_BUDGET", 1)
    conn = await db._get_conn()
    state, release = await _commit_blocked_in_worker(conn)
    try:
        with pytest.raises(db.DatabaseFailStop):
            await asyncio.wait_for(
                db.store_net_audit("a1", 1.0, [_event()], []), timeout=15)
        assert state.get("submitted"), "COMMIT never reached the worker queue"
        assert db._fail_stop is not None
    finally:
        await release()


async def test_cancellation_during_rollback_completes_the_rollback(fresh_db):
    # Synchronised on the rollback actually reaching the worker queue, not on a
    # number of scheduler turns.
    conn = await db._get_conn()
    rollback_submitted = asyncio.Event()
    real_rollback = conn.rollback
    completed = []

    async def observed_rollback():
        rollback_submitted.set()
        result = await real_rollback()
        completed.append("rollback")
        return result

    async def failing_commit():
        raise RuntimeError("commit failed")

    conn.rollback = observed_rollback
    conn.commit = failing_commit
    task = None
    try:
        task = asyncio.create_task(db.store_net_audit("a1", 1.0, [_event()], []))
        await asyncio.wait_for(rollback_submitted.wait(), timeout=10)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    finally:
        del conn.rollback
        del conn.commit

    assert completed == ["rollback"], "rollback was abandoned half-done"
    assert conn.in_transaction is False


# ── 7. fail-stop when an outcome cannot be established ──


async def test_unconfirmable_rollback_refuses_later_writes(fresh_db):
    # Requirement: do NOT swap the connection and keep writing. A rollback whose
    # outcome cannot be established leaves the pending statement's fate unknown;
    # the DB goes restart-required and later writes are refused explicitly.
    metric = await db._get_metric_db()

    async def failing_commit():
        raise RuntimeError("commit failed")

    async def failing_rollback():
        raise RuntimeError("rollback failed")

    metric.commit = failing_commit
    metric.rollback = failing_rollback
    try:
        with pytest.raises(BaseException):
            await asyncio.wait_for(db.store_metric("a1", 100.0, _metric()), timeout=5)

        assert db._metric_conn is metric, "connection was swapped instead of failing stop"
        assert db._fail_stop is not None

        with pytest.raises(db.DatabaseFailStop):
            await asyncio.wait_for(db.store_metric("a1", 101.0, _metric(2)), timeout=5)
        with pytest.raises(db.DatabaseFailStop):
            await asyncio.wait_for(
                db.store_net_audit("a1", 101.0, [_event(101.0)], []), timeout=5)
    finally:
        del metric.commit
        del metric.rollback


async def test_fail_stop_blocks_writes_on_the_other_connection_too(fresh_db):
    # One DB file, one verdict: a metric-connection failure must stop shared
    # writes as well, or the same file keeps being written from the other side.
    shared = await db._get_conn()

    async def failing_commit():
        raise RuntimeError("commit failed")

    async def failing_rollback():
        raise RuntimeError("rollback failed")

    shared.commit = failing_commit
    shared.rollback = failing_rollback
    try:
        with pytest.raises(BaseException):
            await asyncio.wait_for(
                db.store_net_audit("a1", 100.0, [_event()], []), timeout=5)
        assert db._conn is shared
        with pytest.raises(db.DatabaseFailStop):
            await asyncio.wait_for(db.store_metric("a1", 100.0, _metric()), timeout=5)
    finally:
        del shared.commit
        del shared.rollback


async def test_fail_stop_refuses_writes_and_reads_alike(fresh_db):
    # Contract change: fail-stop used to refuse only WRITES, on the theory that
    # reads are harmless. They are not — a read goes through the same worker on
    # the same connection, so admitting one after a terminal outcome went
    # unknown keeps using a connection whose state nobody can vouch for (and can
    # hand back rows from a transaction whose fate is undecided).
    await db.store_metric("a1", 100.0, _metric())
    db._fail_stop = "test"
    with pytest.raises(db.DatabaseFailStop):
        await db.get_recent_metrics("a1")
    with pytest.raises(db.DatabaseFailStop):
        await db.store_metric("a1", 101.0, _metric(2))


async def test_fail_stop_error_names_the_reason(fresh_db):
    db._fail_stop = "rollback outcome unknown"
    with pytest.raises(db.DatabaseFailStop, match="rollback outcome unknown"):
        await db.blacklist_token("t", 1.0)


async def test_fail_stop_reason_is_observable(fresh_db):
    # audit() swallows Exception by contract, so a caller that only sees a
    # best-effort write needs another way to learn the DB is refusing writes.
    assert db.db_fail_stop_reason() is None
    db._fail_stop = "rollback outcome unknown"
    assert "rollback outcome unknown" in db.db_fail_stop_reason()


async def test_unconfirmable_cursor_close_also_fails_stop(fresh_db):
    # A cursor whose close cannot be confirmed pins a read snapshot just like a
    # failed transaction does (Phase 0 CP-1-RECOVER-CLOSE) — same verdict.
    shared = await db._get_conn()
    real_execute = shared.execute

    async def poisoned_execute(sql, *args, **kwargs):
        cursor = await real_execute(sql, *args, **kwargs)

        async def failing_close():
            raise RuntimeError("cursor close failed")

        cursor.close = failing_close
        return cursor

    shared.execute = poisoned_execute
    try:
        # The immediate caller must be told the DB is restart-required, not
        # handed the raw close error — otherwise a caller that distinguishes
        # DatabaseFailStop from ordinary failures cannot do so.
        with pytest.raises(db.DatabaseFailStop):
            await asyncio.wait_for(
                db.store_net_audit("a1", 100.0, [_event()], []), timeout=5)
        assert db._fail_stop is not None
    finally:
        del shared.execute


# ── 8. every mutating path goes through the helper ───


_MUTATING_SQL = ("INSERT", "UPDATE ", "DELETE ", "REPLACE", "CREATE ", "ALTER ", "DROP ")

# The only functions allowed to touch a raw connection: the boundary itself and
# connection lifecycle. Everything else must go through write_transaction.
_RAW_CONNECTION_ALLOWLIST = {
    "_write_transaction",
    "_commit_transaction",
    "_abort_transaction",
    "_open_connection",
    "_discard_connection",
    "_run_statement",
    "_fetch_all_unrestricted",
    "_transaction_state",
    "_reclassify",
    "_cleanup_cursors",
    "_close_cursor",
    "_rollback",
    "_configure",
    "_fetch_all",
    "_fetch_one",
    "_get_conn",
    "_get_metric_db",
    "close_db",
    "execute",       # _TxHandle.execute
    "executemany",   # _TxHandle.executemany
    "fetch_all",     # _TxHandle.fetch_all
    "fetch_one",     # _TxHandle.fetch_one
}

# Statements inside a boundary go through the handle, which by convention is
# always bound to the name `tx`. Anything else receiving .execute()/.commit()
# is a raw connection.
_HANDLE_NAME = "tx"


def _takes_open_handle(node) -> bool:
    """True for a helper that mutates inside a boundary its CALLER opened.

    Such a helper takes the open handle as its first parameter (named `tx` by
    the same convention as above), so it cannot run outside a transaction: it
    has nothing to execute against until a write_transaction hands it one.
    Requiring it to open its own boundary would mean nesting the DB-file
    operation lock, and listing it as a canonical write path would claim it is
    independently drivable, which it is not.

    This is NOT an escape hatch: the raw-connection guard still applies in
    full, so a helper exempted here can only ever touch `tx`. A mutating
    function that does not take an open handle stays subject to both guards.
    """
    args = node.args.posonlyargs + node.args.args
    return bool(args) and args[0].arg == _HANDLE_NAME


def _enclosing_functions(tree):
    """Map every node to the name of the function that lexically contains it."""
    owner = {}

    def walk(node, current):
        for child in ast.iter_child_nodes(node):
            name = current
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                name = child.name
            owner[child] = name
            walk(child, name)

    walk(tree, "<module>")
    return owner


def test_no_raw_connection_calls_outside_the_boundary():
    tree = ast.parse(DATABASE_PY.read_text())
    owner = _enclosing_functions(tree)
    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr not in ("execute", "executemany", "commit", "rollback"):
            continue
        receiver = node.func.value
        if isinstance(receiver, ast.Name) and receiver.id == _HANDLE_NAME:
            continue          # the boundary's own handle, not a raw connection
        fn = owner.get(node, "<module>")
        if fn in _RAW_CONNECTION_ALLOWLIST:
            continue
        offenders.append(f"{fn}() line {node.lineno}: .{node.func.attr}()")
    assert not offenders, (
        "raw connection calls outside the write_transaction boundary:\n  "
        + "\n  ".join(offenders)
    )


def test_every_mutating_function_uses_write_transaction():
    tree = ast.parse(DATABASE_PY.read_text())
    missing = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name in _RAW_CONNECTION_ALLOWLIST or _takes_open_handle(node):
            continue
        sql = [
            n.value.strip().upper()
            for n in ast.walk(node)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)
        ]
        if not any(s.startswith(_MUTATING_SQL) for s in sql):
            continue
        uses_helper = any(
            isinstance(n, ast.Call)
            and isinstance(n.func, (ast.Name, ast.Attribute))
            and getattr(n.func, "id", getattr(n.func, "attr", None)) == "write_transaction"
            for n in ast.walk(node)
        )
        # A function may instead delegate wholly to another public write.
        delegates = any(
            isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
            and n.func.id in {"set_runtime_config"}
            for n in ast.walk(node)
        )
        if not (uses_helper or delegates):
            missing.append(f"{node.name}() line {node.lineno}")
    assert not missing, (
        "mutating functions that do not open a write_transaction:\n  "
        + "\n  ".join(missing)
    )


def test_write_transaction_and_fail_stop_are_public():
    assert hasattr(db, "write_transaction")
    assert hasattr(db, "DatabaseFailStop")
    assert issubclass(db.DatabaseFailStop, Exception)


async def test_tx_handle_does_not_leak_a_raw_cursor(fresh_db):
    # A handle that hands out a live cursor would let a caller re-create the
    # exact Phase 0 trigger from inside the boundary.
    async with db.write_transaction() as tx:
        result = await tx.execute(
            "INSERT INTO audit_log (timestamp, user_email, action) VALUES (?,?,?)",
            (1.0, "u", "a"),
        )
    assert not hasattr(result, "fetchone")
    assert not hasattr(result, "close")
    assert isinstance(result.lastrowid, int)
    assert result.rowcount == 1


async def test_tx_handle_is_rejected_after_the_transaction_ends(fresh_db):
    async with db.write_transaction() as tx:
        pass
    with pytest.raises(RuntimeError):
        await tx.execute(
            "INSERT INTO audit_log (timestamp, user_email, action) VALUES (1,'u','a')")


# ── 9. production code outside database.py ───────────


def test_no_production_module_uses_a_raw_connection():
    # The raw-connection accessor is module-private; anything holding a raw
    # connection can leave a cursor or a transaction open outside the boundary,
    # which is exactly the Phase 0 trigger.
    root = DATABASE_PY.parent
    offenders = []
    for path in sorted(root.rglob("*.py")):
        if path == DATABASE_PY:
            continue
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
                    and node.func.id in ("get_db", "_get_conn"):
                offenders.append(f"{path.relative_to(root)}:{node.lineno} {node.func.id}()")
    assert not offenders, (
        "production code holding a raw DB connection:\n  " + "\n  ".join(offenders)
    )


async def test_smtp_config_read_then_write_is_one_operation(fresh_db):
    # save_smtp_config reads the stored row, merges the encrypted password
    # marker into it, and writes it back. A commit landing between the read and
    # the write is the CP-1 shape, and the merge would also be based on a row
    # that no longer exists.
    from app.services import alert_service

    shared = await db._get_conn()
    await alert_service.save_smtp_config({"host": "a", "port": 25, "password": "pw"})

    reached_write = asyncio.Event()
    release = asyncio.Event()
    real_execute = shared.execute

    async def gated_execute(sql, *args, **kwargs):
        if isinstance(sql, str) and sql.startswith("INSERT OR REPLACE INTO alert_config"):
            reached_write.set()
            await release.wait()
        return await real_execute(sql, *args, **kwargs)

    shared.execute = gated_execute
    metric = await db._get_metric_db()
    inserts = []
    real_metric_execute = metric.execute

    async def recording_execute(sql, *args, **kwargs):
        if isinstance(sql, str) and sql.startswith("INSERT INTO metrics"):
            inserts.append(sql)
        return await real_metric_execute(sql, *args, **kwargs)

    metric.execute = recording_execute
    saver = storer = None
    try:
        saver = asyncio.create_task(
            alert_service.save_smtp_config({"host": "b", "port": 25}))
        await asyncio.wait_for(reached_write.wait(), timeout=5)
        storer = asyncio.create_task(db.store_metric("a1", 100.0, _metric()))
        for _ in range(10):
            await asyncio.sleep(0)
        assert inserts == [], "a commit interleaved inside save_smtp_config's read→write"
    finally:
        del shared.execute
        del metric.execute
        release.set()
        await asyncio.wait_for(
            asyncio.gather(*(t for t in (saver, storer) if t is not None),
                           return_exceptions=True),
            timeout=5,
        )


async def test_smtp_config_read_closes_its_cursor(fresh_db):
    from app.services import alert_service

    shared = await db._get_conn()
    opened, closed = [], []
    real_execute = shared.execute

    async def tracking_execute(sql, *args, **kwargs):
        cursor = await real_execute(sql, *args, **kwargs)
        opened.append(cursor)
        real_close = cursor.close

        async def close_and_record():
            closed.append(cursor)
            return await real_close()

        cursor.close = close_and_record
        return cursor

    shared.execute = tracking_execute
    try:
        alert_service._invalidate_config_cache()
        await alert_service.get_smtp_config()
        assert opened, "no statement ran"
        assert set(map(id, opened)) == set(map(id, closed)), (
            "get_smtp_config left a cursor open"
        )
    finally:
        del shared.execute


async def test_save_smtp_config_leaves_no_open_transaction_when_commit_fails(fresh_db):
    from app.services import alert_service

    shared = await db._get_conn()

    async def failing_commit():
        raise RuntimeError("commit failed")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(shared, "commit", failing_commit, raising=False)
        try:
            await alert_service.save_smtp_config({"host": "a", "port": 25})
        except BaseException:
            pass
    assert shared.in_transaction is False


# ── 10. fail-stop reaches the caller with the right type ──


def _poison_cursor_close(conn, sql_prefix):
    """Make the cursor for one statement fail to close — the same transient
    class of fault as a failing commit, but on the cursor half of the remedy."""
    real_execute = conn.execute

    async def poisoned(sql, *args, **kwargs):
        cursor = await real_execute(sql, *args, **kwargs)
        if isinstance(sql, str) and sql.startswith(sql_prefix):
            async def failing_close():
                raise RuntimeError("cursor close failed")
            cursor.close = failing_close
        return cursor

    conn.execute = poisoned


async def test_body_cursor_close_failure_surfaces_as_fail_stop(fresh_db):
    # The abort path must classify like the commit path: if its cleanup could
    # not be resolved, the caller gets DatabaseFailStop, not the raw error.
    shared = await db._get_conn()
    _poison_cursor_close(shared, "INSERT OR IGNORE INTO token_blacklist")
    try:
        with pytest.raises(db.DatabaseFailStop):
            await asyncio.wait_for(db.blacklist_token("t", 1.0), timeout=5)
    finally:
        del shared.execute


async def test_create_user_reports_fail_stop_rather_than_false(fresh_db):
    # create_user returns False for ordinary failures (duplicate email). A
    # restart-required DB is not that, and its `except DatabaseFailStop: raise`
    # guard must hold whichever half of the cleanup could not be resolved.
    shared = await db._get_conn()
    _poison_cursor_close(shared, "INSERT INTO users")
    try:
        with pytest.raises(db.DatabaseFailStop):
            await asyncio.wait_for(db.create_user("x@y", "hash"), timeout=5)
    finally:
        del shared.execute


async def test_internal_outcome_unknown_never_reaches_a_caller(fresh_db, monkeypatch):
    # _OutcomeUnknown is an internal classification; callers must only ever see
    # DatabaseFailStop so they can act on it.
    monkeypatch.setattr(db, "_CLEANUP_TIMEOUT", 0.05)
    monkeypatch.setattr(db, "_CLEANUP_CANCEL_BUDGET", 1)
    shared = await db._get_conn()
    real_execute = shared.execute
    block = asyncio.Event()  # never set

    async def hanging_close_execute(sql, *args, **kwargs):
        cursor = await real_execute(sql, *args, **kwargs)
        if isinstance(sql, str) and sql.startswith("INSERT OR IGNORE INTO token_blacklist"):
            async def hanging_close():
                await block.wait()
            cursor.close = hanging_close
        return cursor

    shared.execute = hanging_close_execute
    try:
        with pytest.raises(db.DatabaseFailStop):
            await asyncio.wait_for(db.blacklist_token("t", 1.0), timeout=10)
    finally:
        block.set()
        del shared.execute


async def test_cancellation_still_wins_over_fail_stop(fresh_db):
    # If a cancelled write's cleanup ALSO fails, the caller is still owed its
    # CancelledError — replacing it with DatabaseFailStop would break
    # cancellation semantics. The latch still refuses the NEXT write.
    shared = await db._get_conn()
    entered = asyncio.Event()
    real_executemany = shared.executemany

    async def slow_executemany(sql, seq):
        entered.set()
        await asyncio.sleep(0)
        return await real_executemany(sql, seq)

    async def failing_rollback():
        raise RuntimeError("rollback failed")

    shared.executemany = slow_executemany
    shared.rollback = failing_rollback
    task = None
    try:
        task = asyncio.create_task(db.store_net_audit("a1", 100.0, [_event()], []))
        await asyncio.wait_for(entered.wait(), timeout=5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=5)
        assert db._fail_stop is not None
    finally:
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        del shared.executemany
        del shared.rollback

    with pytest.raises(db.DatabaseFailStop):
        await db.store_metric("a1", 100.0, _metric())


# ═════════════════════════════════════════════════════
# Hardening round: real worker-submission faults
#
# The tests above park a wrapper BEFORE it calls the real driver method. That
# proves the caller unwinds, but it never submits anything to aiosqlite's
# worker thread — so it cannot reach the failure this section is about: the
# worker keeps running after its caller is cancelled, and whatever it produced
# (a cursor, a COMMIT) is dropped on the floor because the future is already
# done. Everything below drives the REAL worker and proves it entered.
# ═════════════════════════════════════════════════════

import sqlite3  # noqa: E402
import threading  # noqa: E402


class WorkerProbe:
    """A SQLite user function that proves the aiosqlite WORKER THREAD actually
    began executing a statement, and blocks it there until released.

    `entered` is set from inside the worker thread, so waiting on it is proof
    of submission — not of a wrapper having been called on the event loop."""

    def __init__(self, name="probe"):
        self.name = name
        self.entered = threading.Event()
        self.release = threading.Event()
        self.calls = 0

    def __call__(self, value=0):
        self.calls += 1
        self.entered.set()
        self.release.wait(timeout=30)
        return value

    async def install(self, conn):
        await conn.create_function(self.name, 1, self)

    async def wait_entered(self, timeout=5.0):
        await asyncio.wait_for(asyncio.to_thread(self.entered.wait, timeout),
                               timeout=timeout + 2)
        assert self.entered.is_set(), "aiosqlite worker never entered the statement"

    def let_go(self):
        self.release.set()


def _busy_snapshot_name(exc):
    """Exact SQLite error name, so a test cannot pass on an unrelated error."""
    return getattr(exc, "sqlite_errorname", None)


async def _advance_wal_from_another_connection(agent="wal"):
    """Commit on the metric connection so the shared connection's snapshot goes
    stale — the second half of the Phase 0 CP-1 trigger."""
    await db.store_metric(agent, 1.0, _metric())


# ── F1. read cancelled AFTER the worker started ──────


async def test_read_cancelled_after_worker_started_recovers_its_cursor(fresh_db):
    conn = await db._get_conn()
    probe = WorkerProbe()
    await probe.install(conn)
    await db.store_metric("seed", 1.0, _metric())

    task = asyncio.create_task(
        db._fetch_all("SELECT probe(id) AS v FROM metrics"))
    await probe.wait_entered()          # worker is INSIDE the statement
    task.cancel()
    probe.let_go()                      # the worker runs to completion regardless
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=10)
    assert probe.calls >= 1, "fault was never injected"

    # The cursor the worker produced was never handed to the caller. If it was
    # not recovered and closed, its snapshot is still pinned and the next write
    # after another connection commits fails with SQLITE_BUSY_SNAPSHOT.
    await _advance_wal_from_another_connection()
    await db.store_net_audit("a1", 1.0, [_event(1.0)], [])
    assert len(await db.get_net_conn_events("a1")) == 1


async def test_read_cancelled_after_worker_started_leaves_no_stale_snapshot(fresh_db):
    conn = await db._get_conn()
    probe = WorkerProbe()
    await probe.install(conn)
    await db.store_metric("seed", 1.0, _metric())

    task = asyncio.create_task(db._fetch_all("SELECT probe(id) AS v FROM metrics"))
    await probe.wait_entered()
    task.cancel()
    probe.let_go()                      # the worker runs to completion regardless
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=10)
    assert probe.calls >= 1, "fault was never injected"

    await _advance_wal_from_another_connection()
    try:
        cursor = await conn.execute("BEGIN IMMEDIATE")
        await cursor.close()
        await conn.rollback()
    except sqlite3.OperationalError as exc:  # pragma: no cover - the RED path
        pytest.fail(f"stale snapshot survived: {_busy_snapshot_name(exc)} {exc}")


# ── F2. the cleanup child itself is cancelled ────────


@pytest.mark.expect_db_leftovers
async def test_commit_child_cancelled_is_not_success(fresh_db):
    # A COMMIT submission that comes back CANCELLED proves nothing about
    # whether the COMMIT ran. Treating it as commit-wins releases the lock over
    # a possibly-open transaction — and the submission stays unresolved, because
    # nothing here can tell "the COMMIT never reached the worker" from "the
    # COMMIT is on the worker right now".
    conn = await db._get_conn()
    hit = []

    async def cancelled_commit():
        hit.append(1)
        raise asyncio.CancelledError()

    conn.commit = cancelled_commit
    try:
        with pytest.raises(db.DatabaseFailStop):
            await asyncio.wait_for(
                db.store_net_audit("a1", 1.0, [_event()], []), timeout=10)
    finally:
        del conn.commit
    assert hit, "fault was never injected"
    assert db._fail_stop is not None
    assert conn.in_transaction is False, "lock released over an open transaction"
    assert db._unresolved, (
        "a cancelled COMMIT submission was retired without any evidence about "
        "the worker that may still be running it")


@pytest.mark.expect_db_leftovers
async def test_rollback_child_cancelled_is_not_complete(fresh_db):
    conn = await db._get_conn()
    hit = []

    async def failing_commit():
        raise RuntimeError("commit failed")

    async def cancelled_rollback():
        hit.append(1)
        raise asyncio.CancelledError()

    conn.commit = failing_commit
    conn.rollback = cancelled_rollback
    try:
        with pytest.raises(db.DatabaseFailStop):
            await asyncio.wait_for(
                db.store_net_audit("a1", 1.0, [_event()], []), timeout=10)
    finally:
        del conn.commit
        del conn.rollback
    assert hit, "fault was never injected"
    assert db._fail_stop is not None


@pytest.mark.expect_db_leftovers
async def test_child_cancellation_blocks_all_later_admission(fresh_db):
    conn = await db._get_conn()

    async def cancelled_commit():
        raise asyncio.CancelledError()

    conn.commit = cancelled_commit
    try:
        with pytest.raises(db.DatabaseFailStop):
            await asyncio.wait_for(db.blacklist_token("t", 1.0), timeout=10)
    finally:
        del conn.commit
    with pytest.raises(db.DatabaseFailStop):
        await db.store_metric("a1", 1.0, _metric())
    with pytest.raises(db.DatabaseFailStop):
        await db.get_recent_metrics("a1")


# ── F3. commit response lost after a durable commit ──


async def test_commit_response_lost_after_durable_commit_fails_stop(fresh_db):
    # The COMMIT really ran; only its response was lost. Reporting an ordinary
    # failure and carrying on would let the caller retry a write that IS
    # durable, and would keep writing next to an unknown state.
    conn = await db._get_conn()
    real_commit = conn.commit
    hit = []

    async def commit_then_lose_response():
        await real_commit()          # genuinely durable
        hit.append(1)
        raise RuntimeError("response lost after COMMIT")

    conn.commit = commit_then_lose_response
    try:
        with pytest.raises(db.DatabaseFailStop):
            await asyncio.wait_for(
                db.store_net_audit("a1", 1.0, [_event()], []), timeout=10)
    finally:
        del conn.commit
    assert hit, "fault was never injected"
    # Every later DB operation is refused — no "saved but reported failed, and
    # the next write proceeds" window.
    with pytest.raises(db.DatabaseFailStop):
        await db.store_net_audit("a1", 2.0, [_event(2.0)], [])
    with pytest.raises(db.DatabaseFailStop):
        await db.get_net_conn_events("a1")


# ── F4. TxHandle used by a late child task ───────────


async def test_tx_handle_rejects_use_from_another_task(fresh_db):
    # A child task that passed tx's checks before the owner finished must not
    # be able to submit anything afterwards.
    gate = asyncio.Event()
    child_error = {}

    async def late_child(tx):
        await gate.wait()            # resumes AFTER the owner rolled back
        try:
            await tx.execute(
                "INSERT INTO audit_log (timestamp, user_email, action) VALUES (?,?,?)",
                (1.0, "late", "child"),
            )
            child_error["outcome"] = "submitted"
        except BaseException as exc:
            child_error["outcome"] = type(exc).__name__
            child_error["exc"] = exc

    child = None
    with pytest.raises(ValueError):
        async with db.write_transaction() as tx:
            child = asyncio.create_task(late_child(tx))
            await asyncio.sleep(0)   # child is parked on the gate
            raise ValueError("owner aborts")

    gate.set()
    await asyncio.wait_for(child, timeout=10)

    assert child_error["outcome"] != "submitted", "late child wrote through a dead handle"
    assert isinstance(child_error["exc"], db.TransactionHandleMisuse), child_error
    assert await db.get_audit_log() == []


async def test_tx_handle_rejects_concurrent_use_by_another_task(fresh_db):
    # Even while the owner transaction is still OPEN, another task must not
    # submit through the handle — two tasks interleaving statements inside one
    # transaction is the same uncontrolled-cursor shape.
    seen = {}

    async def other(tx):
        try:
            await tx.execute(
                "INSERT INTO audit_log (timestamp, user_email, action) VALUES (?,?,?)",
                (1.0, "other", "task"),
            )
            seen["outcome"] = "submitted"
        except BaseException as exc:
            seen["outcome"] = type(exc).__name__
            seen["exc"] = exc

    async with db.write_transaction() as tx:
        await asyncio.wait_for(asyncio.create_task(other(tx)), timeout=10)

    assert seen["outcome"] != "submitted"
    assert isinstance(seen["exc"], db.TransactionHandleMisuse), seen
    assert await db.get_audit_log() == []


# ── F5. unresolved worker ────────────────────────────


async def _commit_blocked_in_worker(conn):
    """Replace conn.commit with one that is genuinely stuck on the worker
    thread: a probe statement occupies the worker, so the COMMIT behind it is
    submitted and cannot run. Returns a release callable."""
    real_commit = conn.commit
    state = {}

    async def blocked_commit():
        state["hit"] = True
        probe, holder = await _occupy_worker(conn)
        state["probe"], state["holder"] = probe, holder
        # Submit the real COMMIT while the worker is occupied, then prove it is
        # sitting in the worker's queue — genuinely submitted, unable to run.
        inner = asyncio.ensure_future(real_commit())
        state["inner"] = inner
        await _assert_submitted_to_worker(conn, "COMMIT")
        state["submitted"] = True
        return await inner

    conn.commit = blocked_commit

    async def release():
        if "probe" in state:
            state["probe"].let_go()
            await asyncio.gather(state["holder"], return_exceptions=True)
        if "inner" in state:
            await asyncio.gather(state["inner"], return_exceptions=True)
        if "commit" in conn.__dict__:
            del conn.commit
        # Drain the submissions the boundary deliberately did NOT cancel, so the
        # test does not leave a worker writing into a closed event loop.
        pending = [entry.task for entry in db._unresolved]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        # Production leaves this connection open on purpose (restart-required
        # means the process goes away). The test has no process to end, so it
        # closes the connection itself rather than leaking a worker thread.
        try:
            await asyncio.wait_for(conn.close(), timeout=5)
        except BaseException:
            pass

    return state, release


@pytest.mark.expect_db_leftovers
async def test_unresolved_commit_worker_blocks_admission_and_close_is_bounded(
    fresh_db, monkeypatch
):
    monkeypatch.setattr(db, "_CLEANUP_TIMEOUT", 0.2)
    monkeypatch.setattr(db, "_CLEANUP_CANCEL_BUDGET", 1)
    conn = await db._get_conn()
    state, release = await _commit_blocked_in_worker(conn)
    try:
        with pytest.raises(db.DatabaseFailStop):
            await asyncio.wait_for(db.blacklist_token("t", 1.0), timeout=20)
        assert state.get("submitted"), "COMMIT never reached the worker queue"

        # No later operation may wait indefinitely, and none may quietly open a
        # replacement connection.
        before = db._conn
        with pytest.raises(db.DatabaseFailStop):
            await asyncio.wait_for(db.store_metric("a1", 1.0, _metric()), timeout=5)
        with pytest.raises(db.DatabaseFailStop):
            await asyncio.wait_for(db.get_recent_metrics("a1"), timeout=5)
        assert db._conn is before, "a replacement connection was opened"

        # Shutdown must be bounded and must report restart-required.
        verdict = await asyncio.wait_for(db.close_db(timeout=2), timeout=20)
        assert verdict is db.CloseVerdict.RESTART_REQUIRED, verdict
    finally:
        await release()


async def test_unresolved_worker_is_not_silently_forgotten(fresh_db, monkeypatch):
    monkeypatch.setattr(db, "_CLEANUP_TIMEOUT", 0.2)
    monkeypatch.setattr(db, "_CLEANUP_CANCEL_BUDGET", 1)
    conn = await db._get_conn()
    state, release = await _commit_blocked_in_worker(conn)
    try:
        with pytest.raises(db.DatabaseFailStop):
            await asyncio.wait_for(db.blacklist_token("t", 1.0), timeout=20)
        assert state.get("submitted"), "COMMIT never reached the worker queue"
        # The submission is tracked, not cancelled-and-dropped: cancelling the
        # future would not stop the worker, it would only hide the unknown.
        assert db._unresolved, "unresolved worker submission was forgotten"
        assert any("commit" in entry.what for entry in db._unresolved), db._unresolved
        assert not any(entry.task.cancelled() for entry in db._unresolved)
    finally:
        await release()


# ── F6. close_db races ───────────────────────────────


async def test_close_db_waits_for_an_active_transaction(fresh_db):
    conn = await db._get_conn()
    probe = WorkerProbe()
    await probe.install(conn)
    committed = asyncio.Event()

    async def slow_writer():
        async with db.write_transaction() as tx:
            await tx.execute(
                "INSERT INTO audit_log (timestamp, user_email, action) VALUES (?,?,?)",
                (1.0, "u", "a"),
            )
            await tx.fetch_all("SELECT probe(1)")   # parks INSIDE the worker
        committed.set()

    writer = asyncio.create_task(slow_writer())
    await probe.wait_entered()

    closer = asyncio.create_task(db.close_db())
    await asyncio.sleep(0)
    assert not closer.done(), "close_db cut into an in-flight transaction"
    assert not committed.is_set()

    probe.let_go()
    await asyncio.wait_for(writer, timeout=10)
    verdict = await asyncio.wait_for(closer, timeout=10)
    assert committed.is_set(), "close_db did not let the transaction finish"
    assert verdict is db.CloseVerdict.CLOSED, verdict


async def test_operations_during_close_are_refused_not_raced(fresh_db):
    conn = await db._get_conn()
    probe = WorkerProbe()
    await probe.install(conn)

    async def slow_writer():
        async with db.write_transaction() as tx:
            await tx.fetch_all("SELECT probe(1)")

    writer = asyncio.create_task(slow_writer())
    await probe.wait_entered()
    closer = asyncio.create_task(db.close_db())
    await asyncio.sleep(0)

    # A write/read arriving while close is pending is refused outright rather
    # than queued behind the lock onto a connection that is about to close.
    with pytest.raises(db.DatabaseFailStop):
        await asyncio.wait_for(db.store_metric("a1", 1.0, _metric()), timeout=5)
    with pytest.raises(db.DatabaseFailStop):
        await asyncio.wait_for(db.get_recent_metrics("a1"), timeout=5)

    probe.let_go()
    await asyncio.gather(writer, return_exceptions=True)
    await asyncio.wait_for(closer, timeout=10)


async def test_no_connection_is_published_after_close(fresh_db):
    await db.store_metric("a1", 1.0, _metric())
    assert await db.close_db() is db.CloseVerdict.CLOSED
    with pytest.raises(db.DatabaseFailStop):
        await db.store_metric("a1", 2.0, _metric())
    with pytest.raises(db.DatabaseFailStop):
        await db.get_recent_metrics("a1")
    assert db._conn is None and db._metric_conn is None


# ── F7. read API cannot be used to mutate ────────────


async def test_read_helper_refuses_mutating_sql(fresh_db):
    with pytest.raises(db.ReadOnlyViolation):
        await db._fetch_all(
            "INSERT INTO audit_log (timestamp, user_email, action) "
            "VALUES (1.0,'x','y') RETURNING id"
        )
    assert await db.get_audit_log() == []


async def test_read_helper_refuses_mutating_sql_under_fail_stop(fresh_db):
    db._fail_stop = "test"
    with pytest.raises(db.DatabaseFailStop):
        await db._fetch_all(
            "INSERT INTO audit_log (timestamp, user_email, action) "
            "VALUES (1.0,'x','y') RETURNING id"
        )
    db._fail_stop = None
    assert await db.get_audit_log() == []


async def test_fail_stop_refuses_reads_too(fresh_db):
    await db.store_metric("a1", 1.0, _metric())
    db._fail_stop = "test"
    with pytest.raises(db.DatabaseFailStop):
        await db.get_recent_metrics("a1")
    with pytest.raises(db.DatabaseFailStop):
        await db.get_audit_log()


def test_raw_connection_accessor_is_private():
    assert not hasattr(db, "get_db"), "get_db must not be public"
    assert not hasattr(db, "fetch_all"), "fetch_all must not be public"
    assert not hasattr(db, "fetch_one"), "fetch_one must not be public"


def test_no_module_outside_database_py_imports_a_raw_accessor():
    root = DATABASE_PY.parent
    offenders = []
    for path in sorted(root.rglob("*.py")):
        if path == DATABASE_PY:
            continue
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and (node.module or "").endswith("database"):
                for alias in node.names:
                    if alias.name in {"get_db", "_get_conn", "fetch_all", "fetch_one",
                                      "_fetch_all", "_fetch_one"}:
                        offenders.append(f"{path.relative_to(root)}:{node.lineno} {alias.name}")
    assert not offenders, "raw/read primitives imported outside database.py:\n  " + \
        "\n  ".join(offenders)


# ═════════════════════════════════════════════════════
# Hardening round 2 — counterexamples from independent review
# ═════════════════════════════════════════════════════


class FailingWorkerProbe(WorkerProbe):
    """Like WorkerProbe, but the worker RAISES after it is released.

    aiosqlite reports that as OperationalError on an otherwise-completed task
    (`task.cancelled()` is False), which is the shape that lets a worker error
    overwrite a cancellation the caller is still owed."""

    def __call__(self, value=0):
        self.calls += 1
        self.entered.set()
        self.release.wait(timeout=30)
        raise ValueError("worker exploded")


# ── R1. caller cancellation + worker error ──────────


async def test_caller_cancellation_survives_a_later_worker_error(fresh_db):
    # The caller is cancelled while the worker owns the statement; the worker
    # then fails. The caller is owed its CancelledError — raising the worker's
    # error instead loses the cancellation, and a caller that catches Exception
    # (periodic_maintenance does) keeps running as if nothing happened.
    conn = await db._get_conn()
    probe = FailingWorkerProbe()
    await probe.install(conn)
    await db.store_metric("seed", 1.0, _metric())

    task = asyncio.create_task(db._fetch_all("SELECT probe(id) AS v FROM metrics"))
    await probe.wait_entered()
    task.cancel()
    probe.let_go()                      # worker now raises
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=15)
    assert probe.calls >= 1, "fault was never injected"


async def test_cancelled_write_with_worker_error_still_refuses_next_operation(fresh_db):
    # If the boundary cannot prove the connection is safe after that, fail-stop
    # stays — but the CURRENT caller still gets its CancelledError, and only the
    # NEXT operation is refused.
    conn = await db._get_conn()
    probe = FailingWorkerProbe()
    await probe.install(conn)

    async def failing_rollback():
        raise RuntimeError("rollback failed")

    async def writer():
        async with db.write_transaction() as tx:
            await tx.fetch_all("SELECT probe(1)")

    task = asyncio.create_task(writer())
    await probe.wait_entered()
    conn.rollback = failing_rollback     # cleanup cannot prove safety
    task.cancel()
    probe.let_go()
    try:
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=15)
        assert db._fail_stop is not None, "unsafe cleanup did not fail-stop"
    finally:
        del conn.rollback
    with pytest.raises(db.DatabaseFailStop):
        await db.store_metric("a1", 1.0, _metric())


async def test_maintenance_loop_stops_on_a_cancelled_db_call(fresh_db):
    # The concrete consequence: app.main's periodic_maintenance catches
    # Exception but re-raises CancelledError. A DB call that swallows the
    # cancellation would keep the loop alive through shutdown.
    conn = await db._get_conn()
    probe = FailingWorkerProbe()
    await probe.install(conn)
    rounds = []

    async def maintenance_like():
        for _ in range(3):                   # bounded so a RED run cannot spin
            try:
                await db._fetch_all("SELECT probe(1)")
            except asyncio.CancelledError:
                raise
            except Exception:
                rounds.append("swallowed")   # loop survives -> shutdown hangs
        return "loop finished without ever seeing the cancellation"

    task = asyncio.create_task(maintenance_like())
    await probe.wait_entered()
    task.cancel()
    probe.let_go()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=15)
    assert rounds == [], "the cancellation was swallowed and the loop kept going"
    assert probe.calls >= 1, "fault was never injected"


# ── R2. connection build / configure / readback / publish ──


def _connect_factory(monkeypatch, mutate):
    """Wrap aiosqlite.connect so a test can break a connection AFTER it is
    created but BEFORE database.py configures it."""
    real_connect = db.aiosqlite.connect
    made = []

    def factory(*args, **kwargs):
        awaitable = real_connect(*args, **kwargs)

        class _Wrapper:
            def __await__(self):
                conn = yield from awaitable.__await__()
                made.append(conn)
                mutate(conn)
                return conn

        return _Wrapper()

    monkeypatch.setattr(db.aiosqlite, "connect", factory)
    return made


async def test_connection_is_not_published_when_a_pragma_fails(fresh_db, monkeypatch):
    # A connection published before it is configured is reused forever with the
    # wrong journal_mode — a DELETE-journal connection on a WAL database.
    await db.close_db()
    db._closed = False
    monkeypatch.setattr(db, "_conn", None)

    def break_wal(conn):
        real_execute = conn.execute

        async def execute(sql, *a, **k):
            if isinstance(sql, str) and "journal_mode" in sql:
                raise RuntimeError("PRAGMA journal_mode failed")
            return await real_execute(sql, *a, **k)

        conn.execute = execute

    made = _connect_factory(monkeypatch, break_wal)
    with pytest.raises(BaseException):
        await db.blacklist_token("t", 1.0)      # shared connection path
    assert db._conn is None, "a half-configured connection was published"
    assert made, "fault was never injected"
    # And the connection that was created must not be left ownerless.
    for conn in made:
        assert conn._running is False, "an unpublished connection's worker survived"


async def test_connection_is_not_published_when_readback_disagrees(fresh_db, monkeypatch):
    # PRAGMA can silently no-op (a WAL switch can fail on some filesystems), so
    # the applied value must be read back, not assumed.
    await db.close_db()
    db._closed = False
    monkeypatch.setattr(db, "_conn", None)

    def lie_about_wal(conn):
        real_execute = conn.execute

        async def execute(sql, *a, **k):
            cursor = await real_execute(sql, *a, **k)
            if isinstance(sql, str) and "journal_mode" in sql:
                async def fetchall():
                    return [("delete",)]
                cursor.fetchall = fetchall
            return cursor

        conn.execute = execute

    made = _connect_factory(monkeypatch, lie_about_wal)
    with pytest.raises(BaseException):
        await db.blacklist_token("t", 1.0)
    assert db._conn is None
    assert made, "fault was never injected"


async def test_foreign_keys_are_enabled_and_verified(fresh_db):
    # FK enforcement is connection-local and OFF by default in SQLite, so it
    # must be turned on per connection AND read back.
    rows = await db._fetch_all_unrestricted("PRAGMA foreign_keys")
    assert rows[0][0] == 1, "foreign_keys was not enabled on the shared connection"


async def test_connection_is_not_published_when_foreign_keys_stay_off(
    fresh_db, monkeypatch
):
    await db.close_db()
    db._closed = False
    monkeypatch.setattr(db, "_conn", None)

    def keep_fk_off(conn):
        real_execute = conn.execute

        async def execute(sql, *a, **k):
            cursor = await real_execute(sql, *a, **k)
            if isinstance(sql, str) and "foreign_keys" in sql:
                async def fetchall():
                    return [(0,)]
                cursor.fetchall = fetchall
            return cursor

        conn.execute = execute

    made = _connect_factory(monkeypatch, keep_fk_off)
    with pytest.raises(BaseException):
        await db.blacklist_token("t", 1.0)
    assert db._conn is None
    assert made, "fault was never injected"


async def test_no_connection_is_published_while_closing(fresh_db):
    await db.close_db()
    db._closed = False
    db._conn = None
    db._closing = True
    try:
        with pytest.raises(db.DatabaseFailStop):
            await db.store_metric("a1", 1.0, _metric())
        assert db._conn is None
    finally:
        db._closing = False


# ── R3. transaction-state adjudication ──────────────


@pytest.mark.expect_db_leftovers
async def test_unreadable_transaction_state_is_not_treated_as_safe(fresh_db):
    # The wrapper can report "no active connection" while SQLite still holds an
    # open transaction on the underlying handle. Reading in_transaction then
    # RAISES, and treating that as "already closed" reports a clean terminal on
    # a transaction nobody closed.
    conn = await db._get_conn()

    async def failing_commit():
        raise RuntimeError("commit failed")

    real_rollback = conn.rollback
    detached = {}

    async def detach_then_rollback():
        # Simulate the wrapper losing its connection mid-cleanup while the raw
        # sqlite3 handle still has the transaction open.
        detached["raw"] = conn._connection
        conn._connection = None
        raise ValueError("no active connection")

    conn.commit = failing_commit
    conn.rollback = detach_then_rollback
    try:
        with pytest.raises(db.DatabaseFailStop):
            await asyncio.wait_for(
                db.store_net_audit("a1", 1.0, [_event()], []), timeout=15)
        assert db._fail_stop is not None
        assert db._unclosed or db._unresolved, (
            "an unadjudicated transaction left no restart-required trace"
        )
    finally:
        conn.rollback = real_rollback
        del conn.commit
        if detached.get("raw") is not None:
            conn._connection = detached["raw"]
            try:
                detached["raw"].rollback()
            except BaseException:
                pass


# ── R4. close_db lifecycle ──────────────────────────


async def test_close_db_uses_one_absolute_deadline(fresh_db, monkeypatch):
    # Each stage must draw down the SAME budget. Per-stage timeouts let a slow
    # close take timeout x stages.
    conn = await db._get_conn()
    probe = WorkerProbe()
    await probe.install(conn)

    async def slow_writer():
        async with db.write_transaction() as tx:
            await tx.fetch_all("SELECT probe(1)")

    writer = asyncio.create_task(slow_writer())
    await probe.wait_entered()

    loop = asyncio.get_running_loop()
    started = loop.time()
    verdict = await asyncio.wait_for(db.close_db(timeout=0.5), timeout=10)
    elapsed = loop.time() - started
    probe.let_go()
    await asyncio.gather(writer, return_exceptions=True)

    assert verdict is db.CloseVerdict.RESTART_REQUIRED, verdict
    assert elapsed < 2.0, f"close_db took {elapsed:.2f}s for a 0.5s budget"


async def test_close_db_cancelled_while_waiting_does_not_reopen_admission(fresh_db):
    conn = await db._get_conn()
    probe = WorkerProbe()
    await probe.install(conn)

    async def slow_writer():
        async with db.write_transaction() as tx:
            await tx.fetch_all("SELECT probe(1)")

    writer = asyncio.create_task(slow_writer())
    await probe.wait_entered()

    closer = asyncio.create_task(db.close_db(timeout=30))
    await asyncio.sleep(0)
    closer.cancel()
    result = await asyncio.gather(closer, return_exceptions=True)
    probe.let_go()
    await asyncio.gather(writer, return_exceptions=True)

    # Whatever the cancellation did, the DB must NOT be back to normal service.
    assert db._closing or db._closed or db._fail_stop is not None, (
        f"close_db cancellation returned the DB to normal admission ({result})"
    )
    with pytest.raises(db.DatabaseFailStop):
        await db.store_metric("a1", 1.0, _metric())


@pytest.mark.expect_db_leftovers
async def test_close_db_never_reports_closed_while_a_connection_is_unclosed(fresh_db):
    conn = await db._get_conn()

    async def failing_close():
        raise RuntimeError("close failed")

    conn.close = failing_close
    try:
        first = await asyncio.wait_for(db.close_db(timeout=5), timeout=15)
        assert first is db.CloseVerdict.RESTART_REQUIRED, first
        assert db._unclosed, "an unclosed connection left no trace"
        second = await asyncio.wait_for(db.close_db(timeout=5), timeout=15)
        assert second is db.CloseVerdict.RESTART_REQUIRED, (
            "close_db reported CLOSED while a connection was still unclosed"
        )
    finally:
        del conn.close
        try:
            await asyncio.wait_for(conn.close(), timeout=5)
        except BaseException:
            pass


async def test_completed_unresolved_task_is_classified_before_removal(fresh_db, monkeypatch):
    # A submission that later completes must be ADJUDICATED (did it succeed?
    # did it error?) before it stops counting as unresolved.
    monkeypatch.setattr(db, "_CLEANUP_TIMEOUT", 0.2)
    monkeypatch.setattr(db, "_CLEANUP_CANCEL_BUDGET", 1)
    conn = await db._get_conn()
    state, release = await _commit_blocked_in_worker(conn)
    try:
        with pytest.raises(db.DatabaseFailStop):
            await asyncio.wait_for(db.blacklist_token("t", 1.0), timeout=20)
        assert db._unresolved
        await release()                       # the submission now completes
        adjudicated = db.adjudicate_unresolved()
        assert adjudicated, "completed submissions were never classified"
        assert all(entry["outcome"] in ("completed", "failed", "cancelled")
                   for entry in adjudicated), adjudicated
        assert db._fail_stop is not None, "adjudication must not clear fail-stop"
    finally:
        if "commit" in conn.__dict__:
            del conn.commit


# ── R5. read-only boundary: mutating PRAGMA ─────────


@pytest.mark.parametrize("sql", [
    "PRAGMA user_version = 731",
    "PRAGMA journal_mode = DELETE",
    "PRAGMA wal_checkpoint(TRUNCATE)",
    "PRAGMA foreign_keys = OFF",
    "pragma  user_version=731",
])
async def test_read_helper_refuses_mutating_pragma(fresh_db, sql):
    before = (await db._fetch_all_unrestricted("PRAGMA user_version"))[0][0]
    with pytest.raises(db.ReadOnlyViolation):
        await db._fetch_all(sql)
    after = (await db._fetch_all_unrestricted("PRAGMA user_version"))[0][0]
    assert after == before == 0, (before, after)
    journal = (await db._fetch_all_unrestricted("PRAGMA journal_mode"))[0][0]
    assert journal.lower() == "wal", journal


# ── R8. the submission budget is a deadline, not a per-turn allowance ──
#
# Independent review counterexample: _submit's retry loop re-spent the SAME
# timeout on every turn, and a plain timeout (nobody cancelled anything) fell
# through to the next turn instead of ending the wait. So an unresponsive worker
# took _CLEANUP_CANCEL_BUDGET x timeout to reach a verdict — with _op_lock held
# the whole time, which stalls every read and write in the process. Every test
# that exercised the budget pinned _CLEANUP_CANCEL_BUDGET = 1, so the multiplied
# path was never covered.


async def _occupy_worker_with_gate(conn, name):
    gate = WorkerProbe(name)
    await gate.install(conn)
    holder = asyncio.create_task(_run_probe(conn, name))
    await gate.wait_entered()
    return gate, holder


async def test_submit_budget_is_not_multiplied_by_the_cancel_allowance(
    fresh_db, monkeypatch
):
    # No cancellation at all: a stuck worker must reach its verdict within ONE
    # budget, not budget x allowance.
    #
    # No `expect_db_leftovers` marker: the BEGIN whose outcome went unknown here
    # hands back its cursor once the gate is released, and shutdown now owns and
    # closes it, so the database is genuinely clean by teardown.
    monkeypatch.setattr(db, "_CLEANUP_TIMEOUT", 0.25)
    monkeypatch.setattr(db, "_CLEANUP_CANCEL_BUDGET", 4)   # deliberately > 1
    conn = await db._get_conn()
    gate, holder = await _occupy_worker_with_gate(conn, "budgetgate")
    loop = asyncio.get_running_loop()
    try:
        started = loop.time()
        with pytest.raises(db.DatabaseFailStop):
            await asyncio.wait_for(db.blacklist_token("t", 1.0), timeout=10)
        elapsed = loop.time() - started
        assert gate.calls >= 1, "the worker was never actually occupied"
        assert elapsed < 0.75, (
            f"a stuck worker took {elapsed:.2f}s to reach a verdict on a 0.25s "
            "budget — the budget is being re-spent per retry"
        )
    finally:
        gate.let_go()
        await asyncio.gather(holder, return_exceptions=True)


@pytest.mark.expect_db_leftovers
async def test_close_db_connection_close_stays_inside_the_deadline(
    fresh_db, monkeypatch
):
    # The connection-close stage passes timeout=remaining() into _submit. If
    # _submit re-spends it per retry, close_db's "one absolute deadline" is
    # multiplied at exactly the stage that matters during shutdown.
    monkeypatch.setattr(db, "_CLEANUP_CANCEL_BUDGET", 4)
    conn = await db._get_conn()
    hit = []

    async def close_that_never_returns():
        hit.append(1)
        await asyncio.Event().wait()

    conn.close = close_that_never_returns
    loop = asyncio.get_running_loop()
    try:
        started = loop.time()
        verdict = await asyncio.wait_for(db.close_db(timeout=0.4), timeout=15)
        elapsed = loop.time() - started
        assert hit, "the close fault was never reached"
        assert verdict is db.CloseVerdict.RESTART_REQUIRED, verdict
        assert elapsed < 1.2, (
            f"close_db(timeout=0.4) took {elapsed:.2f}s — the connection-close "
            "stage re-spent the remaining budget per retry"
        )
    finally:
        del conn.close


async def test_cancellation_still_gets_its_turns_within_the_deadline(fresh_db, monkeypatch):
    # The allowance still exists for its real purpose: a cancellation delivered
    # while the worker runs must not end the recovery early. It just may not
    # extend the deadline.
    monkeypatch.setattr(db, "_CLEANUP_TIMEOUT", 5.0)
    monkeypatch.setattr(db, "_CLEANUP_CANCEL_BUDGET", 4)
    conn = await db._get_conn()
    probe = WorkerProbe()
    await probe.install(conn)
    await db.store_metric("seed", 1.0, _metric())

    task = asyncio.create_task(db._fetch_all("SELECT probe(id) AS v FROM metrics"))
    await probe.wait_entered()
    task.cancel()
    probe.let_go()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=10)
    assert probe.calls >= 1
    assert db._fail_stop is None, db._fail_stop     # outcome WAS recovered
    await db.store_net_audit("a1", 1.0, [_event()], [])


# ═════════════════════════════════════════════════════
# Hardening round 3 — ownership of late results, cancellation
# preservation, and pre-existing foreign-key violations
# ═════════════════════════════════════════════════════


class ConnectGate:
    """Block the REAL `sqlite3.connect` inside the aiosqlite worker thread.

    aiosqlite runs the connector on its own worker, so this is the only way to
    make a connect submission outlive its caller's deadline and then come back
    holding a live Connection nobody is waiting for. `entered` is set from the
    worker thread, so waiting on it is proof of submission."""

    def __init__(self, monkeypatch, path):
        self.path = str(path)
        self.entered = threading.Event()
        self.release = threading.Event()
        self.calls = 0
        real_connect = sqlite3.connect

        def gated(loc, *args, **kwargs):
            if str(loc) == self.path:
                self.calls += 1
                self.entered.set()
                self.release.wait(timeout=30)
            return real_connect(loc, *args, **kwargs)

        monkeypatch.setattr(sqlite3, "connect", gated)

    async def wait_entered(self, timeout=5.0):
        await asyncio.wait_for(asyncio.to_thread(self.entered.wait, timeout),
                               timeout=timeout + 2)
        assert self.entered.is_set(), "sqlite3.connect never ran on the worker"

    def let_go(self):
        self.release.set()


async def _wait_until_queued(conn, what, timeout=10.0):
    """Poll the aiosqlite worker's OWN queue until the operation is really in
    it — proof of submission, not of a wrapper having run on the event loop."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if conn._tx.qsize() >= 1:
            return
        await asyncio.sleep(0.005)
    pytest.fail(f"{what} was never submitted to the aiosqlite worker")


async def _collect_late(tasks, timeout=25.0):
    """Whatever the workers handed back after their callers gave up."""
    results = await asyncio.wait_for(
        asyncio.gather(*tasks, return_exceptions=True), timeout=timeout)
    return [r for r in results if isinstance(r, aiosqlite.Connection)]


async def _force_close(conns):
    """Last-resort cleanup so a RED run cannot leave a non-daemon aiosqlite
    thread behind and hang the session. Runs only AFTER the assertions."""
    for conn in conns:
        if "close" in conn.__dict__:
            del conn.close
        try:
            await asyncio.wait_for(conn.close(), timeout=5)
        except BaseException:
            pass


def _owners_of(conn):
    """Every place production is still accounting for this connection."""
    owners = []
    if any(c is conn for c in db._unclosed):
        owners.append("_unclosed")
    if any(entry[-1] is conn for entry in getattr(db, "_late_results", ())):
        owners.append("_late_results")
    for entry in db._unresolved:
        task = entry[1]
        if task.done() and not task.cancelled() and task.exception() is None \
                and task.result() is conn:
            owners.append("_unresolved")
    return owners


# ── H1. a connect that finishes after its caller gave up ──


async def test_late_connect_result_is_owned_not_dropped(fresh_db, monkeypatch):
    # `_submit(aiosqlite.connect(...))` can time out and the worker still
    # returns a real Connection afterwards. Recording "the task completed" and
    # dropping the object leaves a live sqlite handle and a non-daemon worker
    # thread with no owner — while close_db can still answer CLOSED.
    monkeypatch.setattr(db, "_CLEANUP_TIMEOUT", 0.25)
    monkeypatch.setattr(db, "_CLEANUP_CANCEL_BUDGET", 4)
    gate = ConnectGate(monkeypatch, db._db_path)

    for what in ("shared", "metric"):
        with pytest.raises(db._OutcomeUnknown):
            await asyncio.wait_for(db._open_connection(what), timeout=20)
        await gate.wait_entered()

    assert gate.calls == 2, f"only {gate.calls} connect(s) reached the worker"
    assert len(db._unresolved) == 2, db._unresolved
    tasks = [entry[1] for entry in db._unresolved]

    gate.let_go()
    late = await _collect_late(tasks)
    try:
        assert len(late) == 2, f"the workers did not return two Connections: {late}"
        assert all(c._running for c in late), "the late workers were never alive"

        settled = db.adjudicate_unresolved()
        assert len(settled) == 2, settled
        for conn in late:
            assert _owners_of(conn), (
                "adjudicate_unresolved() recorded a completed connect and threw "
                "the Connection away: the late connection has no owner, so its "
                "sqlite handle and non-daemon aiosqlite worker are unaccounted for"
            )

        verdict = await asyncio.wait_for(db.close_db(timeout=10), timeout=30)
        for conn in late:
            assert (conn._running is False) or any(c is conn for c in db._unclosed), (
                f"close_db returned {verdict} leaving a live late connection that "
                "is not registered as unclosed"
            )
        if verdict is db.CloseVerdict.CLOSED:
            assert not db._unclosed, db._unclosed
            assert not getattr(db, "_late_results", ()), db._late_results
            for conn in late:
                # Measured AT the verdict. Joining first and THEN asserting
                # would pass on a close_db that never waits for the thread.
                assert conn._running is False and conn.is_alive() is False, (
                    "close_db reported CLOSED while a late connection's "
                    "aiosqlite worker thread was still running"
                )
    finally:
        await _force_close(late)


@pytest.mark.expect_db_leftovers
async def test_late_connection_that_cannot_be_closed_is_restart_required(
    fresh_db, monkeypatch
):
    # The other half of the contract: when the late connection's close cannot
    # be confirmed inside the remaining deadline, it is PRESERVED in _unclosed
    # and the verdict is restart-required — never CLOSED.
    monkeypatch.setattr(db, "_CLEANUP_TIMEOUT", 0.25)
    monkeypatch.setattr(db, "_CLEANUP_CANCEL_BUDGET", 4)
    gate = ConnectGate(monkeypatch, db._db_path)

    with pytest.raises(db._OutcomeUnknown):
        await asyncio.wait_for(db._open_connection("shared"), timeout=20)
    await gate.wait_entered()
    tasks = [entry[1] for entry in db._unresolved]
    gate.let_go()
    late = await _collect_late(tasks)
    assert len(late) == 1, late
    orphan = late[0]

    hit = []

    async def close_that_never_returns():
        hit.append(1)
        await asyncio.Event().wait()

    orphan.close = close_that_never_returns
    loop = asyncio.get_running_loop()
    try:
        db.adjudicate_unresolved()
        started = loop.time()
        verdict = await asyncio.wait_for(db.close_db(timeout=0.5), timeout=20)
        elapsed = loop.time() - started
        assert hit, "the late connection's close was never even attempted"
        assert verdict is db.CloseVerdict.RESTART_REQUIRED, verdict
        assert any(c is orphan for c in db._unclosed), (
            "a late connection whose close could not be confirmed was not "
            f"preserved in _unclosed: {db._unclosed}"
        )
        assert elapsed < 2.0, f"close_db(timeout=0.5) took {elapsed:.2f}s"
    finally:
        await _force_close(late)


async def test_public_write_api_never_leaks_an_internal_outcome_class(
    fresh_db, monkeypatch
):
    # The connection is acquired OUTSIDE the boundary's try, so an unresolved
    # connect surfaced `_OutcomeUnknown` — an internal classification a caller
    # cannot act on — straight out of store_metric().
    monkeypatch.setattr(db, "_CLEANUP_TIMEOUT", 0.25)
    monkeypatch.setattr(db, "_CLEANUP_CANCEL_BUDGET", 4)
    gate = ConnectGate(monkeypatch, db._db_path)   # metric conn is not open yet
    assert db._metric_conn is None

    late = []
    try:
        with pytest.raises(BaseException) as excinfo:
            await asyncio.wait_for(db.store_metric("a1", 1.0, _metric()), timeout=20)
        await gate.wait_entered()
        exc = excinfo.value
        assert not isinstance(exc, (db._OutcomeUnknown, db._ChildCancelled)), (
            f"store_metric leaked the internal classification {exc!r}"
        )
        assert isinstance(exc, db.DatabaseFailStop), repr(exc)
    finally:
        gate.let_go()
        late = await _collect_late([entry[1] for entry in db._unresolved])
        db.adjudicate_unresolved()
        await _force_close(late)


async def test_public_read_api_never_leaks_an_internal_outcome_class(
    fresh_db, monkeypatch
):
    monkeypatch.setattr(db, "_CLEANUP_TIMEOUT", 0.25)
    monkeypatch.setattr(db, "_CLEANUP_CANCEL_BUDGET", 4)
    published = db._conn
    monkeypatch.setattr(db, "_conn", None)
    gate = ConnectGate(monkeypatch, db._db_path)

    late = []
    try:
        with pytest.raises(BaseException) as excinfo:
            await asyncio.wait_for(db.get_recent_metrics("a1"), timeout=20)
        await gate.wait_entered()
        exc = excinfo.value
        assert not isinstance(exc, (db._OutcomeUnknown, db._ChildCancelled)), (
            f"get_recent_metrics leaked the internal classification {exc!r}"
        )
        assert isinstance(exc, db.DatabaseFailStop), repr(exc)
    finally:
        gate.let_go()
        late = await _collect_late([entry[1] for entry in db._unresolved])
        db.adjudicate_unresolved()
        await _force_close(late)
        await _force_close([published])


# ── H2. a cancellation owed to the caller survives cleanup ──


async def test_cancellation_during_a_queued_rollback_reaches_the_caller(fresh_db):
    # COMMIT fails with an ordinary error, the ROLLBACK behind it is genuinely
    # queued on the worker, and the caller is cancelled while it waits. The
    # rollback then completes — and the caller is STILL owed its
    # CancelledError. Handing back the commit error instead loses it, and
    # app.main's `except Exception` maintenance loop keeps running.
    conn = await db._get_conn()
    probe = WorkerProbe("rbgate")
    await probe.install(conn)
    real_rollback = conn.rollback
    completed = []
    rollback_submitted = asyncio.Event()
    holder = {}

    async def occupying_failing_commit():
        holder["task"] = asyncio.create_task(_run_probe(conn, probe.name))
        await probe.wait_entered()          # the worker is now busy
        raise RuntimeError("commit failed")

    async def observed_rollback():
        rollback_submitted.set()
        result = await real_rollback()
        completed.append("rollback")
        return result

    conn.commit = occupying_failing_commit
    conn.rollback = observed_rollback
    task = asyncio.create_task(db.store_net_audit("a1", 1.0, [_event()], []))
    try:
        await asyncio.wait_for(rollback_submitted.wait(), timeout=15)
        await _wait_until_queued(conn, "the ROLLBACK")
        task.cancel()
        probe.let_go()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=20)
    finally:
        probe.let_go()
        conn.rollback = real_rollback
        if "commit" in conn.__dict__:
            del conn.commit
        if holder.get("task"):
            await asyncio.gather(holder["task"], return_exceptions=True)

    assert probe.calls >= 1, "the worker was never occupied"
    assert completed == ["rollback"], "the rollback was abandoned half-done"
    assert conn.in_transaction is False, "lock released over an open transaction"


async def test_unknown_outcome_under_cancellation_still_cancels_the_caller(
    fresh_db, monkeypatch
):
    # The worker overruns the deadline AND the caller is cancelled. Both facts
    # are true, and each is owed to a different place: fail-stop plus the
    # unresolved registration stay, but THIS caller gets its CancelledError,
    # not a DatabaseFailStop. Only the NEXT operation is refused.
    monkeypatch.setattr(db, "_CLEANUP_TIMEOUT", 1.0)
    monkeypatch.setattr(db, "_CLEANUP_CANCEL_BUDGET", 4)
    conn = await db._get_conn()
    state, release = await _commit_blocked_in_worker(conn)
    task = asyncio.create_task(db.store_net_audit("a1", 1.0, [_event()], []))
    try:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 15
        while not state.get("submitted") and loop.time() < deadline:
            await asyncio.sleep(0.005)
        assert state.get("submitted"), "COMMIT never reached the worker queue"
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=25)
        assert db._fail_stop is not None, "the unknown outcome did not fail-stop"
        assert db._unresolved, "the unresolved submission was forgotten"
        with pytest.raises(db.DatabaseFailStop):
            await asyncio.wait_for(db.store_metric("a1", 2.0, _metric()), timeout=10)
    finally:
        await release()


async def test_write_loop_that_swallows_exceptions_stops_on_one_cancel(fresh_db):
    # The concrete consequence of losing that cancellation: app.main's
    # periodic_maintenance is `except Exception: continue`, so a swallowed
    # cancellation keeps the loop alive and the shutdown await never finishes.
    conn = await db._get_conn()
    probe = WorkerProbe("loopgate")
    await probe.install(conn)
    real_rollback = conn.rollback
    rollback_submitted = asyncio.Event()
    swallowed = []
    holder = {}

    async def occupying_failing_commit():
        if "task" not in holder:
            holder["task"] = asyncio.create_task(_run_probe(conn, probe.name))
            await probe.wait_entered()
        raise RuntimeError("commit failed")

    async def observed_rollback():
        rollback_submitted.set()
        return await real_rollback()

    conn.commit = occupying_failing_commit
    conn.rollback = observed_rollback

    async def maintenance_like():
        for _ in range(3):                   # bounded so a RED run cannot spin
            try:
                await db.store_net_audit("a1", 1.0, [_event()], [])
            except asyncio.CancelledError:
                raise
            except Exception:
                swallowed.append("swallowed")
        return "the loop outlived its cancellation"

    task = asyncio.create_task(maintenance_like())
    try:
        await asyncio.wait_for(rollback_submitted.wait(), timeout=15)
        await _wait_until_queued(conn, "the ROLLBACK")
        task.cancel()
        probe.let_go()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=25)
    finally:
        probe.let_go()
        conn.rollback = real_rollback
        if "commit" in conn.__dict__:
            del conn.commit
        if holder.get("task"):
            await asyncio.gather(holder["task"], return_exceptions=True)

    assert probe.calls >= 1, "the worker was never occupied"
    assert swallowed == [], "the cancellation was swallowed and the loop kept going"


async def test_cancellation_during_an_unpublished_discard_reaches_the_caller(
    fresh_db, monkeypatch
):
    # A connection that failed verification is discarded before it is
    # published. If the caller is cancelled while that close is in flight, the
    # cancellation is owed to it — raising the verification error instead is
    # the same swallow, one layer further out.
    await db.close_db()
    db._closed = False
    monkeypatch.setattr(db, "_conn", None)
    discarding = asyncio.Event()
    proceed = asyncio.Event()
    closed = []

    def break_fk_and_gate_close(conn):
        real_execute = conn.execute
        real_close = conn.close

        async def execute(sql, *a, **k):
            cursor = await real_execute(sql, *a, **k)
            if isinstance(sql, str) and "foreign_keys" in sql and "=" not in sql:
                async def fetchall():
                    return [(0,)]
                cursor.fetchall = fetchall
            return cursor

        async def close():
            discarding.set()
            await proceed.wait()
            await real_close()               # the real close DOES reach the worker
            closed.append(conn)

        conn.execute = execute
        conn.close = close

    made = _connect_factory(monkeypatch, break_fk_and_gate_close)
    task = asyncio.create_task(db.blacklist_token("t", 1.0))
    await asyncio.wait_for(discarding.wait(), timeout=15)
    task.cancel()
    proceed.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=25)

    assert made, "the connect fault was never injected"
    assert closed, "the unpublished connection was never really closed"
    assert db._conn is None, "a connection that failed verification was published"


# ── H3. close_db's single deadline covers BOTH connections ──


@pytest.mark.expect_db_leftovers
async def test_close_db_deadline_covers_both_connections(fresh_db, monkeypatch):
    # Two connections, both slow to close. The shutdown budget is ONE absolute
    # deadline for the whole close, not one per connection and not one per
    # cancellation retry.
    monkeypatch.setattr(db, "_CLEANUP_CANCEL_BUDGET", 4)
    shared = await db._get_conn()
    await db.store_metric("a1", 1.0, _metric())          # publishes the metric conn
    metric = db._metric_conn
    assert metric is not None and metric is not shared
    hit = []

    async def close_that_never_returns():
        hit.append(1)
        await asyncio.Event().wait()

    shared.close = close_that_never_returns
    metric.close = close_that_never_returns
    loop = asyncio.get_running_loop()
    try:
        started = loop.time()
        verdict = await asyncio.wait_for(db.close_db(timeout=0.4), timeout=20)
        elapsed = loop.time() - started
        assert hit, "no connection-close fault was reached"
        assert verdict is db.CloseVerdict.RESTART_REQUIRED, verdict
        assert len(db._unclosed) == 2, (
            f"both connections must be preserved as unclosed: {db._unclosed}")
        assert elapsed < 1.6, (
            f"close_db(timeout=0.4) took {elapsed:.2f}s for two connections — "
            "the single absolute deadline is being re-spent per connection"
        )
    finally:
        await _force_close([shared, metric])


# ── H4. pre-existing foreign-key violations ─────────

_LEGACY_ORPHAN_SQL = """
PRAGMA foreign_keys=OFF;
CREATE TABLE users (
    email TEXT PRIMARY KEY,
    password_hash TEXT NOT NULL,
    totp_secret TEXT,
    totp_enabled INTEGER DEFAULT 0,
    must_change_password INTEGER DEFAULT 0,
    created_at REAL NOT NULL,
    role TEXT NOT NULL DEFAULT 'user',
    is_active INTEGER NOT NULL DEFAULT 1,
    tokens_valid_after REAL NOT NULL DEFAULT 0
);
CREATE TABLE user_host_accounts (
    user_email TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    host_user TEXT NOT NULL,
    PRIMARY KEY (user_email, agent_id),
    FOREIGN KEY (user_email) REFERENCES users(email) ON DELETE CASCADE
);
INSERT INTO users VALUES ('keep@example.com','h',NULL,0,0,1.0,'admin',1,0);
INSERT INTO users VALUES ('ghost@example.com','h',NULL,0,0,2.0,'user',1,0);
INSERT INTO user_host_accounts VALUES ('ghost@example.com','agent-1','root');
INSERT INTO user_host_accounts VALUES ('ghost@example.com','agent-2','root');
DELETE FROM users WHERE email = 'ghost@example.com';
"""

_GHOST = "ghost@example.com"


@pytest.fixture
async def legacy_orphan_db(tmp_path, monkeypatch):
    """A database in the shape older builds left behind: foreign_keys OFF, a
    user deleted, and their user_host_accounts rows still there.

    Those rows name a host account (`root` here). Whoever next registers that
    email inherits them — which is a terminal-access grant nobody made."""
    path = tmp_path / "legacy.db"
    raw = sqlite3.connect(str(path))
    try:
        raw.executescript(_LEGACY_ORPHAN_SQL)
        raw.commit()
        assert raw.execute(
            "SELECT COUNT(*) FROM user_host_accounts").fetchone()[0] == 2
    finally:
        raw.close()

    monkeypatch.setattr(db, "_db_path", str(path))
    monkeypatch.setattr(db, "_conn", None)
    monkeypatch.setattr(db, "_metric_conn", None, raising=False)
    monkeypatch.setattr(db, "_op_lock", asyncio.Lock(), raising=False)
    monkeypatch.setattr(db, "_fail_stop", None, raising=False)
    yield path
    assert db._db_path == str(path), db._db_path
    await db.close_db()
    db._closed = False


def _orphan_rows(path):
    raw = sqlite3.connect(str(path))
    try:
        return raw.execute(
            "SELECT user_email, agent_id, host_user FROM user_host_accounts "
            "ORDER BY agent_id"
        ).fetchall()
    finally:
        raw.close()


async def test_startup_refuses_a_database_with_foreign_key_violations(
    legacy_orphan_db
):
    try:
        await db.init_db()
    except BaseException as exc:
        assert isinstance(exc, db.SchemaIntegrityViolation), repr(exc)
        message = str(exc)
        assert "user_host_accounts" in message, message
        assert "2" in message, f"the exact row count is not recorded: {message}"
    else:
        pytest.fail(
            "init_db accepted a database with 2 orphaned user_host_accounts "
            "rows; the schema declares a foreign key those rows violate"
        )
    # Evidence, not garbage: never auto-deleted and never re-attributed. A real
    # cleanup is an approved migration, not a startup side effect.
    assert _orphan_rows(legacy_orphan_db) == [
        (_GHOST, "agent-1", "root"),
        (_GHOST, "agent-2", "root"),
    ], _orphan_rows(legacy_orphan_db)


async def test_orphan_host_mapping_never_becomes_a_live_permission(
    legacy_orphan_db
):
    # The user-visible flow: an admin deletes a user on an older build, someone
    # later registers the same email, and the deleted user's host/root mapping
    # comes back as that new account's terminal permission.
    startup_error = None
    try:
        await db.init_db()
    except BaseException as exc:
        startup_error = exc

    if startup_error is None:
        await db.create_user(_GHOST, "hash")
        inherited = await db.get_user_host_accounts(_GHOST)
        assert inherited == {}, (
            "startup accepted orphaned user_host_accounts rows, so a user who "
            f"re-registered a deleted user's email inherited {inherited} — host "
            "root access nobody granted them"
        )
    else:
        assert isinstance(startup_error, db.SchemaIntegrityViolation), \
            repr(startup_error)
        with pytest.raises(db.DatabaseFailStop):
            await db.get_user_host_accounts(_GHOST)
        with pytest.raises(db.DatabaseFailStop):
            await db.create_user(_GHOST, "hash")


# ═════════════════════════════════════════════════════
# Hardening round 5 — a cancelled future is not a worker terminal,
# close != worker exit, and cancellation survives every stage
# ═════════════════════════════════════════════════════


def _live_aiosqlite_workers():
    """Every aiosqlite worker thread alive in this process, found through the
    THREAD registry rather than through anything app.database tracks — so a
    connection the product forgot about is still visible here."""
    return [t for t in threading.enumerate()
            if isinstance(t, aiosqlite.Connection) and t.is_alive()]


async def _worker_gone(conn, timeout=5.0):
    """Wait, off the loop, for one worker thread to actually exit."""
    if getattr(conn, "ident", None) is None:
        return True
    await asyncio.to_thread(conn.join, timeout)
    return not conn.is_alive()


# ── H5-A. a cancelled submission proves nothing about the worker ──


@pytest.mark.expect_db_leftovers
async def test_cancelled_connect_submission_is_not_a_worker_terminal(
    fresh_db, monkeypatch
):
    # aiosqlite runs sqlite3.connect on its own thread. Cancelling the FUTURE
    # we are waiting on does not stop that thread: `_connect` only marks the
    # wrapper not-running and queues a stop sentinel the blocked worker cannot
    # reach. Reading `task.cancelled()` as "the worker is done" therefore
    # retires the submission while a real thread is still inside sqlite3.connect
    # — and close_db can answer CLOSED over it.
    monkeypatch.setattr(db, "_CLEANUP_TIMEOUT", 0.25)
    monkeypatch.setattr(db, "_CLEANUP_CANCEL_BUDGET", 4)
    before = {id(t) for t in _live_aiosqlite_workers()}
    gate = ConnectGate(monkeypatch, db._db_path)

    with pytest.raises(db._OutcomeUnknown):
        await asyncio.wait_for(db._open_connection("shared"), timeout=20)
    await gate.wait_entered()
    assert len(db._unresolved) == 1, db._unresolved

    blocked = [t for t in _live_aiosqlite_workers() if id(t) not in before]
    assert len(blocked) == 1, blocked
    worker = blocked[0]

    task = db._unresolved[0][1]
    task.cancel()                       # the CHILD submission, not our caller
    await asyncio.gather(task, return_exceptions=True)
    try:
        assert task.cancelled() is True, "the submission was not cancelled"
        assert worker._running is False, "aiosqlite did not mark the wrapper stopped"
        assert worker.is_alive() is True, (
            "the fault is not in place: the connector is not blocked in the worker")

        db.adjudicate_unresolved()
        assert db._unresolved, (
            "a cancelled submission was retired while its aiosqlite worker "
            "thread was still blocked inside sqlite3.connect — an asyncio "
            "cancellation is not evidence about the worker"
        )
        verdict = await asyncio.wait_for(db.close_db(timeout=1.0), timeout=30)
        assert verdict is db.CloseVerdict.RESTART_REQUIRED, (
            f"close_db returned {verdict} while a connect worker was still "
            "running; a live non-daemon thread is not a closed database"
        )
        assert worker.is_alive() is True, "the gate released early"
    finally:
        gate.let_go()
        assert await _worker_gone(worker, 10), "the worker never exited"


@pytest.mark.expect_db_leftovers
async def test_cancelled_cursor_submission_makes_its_owner_unsafe(
    fresh_db, monkeypatch
):
    # Same rule for a statement: if the cursor a submission would have produced
    # cannot be recovered, the CONNECTION that ran it is unsafe — the statement
    # may still be holding a read snapshot on it.
    monkeypatch.setattr(db, "_CLEANUP_TIMEOUT", 0.25)
    monkeypatch.setattr(db, "_CLEANUP_CANCEL_BUDGET", 4)
    conn = await db._get_conn()
    gate, holder = await _occupy_worker_with_gate(conn, "cursorgate")
    try:
        with pytest.raises(db.DatabaseFailStop):
            await asyncio.wait_for(db.blacklist_token("t", 1.0), timeout=20)
        assert gate.calls >= 1, "the worker was never occupied"
        assert db._unresolved, db._unresolved

        task = db._unresolved[0][1]
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        assert task.cancelled() is True

        db.adjudicate_unresolved()
        assert db._unresolved, (
            "a cancelled statement submission was retired while its owner "
            "connection's worker was still holding the statement"
        )
        verdict = await asyncio.wait_for(db.close_db(timeout=1.0), timeout=30)
        assert verdict is db.CloseVerdict.RESTART_REQUIRED, verdict
        assert any(c is conn for c in db._unclosed), (
            f"the owner connection was not classified unsafe: {db._unclosed}")
    finally:
        gate.let_go()
        await asyncio.gather(holder, return_exceptions=True)


# ── H5-B. Connection.close() is not worker exit ──────


@pytest.mark.expect_db_leftovers
async def test_close_db_proves_the_worker_thread_exited(fresh_db):
    # aiosqlite 0.21's Connection.close() closes the raw SQLite handle and
    # queues a stop sentinel — it never joins the thread. A worker wedged
    # between the two is still a live non-daemon thread, and CLOSED over it is
    # a shutdown that cannot finish.
    #
    # This gates the VERDICT, which two independent mechanisms enforce: the
    # per-connection join and close_db's terminal straggler scan. Each is
    # isolated by its own test — test_close_db_waits_for_the_worker_thread_to_exit
    # for the join, test_close_db_refuses_closed_while_any_connection_it_built_is_alive
    # for the scan — so removing either one is caught somewhere.
    conn = await db._get_conn()
    loop = asyncio.get_running_loop()
    blocker = threading.Event()
    wedged = threading.Event()
    real_stop = conn._stop_running

    def blocking_item():
        wedged.set()
        blocker.wait(timeout=30)

    def wedging_stop():
        # Queued BEFORE the sentinel: the raw close has already happened, and
        # the worker cannot reach the sentinel behind this.
        conn._tx.put_nowait((loop.create_future(), blocking_item))
        real_stop()

    conn._stop_running = wedging_stop
    try:
        verdict = await asyncio.wait_for(db.close_db(timeout=0.5), timeout=30)
        alive_at_return = conn.is_alive()          # measured AT the verdict,
        running_at_return = conn._running          # never after a join
        diag = (f"verdict={verdict} alive={alive_at_return} "
                f"running={running_at_return} fail_stop={db._fail_stop!r} "
                f"unclosed={len(db._unclosed)} unresolved={len(db._unresolved)} "
                f"tracked={[(id(c), db._worker_alive(c)) for c in db._connections]} "
                f"conn_id={id(conn)} ident={conn.ident} qsize={conn._tx.qsize()}")
        assert await asyncio.to_thread(wedged.wait, 5), f"the wedge never ran ({diag})"
        assert not (verdict is db.CloseVerdict.CLOSED and alive_at_return), (
            "close_db reported CLOSED while the aiosqlite worker thread was "
            "still alive — close() only queues a stop sentinel, it does not "
            f"join the thread [{diag}]"
        )
        assert verdict is db.CloseVerdict.RESTART_REQUIRED, verdict
        assert running_at_return is False and alive_at_return is True
        assert any(c is conn for c in db._unclosed), (
            f"a connection whose worker never exited was not preserved: {db._unclosed}")
    finally:
        blocker.set()
        conn._stop_running = real_stop
        assert await _worker_gone(conn, 10), "the worker never exited"


async def test_close_db_waits_for_the_worker_thread_to_exit(fresh_db):
    # The positive half of the join: a worker that WILL exit, just not
    # instantly, must be waited for inside the shutdown budget — not written
    # off. Without the join close_db returns while the thread is still going,
    # and the only honest verdict left is restart-required for a database that
    # was about to shut down cleanly.
    conn = await db._get_conn()
    loop = asyncio.get_running_loop()
    released = threading.Event()
    wedged = threading.Event()
    real_stop = conn._stop_running

    def slow_to_exit():
        wedged.set()
        released.wait(timeout=30)

    def wedging_stop():
        # Queued ahead of the stop sentinel: the worker is briefly busy after
        # the raw close, exactly as a real one can be.
        conn._tx.put_nowait((loop.create_future(), slow_to_exit))
        real_stop()

    conn._stop_running = wedging_stop
    timer = threading.Timer(0.4, released.set)
    timer.start()
    try:
        verdict = await asyncio.wait_for(db.close_db(timeout=10), timeout=30)
        alive_at_return = conn.is_alive()     # measured AT the verdict
    finally:
        released.set()
        timer.cancel()
        conn._stop_running = real_stop

    diag = (f"verdict={verdict} alive={alive_at_return} running={conn._running} "
            f"fail_stop={db._fail_stop!r} unclosed={len(db._unclosed)} "
            f"unresolved={len(db._unresolved)} conn_id={id(conn)} "
            f"tracked={[(id(c), db._worker_alive(c)) for c in db._connections]}")
    assert wedged.is_set(), f"the slow-exit fault never ran ({diag})"
    assert verdict is db.CloseVerdict.CLOSED, (
        f"close_db returned {verdict} for a worker that exits well inside its "
        f"10s budget — it did not wait for the thread at all [{diag}]")
    assert alive_at_return is False, (
        f"close_db returned CLOSED without waiting for the worker thread [{diag}]")


async def test_clean_close_leaves_no_live_aiosqlite_worker(fresh_db):
    # The positive half: an ordinary shutdown with no faults must return CLOSED
    # AND have no worker thread left, measured at the moment it returns.
    await db.store_metric("a1", 1.0, _metric())     # publishes the metric conn
    shared, metric = db._conn, db._metric_conn
    assert shared is not None and metric is not None

    verdict = await asyncio.wait_for(db.close_db(timeout=10), timeout=30)
    live = _live_aiosqlite_workers()

    assert verdict is db.CloseVerdict.CLOSED, verdict
    assert live == [], (
        f"close_db reported CLOSED with {len(live)} aiosqlite worker thread(s) "
        "still running")
    for name, conn in (("shared", shared), ("metric", metric)):
        assert conn._running is False, f"{name} wrapper still marked running"
        assert conn.is_alive() is False, f"{name} worker thread still alive"


# ── H5-C. cancellation survives every stage ─────────


def _breaking_execute(conn, *, break_fetch=None, break_close=None, match=None):
    """Patch conn.execute so the CURSOR it returns misbehaves in a chosen
    stage, leaving the execute itself real (it must reach the worker).

    `match` narrows the fault to one statement: breaking every cursor would
    also break the boundary's own BEGIN, so the run would never reach the
    statement under test."""
    real_execute = conn.execute

    async def execute(sql, *a, **k):
        cursor = await real_execute(sql, *a, **k)
        if match is None or (isinstance(sql, str) and match in sql):
            if break_fetch is not None:
                cursor.fetchall = break_fetch
            if break_close is not None:
                cursor.close = break_close
        return cursor

    conn.execute = execute
    return real_execute


async def test_read_cancelled_then_fetch_error_hands_back_a_plain_cancellation(
    fresh_db
):
    # C1: the execute is cancelled while the worker owns it, the fetch then
    # fails, and the cursor closes cleanly. The caller is owed a cancellation —
    # and a PLAIN one: `_CancelledAfterWorkerError` is private vocabulary.
    conn = await db._get_conn()
    probe = WorkerProbe("c1")
    await probe.install(conn)
    await db.store_metric("seed", 1.0, _metric())
    hit = []

    async def failing_fetchall():
        hit.append("fetch")
        raise RuntimeError("fetch failed")

    _breaking_execute(conn, break_fetch=failing_fetchall, match=probe.name)
    task = asyncio.create_task(
        db._fetch_all(f"SELECT {probe.name}(id) AS v FROM metrics"))
    try:
        await probe.wait_entered()
        task.cancel()
        probe.let_go()
        with pytest.raises(asyncio.CancelledError) as excinfo:
            await asyncio.wait_for(task, timeout=20)
    finally:
        probe.let_go()
        del conn.execute

    assert probe.calls >= 1, "the worker never entered the statement"
    assert hit == ["fetch"], "the fetch fault was never reached"
    assert type(excinfo.value) is asyncio.CancelledError, (
        f"an internal exception type reached a public caller: {excinfo.value!r}")
    assert conn.in_transaction is False
    assert db._fail_stop is None, (
        f"a clean cursor close was still treated as unsafe: {db._fail_stop}")
    await db.store_metric("a1", 2.0, _metric())     # the DB is still usable


async def test_cursor_close_failing_under_cancellation_fails_stop(fresh_db):
    # C2: the cursor close itself FAILS while we are cancelled. That is an
    # unclosed cursor pinning a read snapshot, so it must latch fail-stop —
    # today it slips out of the read path as "just a cancellation".
    conn = await db._get_conn()
    probe = WorkerProbe("c2")
    await probe.install(conn)
    await db.store_metric("seed", 1.0, _metric())
    hit = []
    holder = {}
    submitted = asyncio.Event()

    def _boom():
        raise RuntimeError("cursor close failed on the worker")

    real_execute = None

    async def failing_close():
        hit.append("close")
        holder["task"] = asyncio.create_task(
            real_execute(f"SELECT {probe.name}(1)"))
        await probe.wait_entered()
        submitted.set()
        await conn._execute(_boom)        # queues behind the blocked probe

    real_execute = _breaking_execute(conn, break_close=failing_close, match="AS v")
    task = asyncio.create_task(db._fetch_all("SELECT 1 AS v"))
    try:
        await asyncio.wait_for(submitted.wait(), timeout=15)
        await _wait_until_queued(conn, "the failing cursor close")
        task.cancel()
        probe.let_go()
        with pytest.raises(asyncio.CancelledError) as excinfo:
            await asyncio.wait_for(task, timeout=20)
    finally:
        probe.let_go()
        del conn.execute
        if holder.get("task"):
            cursor = (await asyncio.gather(holder["task"], return_exceptions=True))[0]
            if not isinstance(cursor, BaseException):
                try:
                    await asyncio.wait_for(cursor.close(), timeout=5)
                except BaseException:
                    pass

    assert hit == ["close"], "the cursor-close fault was never reached"
    assert probe.calls >= 1, "the worker was never occupied"
    assert db._fail_stop is not None, (
        "a cursor whose close FAILED was not treated as unsafe: the read "
        "snapshot it pins wedges WAL checkpointing and every later write, and "
        "the cancellation carried it straight past the fail-stop latch"
    )
    assert type(excinfo.value) is asyncio.CancelledError, (
        f"an internal exception type reached a public caller: {excinfo.value!r}")
    with pytest.raises(db.DatabaseFailStop):
        await db.store_metric("a1", 2.0, _metric())


async def test_fetch_error_does_not_bury_a_cleanup_cancellation(fresh_db):
    # C1/C5 combined: the fetch fails first, and the cursor close then fails
    # WHILE we are cancelled. The cleanup's cancellation must not be buried
    # under the earlier fetch error — an `except Exception: continue` loop
    # survives that, and shutdown never completes.
    conn = await db._get_conn()
    probe = WorkerProbe("c5")
    await probe.install(conn)
    hit = []
    holder = {}
    submitted = asyncio.Event()
    swallowed = []

    def _boom():
        raise RuntimeError("cursor close failed on the worker")

    real_execute = None

    async def failing_fetchall():
        hit.append("fetch")
        raise RuntimeError("fetch failed")

    async def failing_close():
        hit.append("close")
        if "task" not in holder:
            holder["task"] = asyncio.create_task(
                real_execute(f"SELECT {probe.name}(1)"))
            await probe.wait_entered()
        submitted.set()
        await conn._execute(_boom)

    real_execute = _breaking_execute(
        conn, break_fetch=failing_fetchall, break_close=failing_close, match="AS v")

    async def maintenance_like():
        for _ in range(3):                   # bounded so a RED run cannot spin
            try:
                await db._fetch_all("SELECT 1 AS v")
            except asyncio.CancelledError:
                raise
            except Exception:
                swallowed.append("swallowed")
        return "the loop outlived its cancellation"

    task = asyncio.create_task(maintenance_like())
    try:
        await asyncio.wait_for(submitted.wait(), timeout=15)
        await _wait_until_queued(conn, "the failing cursor close")
        task.cancel()
        probe.let_go()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=25)
    finally:
        probe.let_go()
        del conn.execute
        if holder.get("task"):
            cursor = (await asyncio.gather(holder["task"], return_exceptions=True))[0]
            if not isinstance(cursor, BaseException):
                try:
                    await asyncio.wait_for(cursor.close(), timeout=5)
                except BaseException:
                    pass

    assert hit[:2] == ["fetch", "close"], f"both faults must be reached: {hit}"
    assert probe.calls >= 1, "the worker was never occupied"
    assert swallowed == [], (
        "the fetch error buried the cleanup's cancellation, so the loop kept "
        "running through its own cancellation")
    assert db._fail_stop is not None, "an unclosed cursor did not fail-stop"


async def test_tx_handle_release_error_does_not_bury_its_cancellation(fresh_db):
    # C3: inside the write boundary. The execute is cancelled on the worker and
    # the cursor release then raises. `finally: cancelled = await release(...)`
    # loses BOTH the cancellation and the classification when it raises.
    conn = await db._get_conn()
    probe = WorkerProbe("c3")
    await probe.install(conn)
    hit = []

    async def failing_close():
        hit.append("close")
        raise RuntimeError("cursor release failed")

    _breaking_execute(conn, break_close=failing_close, match=probe.name)

    async def writer():
        async with db.write_transaction() as tx:
            await tx.fetch_all(f"SELECT {probe.name}(1)")

    task = asyncio.create_task(writer())
    try:
        await probe.wait_entered()
        task.cancel()
        probe.let_go()
        with pytest.raises(asyncio.CancelledError) as excinfo:
            await asyncio.wait_for(task, timeout=25)
    finally:
        probe.let_go()
        del conn.execute

    assert probe.calls >= 1, "the worker never entered the statement"
    assert hit, "the cursor-release fault was never reached"
    assert type(excinfo.value) is asyncio.CancelledError, (
        f"an internal exception type reached a public caller: {excinfo.value!r}")
    assert db._fail_stop is not None, "an unreleased cursor did not fail-stop"
    with pytest.raises(db.DatabaseFailStop):
        await db.store_metric("a1", 2.0, _metric())


async def test_durable_commit_with_lost_response_under_cancellation(fresh_db, tmp_path):
    # C4: the COMMIT is DURABLE, the wrapper then reports an error, and the
    # caller was cancelled while the worker held the COMMIT. All three are true
    # at once: fail-stop latches, no ROLLBACK is submitted over a durable
    # commit, and THIS caller is owed a plain CancelledError.
    conn = await db._get_conn()
    probe = WorkerProbe("c4")
    await probe.install(conn)
    real_commit = conn.commit
    hit = []
    holder = {}
    submitted = asyncio.Event()
    rollbacks = []
    real_rollback = conn.rollback

    async def counting_rollback():
        rollbacks.append(1)
        return await real_rollback()

    async def commit_then_lose_response():
        hit.append("commit")
        holder["task"] = asyncio.create_task(_run_probe(conn, probe.name))
        await probe.wait_entered()
        inner = asyncio.ensure_future(real_commit())
        await _wait_until_queued(conn, "the COMMIT")
        submitted.set()
        await inner                       # DURABLE once the gate releases
        hit.append("durable")
        raise RuntimeError("commit response lost")

    conn.commit = commit_then_lose_response
    conn.rollback = counting_rollback
    task = asyncio.create_task(db.store_net_audit("a1", 1.0, [_event()], []))
    try:
        await asyncio.wait_for(submitted.wait(), timeout=15)
        task.cancel()
        probe.let_go()
        with pytest.raises(asyncio.CancelledError) as excinfo:
            await asyncio.wait_for(task, timeout=25)
    finally:
        probe.let_go()
        if "commit" in conn.__dict__:
            del conn.commit
        conn.rollback = real_rollback
        if holder.get("task"):
            await asyncio.gather(holder["task"], return_exceptions=True)

    assert hit == ["commit", "durable"], f"the fault sequence never ran: {hit}"
    assert type(excinfo.value) is asyncio.CancelledError, (
        f"an internal exception type reached a public caller: {excinfo.value!r}")
    # This one leaves through write_transaction's own translation, so it gates
    # that path's cause the way the _TxHandle tests gate theirs.
    assert not isinstance(
        excinfo.value.__cause__,
        (db._CancelledAfterWorkerError, db._OutcomeUnknown, db._ChildCancelled)), (
        "a private classification became the public direct cause: "
        f"{excinfo.value.__cause__!r}")
    assert rollbacks == [], "a ROLLBACK was submitted over a DURABLE commit"
    assert db._fail_stop is not None, "a lost commit response did not fail-stop"
    with pytest.raises(db.DatabaseFailStop):
        await db.store_metric("a1", 2.0, _metric())

    # The commit really is durable — read it back outside the fail-stopped module.
    raw = sqlite3.connect(str(tmp_path / "t.db"))
    try:
        assert raw.execute("SELECT COUNT(*) FROM net_conn_events").fetchone()[0] == 1
    finally:
        raw.close()


async def test_cancellation_before_the_commit_submission_rolls_back(
    fresh_db, tmp_path, monkeypatch
):
    # The other half of the COMMIT rule: a cancellation already CONFIRMED before
    # the COMMIT is submitted must end in a ROLLBACK, not in a commit that the
    # caller is then told was cancelled.
    #
    # Where that rule is actually ENFORCED matters, and this test pins it: the
    # boundary never reaches _commit_transaction at all, because a cancellation
    # delivered inside the body leaves through the abort path. The matching
    # branch inside _commit_transaction is a backstop for a cleanup-collected
    # cancellation, and nothing in this module can reach it today — every
    # cursor is released before its statement returns, so _cleanup_cursors has
    # nothing left to be cancelled during.
    entered_commit = []
    real_commit_transaction = db._commit_transaction

    async def counting_commit_transaction(*args, **kwargs):
        entered_commit.append(1)
        return await real_commit_transaction(*args, **kwargs)

    monkeypatch.setattr(db, "_commit_transaction", counting_commit_transaction)
    conn = await db._get_conn()
    probe = WorkerProbe("cbefore")
    await probe.install(conn)
    commits = []
    real_commit = conn.commit

    async def counting_commit():
        commits.append(1)
        return await real_commit()

    conn.commit = counting_commit

    async def writer():
        async with db.write_transaction() as tx:
            await tx.execute(
                "INSERT INTO net_conn_events (agent_id, ts, event, proto) "
                "VALUES ('a1', 1.0, 'open', 'tcp')")
            await tx.fetch_all(f"SELECT {probe.name}(1)")

    task = asyncio.create_task(writer())
    try:
        await probe.wait_entered()
        task.cancel()
        probe.let_go()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=25)
    finally:
        probe.let_go()
        if "commit" in conn.__dict__:
            del conn.commit

    assert probe.calls >= 1, "the worker never entered the statement"
    assert commits == [], "a COMMIT was submitted for an already-cancelled write"
    assert entered_commit == [], (
        "the boundary reached the COMMIT stage for a write whose caller was "
        "already cancelled — rule 1 is enforced by the abort path")
    assert conn.in_transaction is False, "the transaction was left open"
    rows = await db.get_net_conn_events("a1")
    assert rows == [], f"a cancelled write was committed anyway: {rows}"


# ── H5-D. the deadline gate is structural, not a stopwatch ──


@pytest.mark.expect_db_leftovers
async def test_close_db_hands_the_second_connection_only_what_is_left(
    fresh_db, monkeypatch
):
    # A wall-clock bound cannot tell "one budget spent twice" from "a fast
    # machine". The structural fact is the only real gate: the timeout handed
    # to the SECOND connection close must be what the first stage LEFT, so the
    # close stage can never be handed more than the whole shutdown budget.
    monkeypatch.setattr(db, "_CLEANUP_CANCEL_BUDGET", 4)
    shared = await db._get_conn()
    await db.store_metric("a1", 1.0, _metric())
    metric = db._metric_conn
    assert metric is not None and metric is not shared
    hit = []

    async def close_that_never_returns():
        hit.append(1)
        await asyncio.Event().wait()

    shared.close = close_that_never_returns
    metric.close = close_that_never_returns

    budget = 0.4
    handed = []
    real_submit = db._submit

    async def recording_submit(coro, what, timeout=None, **kwargs):
        if "connection close" in what:
            handed.append((what, timeout))
        return await real_submit(coro, what, timeout=timeout, **kwargs)

    monkeypatch.setattr(db, "_submit", recording_submit)
    try:
        verdict = await asyncio.wait_for(db.close_db(timeout=budget), timeout=30)
        assert hit, "no connection-close fault was reached"
        assert verdict is db.CloseVerdict.RESTART_REQUIRED, verdict
        assert len(handed) == 2, f"both connections must be attempted: {handed}"
        first, second = handed[0][1], handed[1][1]
        assert first is not None and second is not None, handed
        assert second < first, (
            f"the second close was handed a fresh budget, not the remainder: {handed}")
        assert first + second <= budget + 0.05, (
            f"the close stage was handed {first + second:.3f}s out of a "
            f"{budget}s shutdown budget — the deadline is re-spent per "
            f"connection: {handed}")
        assert len(db._unclosed) == 2, db._unclosed
    finally:
        await _force_close([shared, metric])


async def test_conftest_reports_an_unregistered_live_worker(fresh_db, tmp_path):
    # The teardown gate must not depend on app.database's own registries: a
    # connection the product never recorded is exactly the leak worth catching.
    import tests.conftest as suite_conftest

    leaked = await aiosqlite.connect(str(tmp_path / "leaked.db"))
    try:
        assert leaked.is_alive()
        unaccounted = await suite_conftest.unaccounted_live_workers(db, [leaked])
        assert any(c is leaked for c in unaccounted), (
            "the teardown gate did not see a live aiosqlite worker that no "
            "app.database registry knows about")
        # A connection the product IS accounting for is not reported.
        assert await suite_conftest.unaccounted_live_workers(db, [db._conn]) == []
    finally:
        await asyncio.wait_for(leaked.close(), timeout=5)


# ── H5-D (cont). foreign-key gate: exact message, ordering, recovery ──

_LEGACY_ORPHAN_NO_USERS_SQL = _LEGACY_ORPHAN_SQL.replace(
    "INSERT INTO users VALUES ('keep@example.com','h',NULL,0,0,1.0,'admin',1,0);\n", ""
)


@pytest.fixture
async def legacy_orphan_db_without_users(tmp_path, monkeypatch):
    """The same legacy shape, but with NO users left at all — so init_db would
    otherwise reach the default-admin branch and write a plaintext credential
    file for a database it is about to refuse."""
    path = tmp_path / "legacy-empty.db"
    raw = sqlite3.connect(str(path))
    try:
        raw.executescript(_LEGACY_ORPHAN_NO_USERS_SQL)
        raw.commit()
        assert raw.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0
        assert raw.execute(
            "SELECT COUNT(*) FROM user_host_accounts").fetchone()[0] == 2
    finally:
        raw.close()

    monkeypatch.setattr(db, "_db_path", str(path))
    monkeypatch.setattr(db, "_conn", None)
    monkeypatch.setattr(db, "_metric_conn", None, raising=False)
    monkeypatch.setattr(db, "_op_lock", asyncio.Lock(), raising=False)
    monkeypatch.setattr(db, "_fail_stop", None, raising=False)
    yield path
    assert db._db_path == str(path), db._db_path
    await db.close_db()
    db._closed = False


async def test_foreign_key_failure_names_the_table_and_the_exact_count(
    legacy_orphan_db
):
    with pytest.raises(db.SchemaIntegrityViolation) as excinfo:
        await db.init_db()
    message = str(excinfo.value)
    assert "user_host_accounts=2 row(s)" in message, (
        f"the failure must name the table AND the exact row count: {message!r}")


async def test_foreign_key_check_runs_before_any_non_database_side_effect(
    legacy_orphan_db_without_users
):
    # The default-admin branch writes a plaintext password file into the data
    # directory. Doing that for a database startup is about to refuse leaves a
    # credential on disk for a server that never comes up.
    pw_file = os.path.join(os.path.dirname(str(legacy_orphan_db_without_users)),
                           "initial_admin_password")
    assert not os.path.exists(pw_file)
    with pytest.raises(db.SchemaIntegrityViolation):
        await db.init_db()
    assert not os.path.exists(pw_file), (
        "init_db wrote the one-time admin password file for a database whose "
        "foreign keys it then refused"
    )


async def test_startup_succeeds_once_an_operator_has_resolved_the_violations(
    legacy_orphan_db
):
    with pytest.raises(db.SchemaIntegrityViolation):
        await db.init_db()
    await db.close_db()

    # The approved migration an operator runs — deliberately OUTSIDE the
    # product, which never touches these rows itself.
    raw = sqlite3.connect(str(legacy_orphan_db))
    try:
        raw.execute("PRAGMA foreign_keys=ON")
        raw.execute("DELETE FROM user_host_accounts WHERE user_email = ?", (_GHOST,))
        raw.commit()
        assert raw.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        raw.close()

    # A NEW process: fresh module state, same file.
    db._conn = None
    db._metric_conn = None
    db._op_lock = asyncio.Lock()
    db._fail_stop = None
    db._closing = False
    db._closed = False
    db._unresolved.clear()
    db._unclosed.clear()
    db._late_results.clear()

    await db.init_db()
    assert db._fail_stop is None, db._fail_stop
    assert await db._fetch_all_unrestricted("PRAGMA foreign_key_check") == []
    assert await db.get_user_host_accounts(_GHOST) == {}
    await db.store_metric("a1", 1.0, _metric())      # the database really serves


async def test_a_leaked_worker_makes_the_teardown_fail(tmp_path):
    """Prove the teardown gate really fires — end to end, in a real pytest run.

    A gate nobody has watched fail is a gate nobody knows works. This runs one
    deliberately leaky test in a child pytest process (so the leak cannot break
    this run) under the SAME conftest, and requires it to end in a teardown
    error naming the unaccounted worker."""
    repo = Path(db.__file__).parents[2]
    leaky = tmp_path / "test_leaky_worker.py"
    leaky.write_text(
        "import aiosqlite\n"
        "\n"
        "\n"
        "async def test_leaks_a_live_worker(tmp_path):\n"
        "    # Opened, never closed, never handed to app.database.\n"
        "    conn = await aiosqlite.connect(str(tmp_path / 'leak.db'))\n"
        "    assert conn.is_alive()\n"
    )
    (tmp_path / "conftest.py").write_text(
        "from tests.conftest import *  # noqa: F401,F403\n"
        "from tests.conftest import reset_db_module_state  # noqa: F401\n"
    )
    (tmp_path / "pytest.ini").write_text(
        "[pytest]\nasyncio_mode = auto\nasyncio_default_fixture_loop_scope = function\n"
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(repo), str(repo / "backend"), env.get("PYTHONPATH", "")])
    proc = await asyncio.to_thread(
        subprocess.run,
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
         str(leaky)],
        cwd=str(tmp_path), env=env, capture_output=True, text=True, timeout=180,
    )
    output = proc.stdout + proc.stderr
    assert proc.returncode != 0, (
        "a test that leaks a live aiosqlite worker was reported as passing:\n"
        + output)
    assert "aiosqlite worker thread(s) were still running" in output, output
    assert "no app.database registry accounts for" in output, output


# ═════════════════════════════════════════════════════
# Hardening round 6 — defects found by independent review of round 5
# ═════════════════════════════════════════════════════


async def test_late_cursor_that_died_with_its_connection_is_not_a_failure(fresh_db):
    # SQLite drops every cursor when its connection closes, and aiosqlite
    # reports a submission on a closed wrapper as ValueError("Connection
    # closed") — NOT sqlite3.ProgrammingError. Treating that as "could not be
    # closed" latches restart-required over a cursor that is provably gone, and
    # keeps _late_results non-empty for the rest of the process.
    conn = await db._get_conn()
    cursor = await conn.execute("SELECT 1")
    await asyncio.wait_for(conn.close(), timeout=5)
    db._conn = None
    db._late_results.append(
        db._LateResult("read execute", db._Disposal.CURSOR, cursor))

    await asyncio.wait_for(db._dispose_late_results(5.0), timeout=20)

    assert db._late_results == [], (
        "a cursor that SQLite already dropped with its connection was kept as "
        f"un-disposed: {db._late_results}")
    assert db._fail_stop is None, (
        f"a cursor that is provably gone latched restart-required: {db._fail_stop}")


async def test_close_db_closes_a_late_cursor_while_its_connection_is_open(fresh_db):
    # A late cursor pins a read snapshot on a connection that is still
    # published. It has to be closed BEFORE that connection goes: afterwards
    # the close can only ever be inferred, never confirmed.
    conn = await db._get_conn()
    cursor = await conn.execute("SELECT 1")
    running_when_closed = []
    real_close = cursor.close

    async def recording_close():
        running_when_closed.append(conn._running)
        return await real_close()

    cursor.close = recording_close
    db._late_results.append(
        db._LateResult("read execute", db._Disposal.CURSOR, cursor))

    verdict = await asyncio.wait_for(db.close_db(timeout=10), timeout=30)

    assert running_when_closed == [True], (
        "the late cursor was closed only after its own connection had gone: "
        f"{running_when_closed}")
    assert verdict is db.CloseVerdict.CLOSED, verdict
    assert db._late_results == [], db._late_results
    assert db._fail_stop is None, db._fail_stop


async def test_cancellation_during_a_rejected_connection_build_reaches_the_caller(
    fresh_db, monkeypatch
):
    # The caller is cancelled while the connect is on the worker, and the
    # connection is then REJECTED by verification. _submit recovered the
    # cancellation; raising the verification error instead throws it away —
    # the same swallow every other stage of this module was fixed for.
    await db.close_db()
    db._closed = False
    monkeypatch.setattr(db, "_conn", None)
    rejected = []

    def keep_fk_off(conn):
        real_execute = conn.execute

        async def execute(sql, *a, **k):
            cursor = await real_execute(sql, *a, **k)
            if isinstance(sql, str) and "foreign_keys" in sql and "=" not in sql:
                rejected.append(1)

                async def fetchall():
                    return [(0,)]
                cursor.fetchall = fetchall
            return cursor

        conn.execute = execute

    made = _connect_factory(monkeypatch, keep_fk_off)
    gate = ConnectGate(monkeypatch, db._db_path)
    task = asyncio.create_task(db.blacklist_token("t", 1.0))
    try:
        await gate.wait_entered()
        task.cancel()
        gate.let_go()
        with pytest.raises(asyncio.CancelledError) as excinfo:
            await asyncio.wait_for(task, timeout=25)
    finally:
        gate.let_go()

    assert gate.calls >= 1, "sqlite3.connect never ran on the worker"
    assert made, "the connection was never built"
    assert rejected, "the verification fault was never reached"
    assert type(excinfo.value) is asyncio.CancelledError, (
        f"an internal exception type reached a public caller: {excinfo.value!r}")
    assert db._conn is None, "a rejected connection was published"


async def test_worker_join_is_not_queued_behind_the_shared_executor(fresh_db):
    # asyncio.to_thread runs on the loop's DEFAULT executor, which ordinary
    # application code parks: alert_service wraps a blocking getaddrinfo in
    # asyncio.to_thread under a wait_for, and wait_for cancels the AWAIT while
    # the pool thread keeps blocking in the resolver. A join queued behind that
    # is unbounded — with the operation lock held — so close_db's single
    # absolute deadline stops being a wall-clock bound at all.
    conn = await db._get_conn()
    loop = asyncio.get_running_loop()
    release = threading.Event()
    parked: list = []

    def park():
        parked.append(1)
        release.wait(timeout=20)

    hogs = [loop.run_in_executor(None, park) for _ in range(64)]
    try:
        # Poll WITHOUT touching the pool: a to_thread here would queue behind
        # the hogs and quietly wait them out, which is exactly the bug.
        deadline = loop.time() + 5
        while len(parked) < 8 and loop.time() < deadline:
            await asyncio.sleep(0.01)
        assert len(parked) >= 8, f"the executor was never busy ({len(parked)})"
        started = loop.time()
        gone = await asyncio.wait_for(db._join_worker(conn, 0.3), timeout=5)
        elapsed = loop.time() - started
        assert gone is False, "the idle worker should still be alive"
        assert elapsed < 3.0, (
            f"_join_worker took {elapsed:.2f}s for a 0.3s budget — it queued "
            "behind the shared default executor instead of running on a "
            "thread the database controls")
    finally:
        release.set()
        await asyncio.gather(*hogs, return_exceptions=True)


@pytest.mark.expect_db_leftovers
async def test_close_db_refuses_closed_while_any_connection_it_built_is_alive(
    fresh_db, tmp_path
):
    # The connection registry is the only thing that speaks for a connection no
    # other registry names any more — not _conn, not _metric_conn, not
    # _unclosed, not a late result, not an unresolved submission.
    stray = await aiosqlite.connect(str(tmp_path / "stray.db"))
    db._connections.add(stray)
    try:
        assert stray.is_alive()
        verdict = await asyncio.wait_for(db.close_db(timeout=5), timeout=20)
        assert verdict is db.CloseVerdict.RESTART_REQUIRED, (
            f"close_db returned {verdict} with a connection it built still "
            "running, because no other registry happened to name it")
        assert any(c is stray for c in db._unclosed), db._unclosed
        assert db._fail_stop is not None
    finally:
        await asyncio.wait_for(stray.close(), timeout=5)


async def test_a_stage_failure_names_itself_and_keeps_its_cause(fresh_db):
    # settle() built its DatabaseFailStop from the module-wide latch, which
    # deliberately keeps the FIRST reason ever recorded — and it dropped
    # `outcome.error` entirely on the problems path. An operator reading a
    # restart-required database then sees the cleanup failure only, with no
    # trace of the statement error that caused it.
    conn = await db._get_conn()
    hit = []

    async def failing_fetchall():
        hit.append("fetch")
        raise sqlite3.OperationalError("the fetch itself failed")

    async def failing_close():
        hit.append("close")
        raise RuntimeError("and its cursor could not be closed")

    _breaking_execute(conn, break_fetch=failing_fetchall,
                      break_close=failing_close, match="AS v")
    try:
        with pytest.raises(db.DatabaseFailStop) as excinfo:
            await asyncio.wait_for(db._fetch_all("SELECT 1 AS v"), timeout=20)
    finally:
        del conn.execute

    assert hit == ["fetch", "close"], f"both faults must be reached: {hit}"
    message = str(excinfo.value)
    assert "cursor could not be closed" in message, (
        f"the failure does not name the step that actually failed: {message}")
    # Two independent carriers, so neither can stand in for the other: the
    # reason an operator reads, and the cause a traceback follows.
    assert "the fetch itself failed" in message, (
        f"the reason does not mention the error that broke the read: {message}")
    assert isinstance(excinfo.value.__cause__, sqlite3.OperationalError), (
        "the error that broke the statement was not kept as the cause: "
        f"{excinfo.value.__cause__!r}")


# ═════════════════════════════════════════════════════
# Hardening round 7 — the close deadline is a wall-clock bound,
# every boundary hands back one outcome, and the teardown gate
# does not take the marker's word about a forgotten worker
# ═════════════════════════════════════════════════════


def _repo_root():
    return Path(db.__file__).parents[2]


def _child_env():
    env = dict(os.environ)
    repo = _repo_root()
    env["PYTHONPATH"] = os.pathsep.join(
        [str(repo), str(repo / "backend"), env.get("PYTHONPATH", "")])
    env.setdefault("GLASSOPS_SECRET_KEY",
                   "test-secret-key-for-pytest-0123456789abcdef")
    return env


async def _run_child(args, cwd, timeout=180):
    """Run a child process to completion, and NEVER leave it behind.

    These children deliberately leak a non-daemon aiosqlite worker or open a
    database of their own; one that outlives the test would hang the whole
    suite, so it is killed rather than waited on."""
    def run():
        proc = subprocess.Popen(
            args, cwd=str(cwd), env=_child_env(), text=True,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        try:
            out, _ = proc.communicate(timeout=timeout)
            return proc.returncode, out
        except subprocess.TimeoutExpired:
            proc.kill()
            out, _ = proc.communicate(timeout=30)
            return None, out
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=30)

    return await asyncio.to_thread(run)


# ── H7-A. the close deadline covers the queue as well as the join ──


def _wedge_worker_before_its_stop_sentinel(conn, name, loop, blocker, wedged):
    """Stop this connection's worker AFTER its raw close and BEFORE the stop
    sentinel, so a join for it can never succeed."""
    real_stop = conn._stop_running

    def blocking_item():
        wedged[name].set()
        blocker.wait(timeout=30)

    def wedging_stop():
        conn._tx.put_nowait((loop.create_future(), blocking_item))
        real_stop()

    conn._stop_running = wedging_stop
    return real_stop


@pytest.mark.expect_db_leftovers
async def test_close_db_deadline_holds_when_the_first_join_is_cancelled(fresh_db):
    # A join runs on a thread, so cancelling the AWAIT does not stop it. The
    # second connection's join then queues behind a job that is still running,
    # and spends a timeout that was computed before any of that happened — so
    # a 0.4s shutdown budget takes ~0.8s, with the operation lock held for all
    # of it. The queue wait and the join itself have to draw down ONE deadline.
    shared = await db._get_conn()
    await db.store_metric("a1", 1.0, _metric())
    metric = db._metric_conn
    assert metric is not None and metric is not shared

    loop = asyncio.get_running_loop()
    blocker = threading.Event()
    wedged = {"shared": threading.Event(), "metric": threading.Event()}
    restore = {
        name: _wedge_worker_before_its_stop_sentinel(conn, name, loop, blocker, wedged)
        for name, conn in (("shared", shared), ("metric", metric))
    }
    names = {id(shared): "shared", id(metric): "metric"}

    # Watch the boundary's OWN join requests: what budget each was given, and
    # how long it actually took. That is the contract — one absolute deadline
    # shared by every stage — independent of how waiting is implemented.
    joins: list = []
    first_join_started = asyncio.Event()
    real_join_worker = db._join_worker

    async def recording_join_worker(conn, timeout):
        first_join_started.set()
        began = loop.time()
        try:
            return await real_join_worker(conn, timeout)
        finally:
            joins.append({"name": names.get(id(conn), "?"), "budget": timeout,
                          "began": began - started, "took": loop.time() - began})

    budget = 0.5
    started = loop.time()
    db._join_worker = recording_join_worker
    closer = asyncio.create_task(db.close_db(timeout=budget))
    try:
        await asyncio.wait_for(first_join_started.wait(), timeout=10)
        closer.cancel()
        result = (await asyncio.gather(closer, return_exceptions=True))[0]
        elapsed = loop.time() - started
    finally:
        db._join_worker = real_join_worker
        blocker.set()
        for name, conn in (("shared", shared), ("metric", metric)):
            conn._stop_running = restore[name]
        for conn in (shared, metric):
            assert await _worker_gone(conn, 15), "a wedged worker never exited"

    assert wedged["shared"].is_set(), "the shared wedge never ran"
    assert wedged["metric"].is_set(), "the metric wedge never ran"
    assert isinstance(result, db.CloseVerdict), f"close_db raised: {result!r}"
    assert result is db.CloseVerdict.RESTART_REQUIRED, result

    assert len(joins) == 2, f"both connections must be joined: {joins}"
    for entry in joins:
        # Every stage draws down the SAME absolute deadline: a join may not run
        # past the budget it was given, and it may not start so late that its
        # budget reaches beyond the shutdown deadline.
        assert entry["took"] <= entry["budget"] + 0.2, (
            f"the {entry['name']} join ran {entry['took']:.3f}s on a "
            f"{entry['budget']:.3f}s budget — leftover work from a cancelled "
            f"join let it re-spend the deadline: {joins}")
        assert entry["began"] + entry["budget"] <= budget + 0.05, (
            f"the {entry['name']} join was given a budget reaching "
            f"{entry['began'] + entry['budget']:.3f}s into a {budget}s "
            f"shutdown: {joins}")
    assert elapsed <= budget + 0.35, (
        f"close_db(timeout={budget}) returned after {elapsed:.2f}s: {joins}")


# ── H7-B. one outcome out of every boundary ──


@pytest.mark.expect_db_leftovers
async def test_unpublished_connection_whose_discard_also_fails_reports_fail_stop(
    fresh_db, monkeypatch
):
    # B1: the connection fails verification AND cannot be discarded. The
    # database is restart-required, so the caller has to be told that — not
    # handed the configuration error as if a retry might work.
    await db.close_db()
    db._closed = False
    monkeypatch.setattr(db, "_conn", None)
    hits = {"configure": 0, "close": 0}

    def reject_and_refuse_to_close(conn):
        real_execute = conn.execute

        async def execute(sql, *a, **k):
            cursor = await real_execute(sql, *a, **k)
            if isinstance(sql, str) and "foreign_keys" in sql and "=" not in sql:
                hits["configure"] += 1

                async def fetchall():
                    return [(0,)]
                cursor.fetchall = fetchall
            return cursor

        async def close():
            hits["close"] += 1
            raise RuntimeError("the discard close failed too")

        conn.execute = execute
        conn.close = close

    made = _connect_factory(monkeypatch, reject_and_refuse_to_close)
    try:
        with pytest.raises(BaseException) as excinfo:
            await asyncio.wait_for(db.blacklist_token("t", 1.0), timeout=20)
        assert hits["configure"] >= 1, "the verification fault was never reached"
        assert hits["close"] >= 1, "the discard fault was never reached"
        assert isinstance(excinfo.value, db.DatabaseFailStop), (
            "a connection that could neither be published nor discarded was "
            f"reported as an ordinary failure: {excinfo.value!r}")
        assert db._fail_stop is not None
        assert db._conn is None
        assert any(c is made[0] for c in db._unclosed), db._unclosed
    finally:
        await _force_close(made)


async def test_cancellation_while_a_built_connection_is_abandoned_reaches_the_caller(
    fresh_db, monkeypatch
):
    # B2: the connection finishes building exactly as the database starts
    # closing, so it is discarded instead of published. A cancellation
    # collected during that discard is still owed to THIS caller.
    await db.close_db()
    db._closed = False
    monkeypatch.setattr(db, "_conn", None)
    discarding = asyncio.Event()
    proceed = asyncio.Event()
    closed = []

    def gate_the_discard(conn):
        real_close = conn.close

        async def close():
            discarding.set()
            await proceed.wait()
            await real_close()
            closed.append(conn)

        conn.close = close

    made = _connect_factory(monkeypatch, gate_the_discard)
    real_open = db._open_connection

    async def build_then_start_closing(what):
        conn = await real_open(what)
        db._closing = True          # close_db began between build and publish
        return conn

    monkeypatch.setattr(db, "_open_connection", build_then_start_closing)
    task = asyncio.create_task(db.blacklist_token("t", 1.0))
    try:
        await asyncio.wait_for(discarding.wait(), timeout=20)
        task.cancel()
        proceed.set()
        with pytest.raises(asyncio.CancelledError) as excinfo:
            await asyncio.wait_for(task, timeout=25)
    finally:
        proceed.set()
        db._closing = False
        await _force_close(made)

    assert made, "no connection was built"
    assert closed, "the abandoned connection was never really closed"
    assert type(excinfo.value) is asyncio.CancelledError, (
        f"an internal exception type reached a public caller: {excinfo.value!r}")
    assert db._conn is None, "a connection was published while closing"


class _RowsThatCannotBeMaterialised:
    """Rows whose iteration fails — a row_factory raising, in practice."""

    def __init__(self, hit):
        self._hit = hit

    def __iter__(self):
        self._hit.append("materialise")
        raise RuntimeError("row materialisation failed")


async def test_read_row_materialisation_error_does_not_bury_the_cancellation(
    fresh_db
):
    # B3: the cancellation _submit recovered from the FETCH is recorded only
    # after list(...) has succeeded, so a row that cannot be materialised takes
    # the cancellation down with it and the caller is handed a plain error.
    conn = await db._get_conn()
    probe = WorkerProbe("b3read")
    await probe.install(conn)
    hit = []
    holder = {}
    submitted = asyncio.Event()
    real_execute = None

    def _noop():
        return None

    async def occupying_fetchall():
        holder["task"] = asyncio.create_task(
            real_execute(f"SELECT {probe.name}(1)"))
        await probe.wait_entered()
        submitted.set()
        await conn._execute(_noop)          # queues behind the blocked probe
        return _RowsThatCannotBeMaterialised(hit)

    real_execute = _breaking_execute(conn, break_fetch=occupying_fetchall,
                                     match="AS v")
    task = asyncio.create_task(db._fetch_all("SELECT 1 AS v"))
    try:
        await asyncio.wait_for(submitted.wait(), timeout=20)
        await _wait_until_queued(conn, "the fetch's follow-up statement")
        task.cancel()
        probe.let_go()
        with pytest.raises(asyncio.CancelledError) as excinfo:
            await asyncio.wait_for(task, timeout=25)
    finally:
        probe.let_go()
        del conn.execute
        if holder.get("task"):
            cursor = (await asyncio.gather(holder["task"],
                                           return_exceptions=True))[0]
            if not isinstance(cursor, BaseException):
                try:
                    await asyncio.wait_for(cursor.close(), timeout=5)
                except BaseException:
                    pass

    assert probe.calls >= 1, "the worker was never occupied"
    assert hit == ["materialise"], f"the materialisation fault never ran: {hit}"
    assert type(excinfo.value) is asyncio.CancelledError, (
        f"an internal exception type reached a public caller: {excinfo.value!r}")
    assert isinstance(excinfo.value.__cause__, RuntimeError), (
        f"the error that broke the read was dropped: {excinfo.value.__cause__!r}")


async def test_tx_row_materialisation_error_does_not_bury_the_cancellation(fresh_db):
    # B3, same hole inside the write boundary's own fetch.
    conn = await db._get_conn()
    probe = WorkerProbe("b3tx")
    await probe.install(conn)
    hit = []
    holder = {}
    submitted = asyncio.Event()
    real_execute = None

    def _noop():
        return None

    async def occupying_fetchall():
        holder["task"] = asyncio.create_task(
            real_execute(f"SELECT {probe.name}(1)"))
        await probe.wait_entered()
        submitted.set()
        await conn._execute(_noop)
        return _RowsThatCannotBeMaterialised(hit)

    real_execute = _breaking_execute(conn, break_fetch=occupying_fetchall,
                                     match="AS v")

    async def writer():
        async with db.write_transaction() as tx:
            await tx.fetch_all("SELECT 1 AS v")

    task = asyncio.create_task(writer())
    try:
        await asyncio.wait_for(submitted.wait(), timeout=20)
        await _wait_until_queued(conn, "the fetch's follow-up statement")
        task.cancel()
        probe.let_go()
        with pytest.raises(asyncio.CancelledError) as excinfo:
            await asyncio.wait_for(task, timeout=25)
    finally:
        probe.let_go()
        del conn.execute
        if holder.get("task"):
            cursor = (await asyncio.gather(holder["task"],
                                           return_exceptions=True))[0]
            if not isinstance(cursor, BaseException):
                try:
                    await asyncio.wait_for(cursor.close(), timeout=5)
                except BaseException:
                    pass

    assert probe.calls >= 1, "the worker was never occupied"
    assert hit == ["materialise"], f"the materialisation fault never ran: {hit}"
    assert type(excinfo.value) is asyncio.CancelledError, (
        f"an internal exception type reached a public caller: {excinfo.value!r}")
    assert conn.in_transaction is False, "the transaction was left open"


async def test_commit_reporting_success_over_an_open_transaction_fails_stop(
    fresh_db
):
    # B4: aiosqlite saying the COMMIT is done is not SQLite having left the
    # transaction. Returning success on the submission alone releases the
    # operation lock over a transaction that is still open — the exact state
    # the whole boundary exists to prevent.
    conn = await db._get_conn()
    hit = []

    async def commit_that_reports_success_without_committing():
        hit.append(1)
        return None

    conn.commit = commit_that_reports_success_without_committing
    try:
        with pytest.raises(db.DatabaseFailStop):
            await asyncio.wait_for(
                db.store_net_audit("a1", 1.0, [_event()], []), timeout=20)
    finally:
        if "commit" in conn.__dict__:
            del conn.commit
        if conn.in_transaction:
            await asyncio.wait_for(conn.rollback(), timeout=5)

    assert hit, "the fault was never reached"
    assert db._fail_stop is not None, (
        "a COMMIT that left the transaction open was accepted as a success")
    assert conn.in_transaction is False, (
        "the operation lock was released over an open transaction")


async def test_ordinary_commit_still_succeeds(fresh_db):
    # The other side of B4: verifying the transaction really closed must not
    # break the ordinary aiosqlite commit path.
    await db.store_net_audit("a1", 1.0, [_event()], [])
    assert await db.get_net_conn_events("a1")
    conn = await db._get_conn()
    assert conn.in_transaction is False
    assert db._fail_stop is None, db._fail_stop


# ── H7-C. the teardown gate does not take the marker's word ──


async def test_a_declared_leftover_does_not_excuse_a_forgotten_worker(tmp_path):
    # `expect_db_leftovers` says "this test leaves a resource the product is
    # TRACKING". It must not also wave through a worker the product forgot,
    # which is a different failure with a different fix.
    leaky = tmp_path / "test_marked_but_forgotten.py"
    leaky.write_text(
        "import aiosqlite\n"
        "import pytest\n"
        "import app.database as db\n"
        "\n"
        "\n"
        "@pytest.mark.expect_db_leftovers\n"
        "async def test_declares_one_leftover_and_leaks_another_worker(tmp_path):\n"
        "    # An EXPECTED leftover, recorded in the product's own registry.\n"
        "    expected = await aiosqlite.connect(str(tmp_path / 'expected.db'))\n"
        "    db._unclosed.append(expected)\n"
        "    # And a second worker no registry knows about.\n"
        "    orphan = await aiosqlite.connect(str(tmp_path / 'orphan.db'))\n"
        "    assert orphan.is_alive()\n"
    )
    (tmp_path / "conftest.py").write_text(
        "from tests.conftest import *  # noqa: F401,F403\n"
        "from tests.conftest import reset_db_module_state  # noqa: F401\n"
    )
    (tmp_path / "pytest.ini").write_text(
        "[pytest]\nasyncio_mode = auto\n"
        "asyncio_default_fixture_loop_scope = function\n"
    )
    code, output = await _run_child(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
         str(leaky)],
        cwd=tmp_path)

    assert code is not None, f"the child pytest run hung:\n{output}"
    assert code != 0, (
        "`expect_db_leftovers` excused an aiosqlite worker the product had "
        f"forgotten about:\n{output}")
    assert "aiosqlite worker thread(s) were still running" in output, output
    assert "no app.database registry accounts for" in output, output


async def test_startup_recovers_in_a_real_new_process_after_an_operator_cleanup(
    legacy_orphan_db, tmp_path
):
    # Resetting this module's globals is not a restart. The recovery claim is
    # about a NEW process opening the same file, so this runs one.
    with pytest.raises(db.SchemaIntegrityViolation):
        await db.init_db()
    await db.close_db()

    # The approved migration an operator runs — deliberately OUTSIDE the
    # product, which never touches these rows itself.
    raw = sqlite3.connect(str(legacy_orphan_db))
    try:
        raw.execute("PRAGMA foreign_keys=ON")
        raw.execute("DELETE FROM user_host_accounts WHERE user_email = ?", (_GHOST,))
        raw.commit()
        assert raw.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        raw.close()

    script = tmp_path / "fresh_process_startup.py"
    script.write_text(
        "import asyncio\n"
        "import app.database as db\n"
        "\n"
        f"db._db_path = {str(legacy_orphan_db)!r}\n"
        "\n"
        "\n"
        "async def main():\n"
        "    await db.init_db()\n"
        "    fk = await db._fetch_all_unrestricted('PRAGMA foreign_key_check')\n"
        f"    inherited = await db.get_user_host_accounts({_GHOST!r})\n"
        "    verdict = await db.close_db()\n"
        "    print('FK_VIOLATIONS=%d' % len(fk))\n"
        "    print('INHERITED=%r' % (dict(inherited),))\n"
        "    print('FAIL_STOP=%r' % (db.db_fail_stop_reason(),))\n"
        "    print('VERDICT=%s' % verdict.value)\n"
        "\n"
        "\n"
        "asyncio.run(main())\n"
    )
    code, output = await _run_child([sys.executable, str(script)], cwd=tmp_path)

    assert code is not None, f"the fresh-process startup hung:\n{output}"
    assert code == 0, f"a new process could not start on the cleaned database:\n{output}"
    assert "FK_VIOLATIONS=0" in output, output
    assert "INHERITED={}" in output, output
    assert "FAIL_STOP=None" in output, output
    assert "VERDICT=closed" in output, output


# ═════════════════════════════════════════════════════
# Hardening round 8 — defects found by the read-only review of round 7
# ═════════════════════════════════════════════════════


async def test_an_abandoned_join_does_not_delay_the_next_one(fresh_db, tmp_path):
    # A join whose caller went away keeps running. If every join shares one
    # thread, the next one cannot even look at its target until the abandoned
    # work finishes — so it waits out its WHOLE budget and only answers at the
    # deadline, even though the thread it is waiting for is already gone. With
    # the operation lock held that is pure shutdown latency, and when the budget
    # runs out first it turns a healthy database into RESTART_REQUIRED.
    wedge = await aiosqlite.connect(str(tmp_path / "wedge.db"))
    victim = await aiosqlite.connect(str(tmp_path / "victim.db"))
    loop = asyncio.get_running_loop()

    # Proof of entry, not a guessed delay: count what the join actually does to
    # its target. Watching costs one liveness read per turn (so a second read
    # means the wait loop is running); blocking costs a Thread.join. Either is
    # evidence that the abandoned join is really under way before it is
    # cancelled — a fixed sleep is evidence of nothing.
    entered = threading.Event()
    observed = {"alive": 0, "join": 0}
    real_is_alive, real_join = wedge.is_alive, wedge.join

    def counting_is_alive():
        observed["alive"] += 1
        if observed["alive"] >= 2:
            entered.set()
        return real_is_alive()

    def counting_join(timeout=None):
        observed["join"] += 1
        entered.set()
        return real_join(timeout)

    wedge.is_alive = counting_is_alive
    wedge.join = counting_join
    try:
        abandoned = asyncio.create_task(db._join_worker(wedge, 5.0))
        deadline = loop.time() + 10
        while not entered.is_set() and loop.time() < deadline:
            await asyncio.sleep(0.001)
        assert entered.is_set(), (
            f"the abandoned join never entered its wait: {observed}")
        abandoned.cancel()
        await asyncio.gather(abandoned, return_exceptions=True)
        del wedge.is_alive
        del wedge.join
        assert observed["alive"] >= 2 or observed["join"] >= 1, observed
        assert wedge.is_alive(), "the wedge connection's worker already exited"

        # The victim is STILL ALIVE when its join starts, so the fast path
        # cannot answer; the join has to actually watch the thread. Its entry
        # is observed too — the pre-loop liveness read happens in every design,
        # so it is a design-agnostic "past the fast path, committed to a live
        # thread" gate — rather than assumed after a sleep.
        assert victim.is_alive()
        victim_entered = threading.Event()
        victim_seen = {"alive": 0, "join": 0}
        v_is_alive, v_join = victim.is_alive, victim.join

        def victim_is_alive():
            victim_seen["alive"] += 1
            victim_entered.set()
            return v_is_alive()

        def victim_join(timeout=None):
            victim_seen["join"] += 1
            victim_entered.set()
            return v_join(timeout)

        victim.is_alive = victim_is_alive
        victim.join = victim_join
        joining = asyncio.create_task(db._join_worker(victim, 3.0))
        deadline = loop.time() + 10
        while not victim_entered.is_set() and loop.time() < deadline:
            await asyncio.sleep(0.001)
        assert victim_entered.is_set(), (
            f"the victim's join never started: {victim_seen}")
        del victim.is_alive
        del victim.join

        await asyncio.wait_for(victim.close(), timeout=5)   # the thread exits
        exited_at = None
        deadline = loop.time() + 10
        while exited_at is None and loop.time() < deadline:
            if not victim.is_alive():
                exited_at = loop.time()
            else:
                await asyncio.sleep(0.001)
        assert exited_at is not None, "the victim's worker never exited"
        gone = await asyncio.wait_for(joining, timeout=20)
        # Measured from the moment the thread ACTUALLY died, so neither the
        # cost of close() nor any sleep of the test's own is in the number.
        latency = loop.time() - exited_at

        assert gone is True, "a worker that had exited was reported alive"
        assert latency < 0.5, (
            f"_join_worker took {latency:.2f}s to notice a thread that had "
            "already exited — it was queued behind an abandoned join instead "
            "of watching its own target")
    finally:
        for conn in (wedge, victim):
            for attr in ("is_alive", "join"):
                if attr in conn.__dict__:
                    delattr(conn, attr)
        for conn in (wedge, victim):
            try:
                await asyncio.wait_for(conn.close(), timeout=10)
            except BaseException:
                pass
            await _worker_gone(conn, 10)


def _reject_and_refuse_to_close(hits, close_error="the discard close failed too",
                                gate=None):
    """Break a connection's verification AND its discard close."""
    def mutate(conn):
        real_execute = conn.execute

        async def execute(sql, *a, **k):
            cursor = await real_execute(sql, *a, **k)
            if isinstance(sql, str) and "foreign_keys" in sql and "=" not in sql:
                hits["configure"] += 1

                async def fetchall():
                    return [(0,)]
                cursor.fetchall = fetchall
            return cursor

        async def close():
            hits["close"] += 1
            if gate is not None:
                gate["discarding"].set()
                await gate["proceed"].wait()
            raise RuntimeError(close_error)

        conn.execute = execute
        conn.close = close

    return mutate


@pytest.mark.expect_db_leftovers
async def test_the_public_boundary_keeps_the_diagnostic_chain(fresh_db, monkeypatch):
    # `raise exc from None` is not a no-op: it clears __cause__ and suppresses
    # __context__ on the very object being raised. The connection boundary uses
    # it whenever nothing needed translating, which throws away the chain the
    # failure was deliberately built with — an operator then sees the cleanup
    # failure with nothing to say what caused it.
    await db.close_db()
    db._closed = False
    monkeypatch.setattr(db, "_conn", None)
    hits = {"configure": 0, "close": 0}
    made = _connect_factory(monkeypatch, _reject_and_refuse_to_close(hits))
    try:
        with pytest.raises(db.DatabaseFailStop) as excinfo:
            await asyncio.wait_for(db.blacklist_token("t", 1.0), timeout=20)
        assert hits["configure"] >= 1 and hits["close"] >= 1, hits
        exc = excinfo.value
        assert exc.__cause__ is not None, (
            "the public boundary cleared the cause chain the failure was built "
            f"with: {exc!r}")
        assert "refusing to publish this connection" in str(exc.__cause__), (
            "the cause no longer names the failure that started this: "
            f"{exc.__cause__!r}")
    finally:
        await _force_close(made)


@pytest.mark.expect_db_leftovers
async def test_one_discard_failure_is_recorded_as_one_fail_stop(
    fresh_db, monkeypatch, caplog
):
    # The discard latches the reason, and the caller then latches the identical
    # string again. The second one is logged as "additional reason", whose whole
    # point is a DIFFERENT later fact — so one failure reads as two.
    await db.close_db()
    db._closed = False
    monkeypatch.setattr(db, "_conn", None)
    hits = {"configure": 0, "close": 0}
    made = _connect_factory(monkeypatch, _reject_and_refuse_to_close(hits))
    caplog.set_level(logging.ERROR, logger="glassops.db")
    try:
        with pytest.raises(db.DatabaseFailStop):
            await asyncio.wait_for(db.blacklist_token("t", 1.0), timeout=20)
        assert hits["close"] >= 1, hits
        latched = [r.getMessage() for r in caplog.records
                   if "fail-stop engaged" in r.getMessage()
                   or "additional reason" in r.getMessage()]
        assert len(latched) == 1, (
            f"one discard failure was recorded as {len(latched)} fail-stop "
            f"events: {latched}")
    finally:
        await _force_close(made)


@pytest.mark.expect_db_leftovers
async def test_the_discard_reason_names_the_real_close_failure(
    fresh_db, monkeypatch
):
    # When the caller is cancelled while the discard close is in flight AND the
    # close fails, _submit reports both as one exception. Formatting that
    # wrapper with !r prints an EMPTY class name, so the only recorded reason
    # for a restart-required database names nothing at all.
    await db.close_db()
    db._closed = False
    monkeypatch.setattr(db, "_conn", None)
    hits = {"configure": 0, "close": 0}
    gate = {"discarding": asyncio.Event(), "proceed": asyncio.Event()}
    made = _connect_factory(
        monkeypatch, _reject_and_refuse_to_close(hits, gate=gate))
    task = asyncio.create_task(db.blacklist_token("t", 1.0))
    try:
        await asyncio.wait_for(gate["discarding"].wait(), timeout=20)
        task.cancel()
        gate["proceed"].set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=25)
        assert hits["close"] >= 1, hits
        assert db._fail_stop is not None
        assert "the discard close failed too" in db._fail_stop, (
            "the recorded restart-required reason does not name the failure "
            f"that caused it: {db._fail_stop}")
    finally:
        gate["proceed"].set()
        await _force_close(made)


@pytest.mark.expect_db_leftovers
async def test_a_connection_is_registered_unclosed_at_most_once(fresh_db):
    # readiness() serves len(_unclosed) as "unclosed_connections". Appending the
    # same object from two different failure paths reports two connections in
    # trouble when there is one.
    conn = await db._get_conn()
    real_commit = conn.commit
    detached = {}

    async def commit_then_detach():
        result = await real_commit()
        # The wrapper loses its connection: in_transaction now raises, so the
        # transaction state is unreadable.
        detached["raw"] = conn._connection
        conn._connection = None
        return result

    conn.commit = commit_then_detach
    try:
        with pytest.raises(db.DatabaseFailStop):
            await asyncio.wait_for(
                db.store_net_audit("a1", 1.0, [_event()], []), timeout=20)
        assert sum(1 for c in db._unclosed if c is conn) == 1, db._unclosed
        verdict = await asyncio.wait_for(db.close_db(timeout=3), timeout=30)
        assert verdict is db.CloseVerdict.RESTART_REQUIRED, verdict
        copies = sum(1 for c in db._unclosed if c is conn)
        assert copies == 1, (
            f"one connection was registered {copies} times in _unclosed, so "
            f"readiness reports {len(db._unclosed)} connections in trouble")
    finally:
        if "commit" in conn.__dict__:
            del conn.commit
        if detached.get("raw") is not None:
            conn._connection = detached["raw"]
        try:
            await asyncio.wait_for(conn.close(), timeout=10)
        except BaseException:
            pass
        assert await _worker_gone(conn, 10), "the detached worker never exited"


async def test_unrestricted_read_materialisation_error_keeps_the_cancellation(
    fresh_db
):
    # The third of the three fetch sites B3 reordered. _run_statement backs
    # _fetch_all_unrestricted AND _configure, so a cancellation lost here is
    # reported as "this connection failed verification" to a caller that was
    # actually cancelled.
    conn = await db._get_conn()
    probe = WorkerProbe("b3unres")
    await probe.install(conn)
    hit = []
    holder = {}
    submitted = asyncio.Event()
    real_execute = None

    def _noop():
        return None

    async def occupying_fetchall():
        holder["task"] = asyncio.create_task(
            real_execute(f"SELECT {probe.name}(1)"))
        await probe.wait_entered()
        submitted.set()
        await conn._execute(_noop)          # queues behind the blocked probe
        return _RowsThatCannotBeMaterialised(hit)

    real_execute = _breaking_execute(conn, break_fetch=occupying_fetchall,
                                     match="user_version")
    task = asyncio.create_task(db._fetch_all_unrestricted("PRAGMA user_version"))
    try:
        await asyncio.wait_for(submitted.wait(), timeout=20)
        await _wait_until_queued(conn, "the fetch's follow-up statement")
        task.cancel()
        probe.let_go()
        with pytest.raises(asyncio.CancelledError) as excinfo:
            await asyncio.wait_for(task, timeout=25)
    finally:
        probe.let_go()
        del conn.execute
        if holder.get("task"):
            cursor = (await asyncio.gather(holder["task"],
                                           return_exceptions=True))[0]
            if not isinstance(cursor, BaseException):
                try:
                    await asyncio.wait_for(cursor.close(), timeout=5)
                except BaseException:
                    pass

    assert probe.calls >= 1, "the worker was never occupied"
    assert hit == ["materialise"], f"the materialisation fault never ran: {hit}"
    assert type(excinfo.value) is asyncio.CancelledError, (
        f"an internal exception type reached a public caller: {excinfo.value!r}")


# ═════════════════════════════════════════════════════
# Hardening round 8 closure — the four items left open
# ═════════════════════════════════════════════════════


# ── A. a public cancellation names the real worker error ──


async def test_public_cancellation_names_the_real_worker_error(fresh_db, caplog):
    # _as_public already picks the right cause — the error the WORKER raised.
    # Re-raising with `from exc` then overwrites it with this module's private
    # wrapper, so the only thing a caller can follow is a class it is not
    # supposed to know exists, and the real error is nowhere in the chain.
    worker_error = RuntimeError("worker failed")
    internal = db._CancelledAfterWorkerError(worker_error)
    caplog.set_level(logging.WARNING, logger="glassops.db")

    with pytest.raises(BaseException) as excinfo:
        db._raise_public(internal)
    exc = excinfo.value

    assert type(exc) is asyncio.CancelledError, (
        f"a private exception type reached a public caller: {exc!r}")
    assert exc.__cause__ is worker_error, (
        "the public cancellation's direct cause is not the error the worker "
        f"actually raised: {exc.__cause__!r}")
    assert not isinstance(exc.__cause__, db._CancelledAfterWorkerError), (
        f"the private wrapper became the public direct cause: {exc.__cause__!r}")
    translations = [r.getMessage() for r in caplog.records
                    if "database operation cancelled" in r.getMessage()]
    assert len(translations) == 1, (
        f"one cancellation was translated/logged {len(translations)} times: "
        f"{translations}")


async def test_write_boundary_cancellation_keeps_the_real_cause(fresh_db):
    # The same guarantee through the real write boundary: the caller is
    # cancelled while the worker owns its statement, and the worker then fails.
    conn = await db._get_conn()
    probe = FailingWorkerProbe("apub")
    await probe.install(conn)

    async def writer():
        async with db.write_transaction() as tx:
            await tx.fetch_all(f"SELECT {probe.name}(1)")

    task = asyncio.create_task(writer())
    try:
        await probe.wait_entered()
        task.cancel()
        probe.let_go()
        with pytest.raises(asyncio.CancelledError) as excinfo:
            await asyncio.wait_for(task, timeout=25)
    finally:
        probe.let_go()

    exc = excinfo.value
    assert probe.calls >= 1, "the worker never entered the statement"
    assert type(exc) is asyncio.CancelledError, (
        f"a private exception type reached a public caller: {exc!r}")
    assert exc.__cause__ is not None, "the cause chain was dropped entirely"
    assert not isinstance(
        exc.__cause__,
        (db._CancelledAfterWorkerError, db._OutcomeUnknown, db._ChildCancelled)), (
        "a private classification became the public direct cause: "
        f"{exc.__cause__!r}")


# ── B. one incident, one fail-stop event ──


def _reject_then_hang_on_close(hits):
    """Break verification, and make the discard close never finish."""
    def mutate(conn):
        real_execute = conn.execute

        async def execute(sql, *a, **k):
            cursor = await real_execute(sql, *a, **k)
            if isinstance(sql, str) and "foreign_keys" in sql and "=" not in sql:
                hits["configure"] += 1

                async def fetchall():
                    return [(0,)]
                cursor.fetchall = fetchall
            return cursor

        async def close():
            hits["close"] += 1
            await asyncio.Event().wait()

        conn.execute = execute
        conn.close = close

    return mutate


@pytest.mark.expect_db_leftovers
async def test_an_unresolved_discard_close_is_one_fail_stop_event(
    fresh_db, monkeypatch, caplog
):
    # The discard's close never resolves, so _submit registers the submission
    # and latches restart-required. The caller then turns the SAME incident
    # into a problem string and latches it again as an "additional reason" —
    # a phrase whose whole purpose is a different, later fact. An operator, or
    # anything counting distinct fail-stop events, reads one failure as two.
    await db.close_db()
    db._closed = False
    monkeypatch.setattr(db, "_conn", None)
    monkeypatch.setattr(db, "_CLEANUP_TIMEOUT", 0.25)
    monkeypatch.setattr(db, "_CLEANUP_CANCEL_BUDGET", 4)
    hits = {"configure": 0, "close": 0}
    made = _connect_factory(monkeypatch, _reject_then_hang_on_close(hits))
    caplog.set_level(logging.ERROR, logger="glassops.db")
    try:
        with pytest.raises(db.DatabaseFailStop):
            await asyncio.wait_for(db.blacklist_token("t", 1.0), timeout=30)
        assert hits["configure"] >= 1, "the verification fault was never reached"
        assert hits["close"] >= 1, "the discard-close fault was never reached"
        assert len(db._unresolved) == 1, db._unresolved
        assert len(db._unclosed) == 1, db._unclosed

        def fail_stop_events():
            return [r.getMessage() for r in caplog.records
                    if "fail-stop engaged" in r.getMessage()
                    or "additional reason" in r.getMessage()]

        assert len(fail_stop_events()) == 1, (
            f"one discard timeout produced {len(fail_stop_events())} fail-stop "
            f"events: {fail_stop_events()}")

        # Control: a genuinely different later failure IS still recorded.
        db._enter_fail_stop("a later, unrelated failure")
        assert len(fail_stop_events()) == 2, (
            "a separate later failure was suppressed: "
            f"{fail_stop_events()}")
    finally:
        await _force_close(made)


# ── C. a published connection left running is not a clean teardown ──


_PUBLISHED_LEAK_BODY = (
    "import app.database as db\n"
    "\n"
    "\n"
    "{marker}async def test_shutdown_left_the_published_connection_running(tmp_path):\n"
    "    db._db_path = str(tmp_path / 'p.db')\n"
    "    await db.init_db()          # the PRODUCT publishes the connection\n"
    "    assert db._conn is not None and db._conn.is_alive()\n"
    "    # and nothing closes it: this is what a shutdown that left a\n"
    "    # connection behind looks like from the outside.\n"
)


async def test_a_published_live_connection_fails_teardown(tmp_path):
    # conftest observed _conn/_metric_conn but treated them as accounted for,
    # left them out of the leftover count, and then quietly closed them. Yet
    # fresh_db closes the database BEFORE the autouse teardown runs, so a live
    # published connection at that moment is not a fixture still in flight —
    # it is evidence that shutdown left one behind.
    (tmp_path / "conftest.py").write_text(
        "from tests.conftest import *  # noqa: F401,F403\n"
        "from tests.conftest import reset_db_module_state  # noqa: F401\n"
    )
    (tmp_path / "pytest.ini").write_text(
        "[pytest]\nasyncio_mode = auto\n"
        "asyncio_default_fixture_loop_scope = function\n"
    )
    for label, marker in (("unmarked", ""),
                          ("marked", "@__import__('pytest').mark.expect_db_leftovers\n")):
        leaky = tmp_path / f"test_published_{label}.py"
        leaky.write_text(_PUBLISHED_LEAK_BODY.format(marker=marker))
        code, output = await _run_child(
            [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
             str(leaky)],
            cwd=tmp_path)
        assert code is not None, f"[{label}] the child pytest run hung:\n{output}"
        assert code != 0, (
            f"[{label}] a connection the database published was still running "
            f"at teardown and the run was reported as passing:\n{output}")
        assert "the database had published" in output, (
            f"[{label}] the failure is not the published-connection gate:\n{output}")


# ── closure follow-ups from the read-only review ──


async def test_public_cancellation_of_an_unknown_outcome_stays_public(fresh_db):
    # A(ii): the cancellation a caller is owed can carry an UNRESOLVED outcome
    # rather than a worker error. _as_public copies whatever it is carrying
    # straight into the public __cause__, so the private classification becomes
    # the direct cause on every path where the worker never answered.
    outcome = db._Outcome()
    outcome.absorb(db._OutcomeUnknown("execute", asyncio.CancelledError()))
    failure = outcome.settle()
    with pytest.raises(BaseException) as excinfo:
        db._raise_public(failure)
    exc = excinfo.value

    assert type(exc) is asyncio.CancelledError, (
        f"a private exception type reached a public caller: {exc!r}")
    assert not isinstance(
        exc.__cause__,
        (db._OutcomeUnknown, db._ChildCancelled, db._CancelledAfterWorkerError)), (
        f"a private classification became the public direct cause: {exc.__cause__!r}")


async def test_unknown_outcome_cancellation_through_the_boundary_stays_public(
    fresh_db, monkeypatch
):
    # A(ii) end to end: the worker never answers AND the caller is cancelled.
    monkeypatch.setattr(db, "_CLEANUP_TIMEOUT", 0.3)
    monkeypatch.setattr(db, "_CLEANUP_CANCEL_BUDGET", 4)
    conn = await db._get_conn()
    gate, holder = await _occupy_worker_with_gate(conn, "apub2")

    async def writer():
        async with db.write_transaction() as tx:
            await tx.execute(
                "INSERT INTO net_conn_events (agent_id, ts, event, proto) "
                "VALUES ('a1', 1.0, 'open', 'tcp')")

    task = asyncio.create_task(writer())
    try:
        await _wait_until_queued(conn, "the write's first statement")
        task.cancel()
        with pytest.raises(asyncio.CancelledError) as excinfo:
            await asyncio.wait_for(task, timeout=25)
    finally:
        gate.let_go()
        await asyncio.gather(holder, return_exceptions=True)

    exc = excinfo.value
    assert gate.calls >= 1, "the worker was never occupied"
    assert type(exc) is asyncio.CancelledError, (
        f"a private exception type reached a public caller: {exc!r}")
    assert not isinstance(
        exc.__cause__,
        (db._OutcomeUnknown, db._ChildCancelled, db._CancelledAfterWorkerError)), (
        f"a private classification became the public direct cause: {exc.__cause__!r}")


async def test_a_failed_rollback_after_a_bad_commit_reaches_the_record(
    fresh_db, monkeypatch
):
    # B(ii): _force_close_transaction appends its problems straight into the
    # list, so they never enter the outcome's ledger of what still needs
    # recording. settle() then latches only the problems that DID, and the
    # operator record says a rollback was attempted without saying it failed.
    conn = await db._get_conn()
    monkeypatch.setattr(db, "_transaction_state", lambda c: True)
    with pytest.raises(db.DatabaseFailStop) as excinfo:
        await asyncio.wait_for(
            db.store_net_audit("a1", 1.0, [_event()], []), timeout=20)

    assert "transaction still open after rollback" in str(excinfo.value), (
        f"the caller was not told the rollback failed: {excinfo.value}")
    assert "transaction still open after rollback" in (db._fail_stop or ""), (
        "the failed rollback never reached the record an operator reads: "
        f"{db._fail_stop}")


@pytest.mark.expect_db_leftovers
async def test_every_fail_stop_event_is_a_distinct_incident(
    fresh_db, monkeypatch, caplog
):
    # B(iii): cursor cleanup on the commit and abort paths still re-reports an
    # incident _submit already recorded. Each unresolved submission is one
    # incident; the operator log must not contain more fail-stop events than
    # there were incidents.
    monkeypatch.setattr(db, "_CLEANUP_TIMEOUT", 0.05)
    monkeypatch.setattr(db, "_CLEANUP_CANCEL_BUDGET", 1)
    conn = await db._get_conn()
    real_execute = conn.execute
    hit = []

    async def hanging_cursor_close():
        hit.append(1)
        await asyncio.Event().wait()

    async def execute(sql, *a, **k):
        cursor = await real_execute(sql, *a, **k)
        cursor.close = hanging_cursor_close
        return cursor

    conn.execute = execute
    caplog.set_level(logging.ERROR, logger="glassops.db")
    try:
        with pytest.raises(db.DatabaseFailStop):
            await asyncio.wait_for(
                db.store_net_audit("a1", 1.0, [_event()], []), timeout=30)
    finally:
        del conn.execute

    events = [r.getMessage() for r in caplog.records
              if "fail-stop engaged" in r.getMessage()
              or "additional reason" in r.getMessage()]
    assert hit, "the cursor-close fault was never reached"
    incidents = len(db._unresolved)
    assert incidents >= 1, db._unresolved
    assert len(events) == incidents, (
        f"{incidents} unresolved submission(s) produced {len(events)} fail-stop "
        f"events: {events}")


_REAL_FIXTURE_LEAK = '''
import asyncio
import pytest
import app.database as db


@pytest.fixture
async def dbfix(tmp_path, monkeypatch):
    # Verbatim the shape every db fixture in this suite uses.
    monkeypatch.setattr(db, "_db_path", str(tmp_path / "t.db"))
    monkeypatch.setattr(db, "_conn", None)
    monkeypatch.setattr(db, "_metric_conn", None, raising=False)
    monkeypatch.setattr(db, "_op_lock", asyncio.Lock(), raising=False)
    monkeypatch.setattr(db, "_fail_stop", None, raising=False)
    await db.init_db()
    yield db
    # shutdown "fails": the connection stays published and running


{marker}async def test_shutdown_left_the_published_connection_running(dbfix):
    db._register_unclosed(db._conn)     # the product IS tracking it
    assert db._conn is not None and db._conn.is_alive()
'''


async def test_the_published_gate_survives_the_real_fixture_shape(tmp_path):
    # C(ii): every db fixture publishes through monkeypatch, and monkeypatch is
    # finalized before an autouse teardown that does not depend on it — so the
    # globals are already restored to None by the time they are read, and the
    # gate never fires for the tests it exists to protect.
    (tmp_path / "conftest.py").write_text(
        "from tests.conftest import *  # noqa: F401,F403\n"
        "from tests.conftest import reset_db_module_state  # noqa: F401\n"
    )
    (tmp_path / "pytest.ini").write_text(
        "[pytest]\nasyncio_mode = auto\n"
        "asyncio_default_fixture_loop_scope = function\n"
    )
    for label, marker in (("plain", ""),
                          ("declared", "@pytest.mark.expect_db_leftovers\n")):
        leaky = tmp_path / f"test_fixtureshape_{label}.py"
        leaky.write_text(_REAL_FIXTURE_LEAK.format(marker=marker))
        code, output = await _run_child(
            [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
             str(leaky)],
            cwd=tmp_path)
        assert code is not None, f"[{label}] the child pytest run hung:\n{output}"
        assert code != 0, (
            f"[{label}] a connection the database published was still running "
            f"at teardown and the run was reported as passing:\n{output}")
        assert "the database had published" in output, (
            f"[{label}] the failure is not the published-connection gate:\n{output}")


async def test_a_published_leak_does_not_hide_an_orphan_worker(tmp_path):
    # C(iii): both are observed before cleanup, but only one was reported. A
    # teardown that has both must say so — they are different failures with
    # different fixes.
    (tmp_path / "conftest.py").write_text(
        "from tests.conftest import *  # noqa: F401,F403\n"
        "from tests.conftest import reset_db_module_state  # noqa: F401\n"
    )
    (tmp_path / "pytest.ini").write_text(
        "[pytest]\nasyncio_mode = auto\n"
        "asyncio_default_fixture_loop_scope = function\n"
    )
    both = tmp_path / "test_bothleaks.py"
    both.write_text(
        "import aiosqlite\n"
        "import app.database as db\n"
        "\n"
        "\n"
        "async def test_leaves_a_published_connection_and_an_orphan(tmp_path):\n"
        "    db._db_path = str(tmp_path / 'p.db')\n"
        "    await db.init_db()                      # published, left running\n"
        "    orphan = await aiosqlite.connect(str(tmp_path / 'o.db'))\n"
        "    assert orphan.is_alive()                # and forgotten entirely\n"
    )
    code, output = await _run_child(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", str(both)],
        cwd=tmp_path)
    assert code is not None, f"the child pytest run hung:\n{output}"
    assert code != 0, output
    assert "the database had published" in output, (
        f"the published leak was not reported:\n{output}")
    assert "no app.database registry accounts for" in output, (
        f"the orphan worker was swallowed by the published report:\n{output}")


async def test_as_public_never_exposes_a_private_type_or_cause(fresh_db):
    # Every shape the translation can be handed, including the ones the
    # boundary reaches only indirectly. None of them may put this module's
    # private vocabulary in front of a caller, as the value OR as its cause.
    private = (db._OutcomeUnknown, db._ChildCancelled, db._CancelledAfterWorkerError)
    for internal in (
        db._OutcomeUnknown("execute", asyncio.CancelledError()),
        db._ChildCancelled("execute", asyncio.CancelledError()),
        db._OutcomeUnknown("execute"),
        db._ChildCancelled("execute"),
        db._CancelledAfterWorkerError(db._OutcomeUnknown("execute")),
        db._CancelledAfterWorkerError(RuntimeError("worker failed")),
    ):
        public = db._as_public(internal)
        assert not isinstance(public, private), (
            f"{internal!r} was handed back unchanged: {public!r}")
        assert not isinstance(public.__cause__, private), (
            f"{internal!r} -> public cause {public.__cause__!r}")


async def test_an_unresolved_commit_is_one_fail_stop_event(
    fresh_db, monkeypatch, caplog
):
    # The commit stage's own unresolved branch: _submit latched when it
    # registered the submission, so describing it must not record it again.
    monkeypatch.setattr(db, "_CLEANUP_TIMEOUT", 0.2)
    monkeypatch.setattr(db, "_CLEANUP_CANCEL_BUDGET", 1)
    conn = await db._get_conn()
    state, release = await _commit_blocked_in_worker(conn)
    caplog.set_level(logging.ERROR, logger="glassops.db")
    try:
        with pytest.raises(db.DatabaseFailStop):
            await asyncio.wait_for(db.blacklist_token("t", 1.0), timeout=30)
        assert state.get("submitted"), "COMMIT never reached the worker queue"
        events = [r.getMessage() for r in caplog.records
                  if "fail-stop engaged" in r.getMessage()
                  or "additional reason" in r.getMessage()]
        assert len(events) == 1, (
            f"one unresolved COMMIT produced {len(events)} fail-stop events: "
            f"{events}")
    finally:
        await release()


# ═════════════════════════════════════════════════════
# Closure round — the private vocabulary never reaches a caller,
# one incident is one fail-stop event, and the teardown gate
# tells a tracked owner from an orphan
# ═════════════════════════════════════════════════════

_PRIVATE = (db._OutcomeUnknown, db._ChildCancelled, db._CancelledAfterWorkerError)

# The NAMES, not just the types: a formatted traceback prints the class name of
# every exception in the chain, so a private classification reachable anywhere
# in the graph publishes this module's vocabulary even when no caller ever
# isinstance()-checks it.
_PRIVATE_NAMES = ("_OutcomeUnknown", "_ChildCancelled",
                  "_CancelledAfterWorkerError")


def _fail_stop_events(caplog):
    """Every log line that RECORDS a fail-stop, first or subsequent.

    Detail lines about an incident already recorded are deliberately not
    counted: they are the same failure still being described."""
    return [r.getMessage() for r in caplog.records
            if "fail-stop engaged" in r.getMessage()
            or "additional reason" in r.getMessage()]


def _assert_public(label, exc, *, expect_type, expect_cause):
    """Exact assertions, not `not isinstance(...)`: a translation that returns
    the wrong PUBLIC type still passes a negative check."""
    assert type(exc) is expect_type, (
        f"[{label}] public type is {type(exc).__name__}, expected "
        f"{expect_type.__name__}: {exc!r}")
    assert not isinstance(exc, _PRIVATE), (
        f"[{label}] a private classification reached a caller: {exc!r}")
    cause = exc.__cause__
    if expect_cause is None:
        assert cause is None, (
            f"[{label}] expected no direct cause, got {cause!r}")
    else:
        assert type(cause) is expect_cause, (
            f"[{label}] direct cause is {type(cause).__name__}, expected "
            f"{expect_cause.__name__}: {cause!r}")
    assert not isinstance(cause, _PRIVATE), (
        f"[{label}] a private classification became the direct cause: {cause!r}")


async def test_raise_public_never_leaves_a_private_type_as_the_direct_cause():
    # A. The two shapes with NO cancellation. _as_public deliberately hands
    # back a DatabaseFailStop with no cause — and _public_form used to fill
    # that emptiness with the very object the translation exists to hide, so
    # `except DatabaseFailStop as e: e.__cause__` read _OutcomeUnknown.
    worker_error = RuntimeError("the worker failed")
    cases = [
        ("_OutcomeUnknown without cancellation",
         db._OutcomeUnknown("probe"), db.DatabaseFailStop, None),
        ("_ChildCancelled without cancellation",
         db._ChildCancelled("probe"), db.DatabaseFailStop, None),
        # Control: a cancellation the caller is owed, plus a real worker error.
        # Here the direct cause MUST be the worker's own exception.
        ("cancellation + worker failure",
         db._CancelledAfterWorkerError(worker_error),
         asyncio.CancelledError, RuntimeError),
        # Control: cancelled AND unresolved — a bare CancelledError says
        # nothing, so it carries a PUBLIC reason instead.
        ("_OutcomeUnknown under cancellation",
         db._OutcomeUnknown("probe", asyncio.CancelledError()),
         asyncio.CancelledError, db.DatabaseFailStop),
        ("_ChildCancelled under cancellation",
         db._ChildCancelled("probe", asyncio.CancelledError()),
         asyncio.CancelledError, db.DatabaseFailStop),
        # Control: what the cancellation carries is NOT always a worker error.
        # When the worker never answered, the public cause has to say that —
        # copying the classification across would publish the private type,
        # and dropping it would leave a bare cancellation with no reason.
        ("cancellation + unresolved worker outcome",
         db._CancelledAfterWorkerError(db._OutcomeUnknown("probe")),
         asyncio.CancelledError, db.DatabaseFailStop),
    ]
    for label, internal, want_type, want_cause in cases:
        with pytest.raises(BaseException) as excinfo:
            db._raise_public(internal)
        _assert_public(label, excinfo.value,
                       expect_type=want_type, expect_cause=want_cause)
    # The control's cause is the worker's exception OBJECT, not a stand-in.
    with pytest.raises(asyncio.CancelledError) as excinfo:
        db._raise_public(db._CancelledAfterWorkerError(worker_error))
    assert excinfo.value.__cause__ is worker_error, (
        "the worker error was replaced instead of carried: "
        f"{excinfo.value.__cause__!r}")


async def test_a_fail_stop_that_arrives_with_a_private_cause_is_sanitised():
    # A(5). Several paths raise `DatabaseFailStop(...) from <private>` before
    # the boundary sees them — already public as a VALUE, private one link
    # down, which is where a caller looks. The central boundary must strip it.
    for label, private in (("_OutcomeUnknown", db._OutcomeUnknown("probe")),
                           ("_ChildCancelled", db._ChildCancelled("probe"))):
        arrived = db.DatabaseFailStop("database is restart-required: probe")
        arrived.__cause__ = private
        with pytest.raises(db.DatabaseFailStop) as excinfo:
            db._raise_public(arrived)
        _assert_public(f"pre-built fail-stop from {label}", excinfo.value,
                       expect_type=db.DatabaseFailStop, expect_cause=None)
        assert excinfo.value is arrived, (
            "an already-public exception was replaced rather than sanitised")

    # And the same shape carrying a WORKER error keeps that error as the cause.
    worker_error = RuntimeError("the worker failed")
    arrived = db.DatabaseFailStop("database is restart-required: probe")
    arrived.__cause__ = db._CancelledAfterWorkerError(worker_error)
    with pytest.raises(db.DatabaseFailStop) as excinfo:
        db._raise_public(arrived)
    assert excinfo.value.__cause__ is worker_error, (
        f"the worker error was not recovered: {excinfo.value.__cause__!r}")


async def test_translating_a_cancellation_logs_exactly_once(caplog):
    # A(6). The translation logs; translating twice reports one cancellation as
    # two. Every boundary re-raises through _raise_public, so an already-public
    # exception passing through a second time must translate nothing.
    caplog.set_level(logging.WARNING, logger="glassops.db")
    internal = db._CancelledAfterWorkerError(RuntimeError("the worker failed"))
    with pytest.raises(asyncio.CancelledError) as excinfo:
        db._raise_public(internal)
    with pytest.raises(asyncio.CancelledError):
        db._raise_public(excinfo.value)          # the outer boundary re-raises
    logged = [r.getMessage() for r in caplog.records
              if "database operation cancelled" in r.getMessage()]
    assert len(logged) == 1, (
        f"one cancellation was translated/logged {len(logged)} times: {logged}")


async def test_begin_whose_outcome_is_unknown_is_public_through_the_api(
    fresh_db, monkeypatch, caplog
):
    # A. Through the real public API, not the helper: the BEGIN reaches the
    # worker, the worker never answers, and what write_transaction() raises is
    # what an application sees.
    monkeypatch.setattr(db, "_CLEANUP_TIMEOUT", 0.2)
    monkeypatch.setattr(db, "_CLEANUP_CANCEL_BUDGET", 1)
    conn = await db._get_conn()
    probe, holder = await _occupy_worker(conn)
    caplog.set_level(logging.ERROR, logger="glassops.db")
    try:
        with pytest.raises(BaseException) as excinfo:
            await asyncio.wait_for(db.blacklist_token("t", 1.0), timeout=30)
        await _wait_until_queued(conn, "BEGIN IMMEDIATE")
        _assert_public("BEGIN unresolved", excinfo.value,
                       expect_type=db.DatabaseFailStop, expect_cause=None)
        events = _fail_stop_events(caplog)
        assert len(events) == 1, (
            f"one unresolved BEGIN produced {len(events)} fail-stop events: "
            f"{events}")
    finally:
        await _release_worker(conn, probe, holder)


async def test_commit_whose_outcome_is_unknown_is_public_through_the_api(
    fresh_db, monkeypatch
):
    # A. The COMMIT stage's own unresolved branch raises
    # `outcome.settle() from exc`, so the private classification was the direct
    # cause of the DatabaseFailStop a caller catches.
    monkeypatch.setattr(db, "_CLEANUP_TIMEOUT", 0.2)
    monkeypatch.setattr(db, "_CLEANUP_CANCEL_BUDGET", 1)
    conn = await db._get_conn()
    state, release = await _commit_blocked_in_worker(conn)
    try:
        with pytest.raises(BaseException) as excinfo:
            await asyncio.wait_for(db.blacklist_token("t", 1.0), timeout=30)
        assert state.get("submitted"), "COMMIT never reached the worker queue"
        _assert_public("COMMIT unresolved", excinfo.value,
                       expect_type=db.DatabaseFailStop, expect_cause=None)
        assert "_OutcomeUnknown" not in str(excinfo.value), (
            f"the private class name is in the caller's message: {excinfo.value}")
    finally:
        await release()


async def _release_worker(conn, probe, holder):
    """Let an occupied worker go and drain everything the boundary kept.

    Production leaves these open on purpose — restart-required means the
    process goes away — but a test has no process to end."""
    if probe is not None:
        probe.let_go()
    if holder is not None:
        await asyncio.gather(holder, return_exceptions=True)
    pending = [entry.task for entry in db._unresolved]
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
    for entry in list(db._late_results):
        try:
            await asyncio.wait_for(entry.obj.close(), timeout=5)
        except BaseException:
            pass
    try:
        await asyncio.wait_for(conn.close(), timeout=5)
    except BaseException:
        pass


# ── B. one incident is one fail-stop event ───────────


async def test_an_unresolved_rollback_is_one_fail_stop_event(
    fresh_db, monkeypatch, caplog
):
    # B(1). The rollback's outcome could not be established, so the transaction
    # is still open — BECAUSE of it. Recording "transaction still open after
    # rollback" as its own reason reported one incident as two, and an operator
    # counting fail-stop events saw a database that failed twice.
    monkeypatch.setattr(db, "_CLEANUP_TIMEOUT", 0.2)
    monkeypatch.setattr(db, "_CLEANUP_CANCEL_BUDGET", 1)
    conn = await db._get_conn()
    caplog.set_level(logging.ERROR, logger="glassops.db")
    probe = holder = None
    try:
        with pytest.raises(BaseException):
            async with db.write_transaction() as tx:
                await tx.execute(
                    "INSERT INTO token_blacklist VALUES (?, ?)", ("x", 1.0))
                probe, holder = await _occupy_worker(conn)
                raise RuntimeError("the body failed")
        events = _fail_stop_events(caplog)
        assert len(events) == 1, (
            f"one unresolved rollback produced {len(events)} fail-stop "
            f"events: {events}")
        # The follow-on observation is not DROPPED — it is the detail of the
        # incident that was recorded, and an operator has to be able to read it.
        detail = [r.getMessage() for r in caplog.records
                  if "transaction still open after rollback" in r.getMessage()]
        assert detail, (
            "the transaction being left open was not reported at all: "
            f"{[r.getMessage() for r in caplog.records]}")
    finally:
        await _release_worker(conn, probe, holder)


async def test_an_unresolved_late_cursor_close_is_one_fail_stop_event(
    fresh_db, monkeypatch, caplog
):
    # B(2). _submit latches when it registers the unresolved submission, then
    # the disposal loop latched the SAME close again as "could not be closed".
    monkeypatch.setattr(db, "_CLEANUP_TIMEOUT", 0.2)
    monkeypatch.setattr(db, "_CLEANUP_CANCEL_BUDGET", 1)
    conn = await db._get_conn()
    cursor = await conn.execute("SELECT 1")
    db._late_results.append(
        db._LateResult("read execute", db._Disposal.CURSOR, cursor, conn))
    probe, holder = await _occupy_worker(conn)
    caplog.set_level(logging.ERROR, logger="glassops.db")
    try:
        await db._dispose_late_results(0.2)
        events = _fail_stop_events(caplog)
        assert len(events) == 1, (
            f"one unresolved late-cursor close produced {len(events)} "
            f"fail-stop events: {events}")
        assert not any("_OutcomeUnknown" in e for e in events), (
            f"the private class name reached the operator record: {events}")
    finally:
        await _release_worker(conn, probe, holder)


@pytest.mark.expect_db_leftovers
async def test_an_unresolved_unpublished_discard_is_one_fail_stop_event(
    fresh_db, monkeypatch, caplog
):
    # B(3). The connection is registered as unclosed — that must not change —
    # but the incident behind it is the one _submit already recorded.
    monkeypatch.setattr(db, "_CLEANUP_TIMEOUT", 0.2)
    monkeypatch.setattr(db, "_CLEANUP_CANCEL_BUDGET", 1)
    conn = await db._get_conn()
    probe, holder = await _occupy_worker(conn)
    caplog.set_level(logging.ERROR, logger="glassops.db")
    try:
        outcome = await db._discard_connection(conn, "probe")
        assert outcome.problems, "the unresolved discard was waved through"
        outcome.settle()                       # what the caller path does next
        events = _fail_stop_events(caplog)
        assert len(events) == 1, (
            f"one unresolved discard produced {len(events)} fail-stop events: "
            f"{events}")
        assert any(c is conn for c in db._unclosed), (
            "the connection stopped being registered as unclosed")
    finally:
        await _release_worker(conn, probe, holder)


@pytest.mark.expect_db_leftovers
async def test_an_unresolved_close_db_connection_close_is_one_fail_stop_event(
    fresh_db, monkeypatch, caplog
):
    # B(4). close_db classified the close into `problem` and latched it, on top
    # of the latch _submit had already taken when it registered the submission.
    monkeypatch.setattr(db, "_CLEANUP_TIMEOUT", 0.2)
    monkeypatch.setattr(db, "_CLEANUP_CANCEL_BUDGET", 1)
    conn = await db._get_conn()
    probe, holder = await _occupy_worker(conn)
    caplog.set_level(logging.ERROR, logger="glassops.db")
    try:
        verdict = await db.close_db(timeout=1.0)
        assert verdict is db.CloseVerdict.RESTART_REQUIRED, verdict
        events = _fail_stop_events(caplog)
        assert len(events) == 1, (
            f"one unresolved connection close produced {len(events)} "
            f"fail-stop events: {events}")
        assert any(c is conn for c in db._unclosed), (
            "the connection stopped being registered as unclosed")
    finally:
        await _release_worker(conn, probe, holder)


@pytest.mark.expect_db_leftovers
async def test_a_genuinely_later_failure_is_still_an_additional_reason(
    fresh_db, monkeypatch, caplog
):
    # B, control. Collapsing an incident into one event must not silence a
    # SEPARATE failure that happened afterwards.
    monkeypatch.setattr(db, "_CLEANUP_TIMEOUT", 0.2)
    monkeypatch.setattr(db, "_CLEANUP_CANCEL_BUDGET", 1)
    conn = await db._get_conn()
    probe, holder = await _occupy_worker(conn)
    caplog.set_level(logging.ERROR, logger="glassops.db")
    try:
        await db.close_db(timeout=1.0)
        assert len(_fail_stop_events(caplog)) == 1
        # A different failure, later. It is not part of the incident above and
        # is the only record an operator will have of it.
        db._enter_fail_stop("a later, unrelated failure")
        events = _fail_stop_events(caplog)
        assert len(events) == 2, (
            f"a separate later failure was swallowed: {events}")
        assert "a later, unrelated failure" in events[-1], events
    finally:
        await _release_worker(conn, probe, holder)


async def test_two_identical_problems_keep_their_own_recorded_state():
    # B, provenance. Held as two parallel string lists, "is this one already
    # recorded?" was answered by asking whether the same TEXT appeared in the
    # other list — so of two identical messages, the unrecorded one inherited
    # the recorded one's answer and its fail-stop was never taken.
    message = "cursor close outcome unknown: execute"
    source = db._Outcome()
    source.unsafe(message, already_recorded=True)    # the incident on record
    source.unsafe(message)                           # a SECOND, separate one
    adopted = db._Outcome()
    adopted.take_problems(source)

    assert [p.message for p in adopted.problems] == [message, message], (
        f"a problem was lost in the copy: {adopted.problems}")
    assert [p.recorded for p in adopted.problems] == [True, False], (
        "the copy did not preserve which of two identical problems was "
        f"already recorded: {adopted.problems}")
    assert adopted.unrecorded == [message], (
        f"the still-unrecorded problem was inferred away: {adopted.unrecorded}")


# ── C. the connect wrapper is installed AND undone by monkeypatch ──

_DEPTH_PROBE = '''\
"""Records how deeply the suite's tracking wrapper is nested, per test."""
import json
import os

import aiosqlite

_depths = []


def _wrapper_depth():
    fn = aiosqlite.connect
    depth, seen = 0, set()
    while (callable(fn)
           and getattr(fn, "__qualname__", "").endswith("tracking_connect")
           and id(fn) not in seen):
        seen.add(id(fn))
        depth += 1
        following = None
        for cell in (getattr(fn, "__closure__", None) or ()):
            try:
                value = cell.cell_contents
            except ValueError:
                continue
            if callable(value):
                following = value
                break
        fn = following
    return depth


def pytest_runtest_call(item):
    _depths.append(_wrapper_depth())


def pytest_sessionfinish(session, exitstatus):
    with open(os.environ["GLASSOPS_DEPTH_OUT"], "a") as handle:
        handle.write(json.dumps(_depths) + "\\n")
'''

_UNDO_DRIVER = '''\
import json
import sys

import aiosqlite
import pytest

original = aiosqlite.connect
runs = []
for _ in range(2):
    rc = pytest.main(["-q", "-p", "no:cacheprovider", "-p", "depthprobe",
                      {target!r}])
    runs.append({{
        "rc": int(rc),
        "is_original": aiosqlite.connect is original,
        "qualname": getattr(aiosqlite.connect, "__qualname__", "?"),
    }})
print("GLASSOPS_RESULT " + json.dumps(runs))
'''


async def test_the_connect_wrapper_is_undone_by_monkeypatch_not_by_hand(tmp_path):
    # C. The autouse fixture assigned aiosqlite.connect directly and restored it
    # directly, while every test that patches connect uses the SAME monkeypatch
    # fixture — which is finalised AFTERWARDS and puts the wrapper back. The
    # module attribute never returned to the real function, and the next test
    # inherited a closure over a dead `created` list.
    target = ("tests/backend/test_db_write_transaction.py"
              "::test_connection_is_not_published_when_a_pragma_fails")
    (tmp_path / "depthprobe.py").write_text(_DEPTH_PROBE)
    driver = tmp_path / "undo_driver.py"
    driver.write_text(_UNDO_DRIVER.format(target=target))
    depth_out = tmp_path / "depths.jsonl"

    # pytest's own monkeypatch, not a hand-rolled backup: it restores a
    # variable that was SET to what it was, and DELETES one that was never
    # there — and it does that for both variables, which the hand-rolled backup
    # only ever did for one. The leaked PYTHONPATH prepended this test's
    # tmp_path to the interpreter path of every test that ran after it in the
    # same process, and grew by one more dead directory on each further run.
    with pytest.MonkeyPatch.context() as env:
        env.setenv("GLASSOPS_DEPTH_OUT", str(depth_out))
        env.setenv("PYTHONPATH", os.pathsep.join(
            [str(tmp_path), os.environ.get("PYTHONPATH", "")]))
        code, output = await _run_child(
            [sys.executable, str(driver)], cwd=_repo_root())

    assert code is not None, f"the child driver hung:\n{output}"
    result = [line for line in output.splitlines()
              if line.startswith("GLASSOPS_RESULT ")]
    assert result, f"the driver produced no result:\n{output}"
    runs = json.loads(result[-1][len("GLASSOPS_RESULT "):])
    assert code == 0, f"the driver failed:\n{output}"

    for index, run in enumerate(runs, start=1):
        assert run["rc"] == 0, f"[run {index}] the target test failed:\n{output}"
        assert run["is_original"], (
            f"[run {index}] aiosqlite.connect was left as "
            f"{run['qualname']!r} instead of the real function:\n{output}")
        assert run["qualname"] == "connect", (
            f"[run {index}] aiosqlite.connect is {run['qualname']!r}:\n{output}")

    depths = [d for line in depth_out.read_text().splitlines() if line.strip()
              for d in json.loads(line)]
    assert depths, f"the depth probe recorded nothing:\n{output}"
    assert set(depths) == {1}, (
        f"the tracking wrapper nested across runs: depths={depths}\n{output}")


# ── D. a late result's OWNER is not an orphan ────────

_LATE_OWNER_CHILD = '''\
import aiosqlite
import pytest

import app.database as db


@pytest.mark.expect_db_leftovers
async def test_late_cursor_and_its_owner(tmp_path):
    conn = await aiosqlite.connect(str(tmp_path / "owner.db"))
    cursor = await conn.execute("SELECT 1")
    # Exactly what adjudicate_unresolved() builds: the cursor is the object,
    # the connection that owns the worker thread is the owner.
    db._late_results.append(
        db._LateResult("read execute", db._Disposal.CURSOR, cursor, conn))
{extra}
'''

_LATE_OWNER_EXTRA = '''\
    stranger = await aiosqlite.connect(str(tmp_path / "stranger.db"))
    assert stranger.is_alive()          # recorded by nothing at all
'''


def _child_layout(tmp_path):
    (tmp_path / "conftest.py").write_text(
        "from tests.conftest import *  # noqa: F401,F403\n"
        "from tests.conftest import reset_db_module_state  # noqa: F401\n"
    )
    (tmp_path / "pytest.ini").write_text(
        "[pytest]\nasyncio_mode = auto\n"
        "asyncio_default_fixture_loop_scope = function\n"
    )


async def test_a_tracked_late_results_owner_is_not_reported_as_an_orphan(tmp_path):
    # D(1). A late CURSOR's `obj` is the cursor; the connection whose worker is
    # running is its `owner`. The gate only ever looked at `obj`, so a
    # correctly tracked late result made its own connection look forgotten.
    _child_layout(tmp_path)
    case = tmp_path / "test_lateowner.py"
    case.write_text(_LATE_OWNER_CHILD.format(extra=""))
    code, output = await _run_child(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", str(case)],
        cwd=tmp_path)
    assert code is not None, f"the child pytest run hung:\n{output}"
    assert "no app.database registry accounts for" not in output, (
        f"the late result's own owner was reported as an orphan:\n{output}")
    assert code == 0, f"the child run failed for another reason:\n{output}"


async def test_a_real_orphan_beside_a_tracked_late_result_still_fails(tmp_path):
    # D(2). The control the fix must not cost: accounting for the owner must
    # not account for every connection that happens to be alive.
    _child_layout(tmp_path)
    case = tmp_path / "test_lateowner_plus.py"
    case.write_text(_LATE_OWNER_CHILD.format(extra=_LATE_OWNER_EXTRA))
    code, output = await _run_child(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", str(case)],
        cwd=tmp_path)
    assert code is not None, f"the child pytest run hung:\n{output}"
    assert code != 0, f"a connection nothing accounts for was excused:\n{output}"
    assert "no app.database registry accounts for" in output, (
        f"the unregistered connection was not reported:\n{output}")
    assert "1 aiosqlite worker thread(s)" in output, (
        f"more than the unregistered connection was reported:\n{output}")


# ── E. a completed close is confirmed, never excused ──


async def test_a_properly_closed_probe_is_never_a_false_orphan(fresh_db, tmp_path):
    # E. aiosqlite 0.21.0: is_alive() is still True the instant close() returns
    # and False after one sleep(0) — so a probe the test closed CORRECTLY read
    # as a live worker no registry knew about, and the gate failed the test that
    # did the right thing. Repeated, because the window is a scheduling one.
    import tests.conftest as suite_conftest

    for round_number in range(20):
        probe = aiosqlite.connect(str(tmp_path / f"p{round_number}.db"))
        suite_conftest._instrument_close(probe)
        await probe
        await probe.close()
        reported = await suite_conftest.unaccounted_live_workers(db, [probe])
        assert reported == [], (
            f"[round {round_number}] a properly closed probe was reported as "
            f"an orphan: {reported}")
        probe.join(timeout=5)


async def test_a_probe_that_was_never_closed_is_always_an_orphan(fresh_db, tmp_path):
    # E, control. The confirmation is spent only on a close that COMPLETED;
    # anything else is an orphan on the first look, with no grace at all.
    import tests.conftest as suite_conftest

    for round_number in range(10):
        probe = aiosqlite.connect(str(tmp_path / f"o{round_number}.db"))
        suite_conftest._instrument_close(probe)
        await probe
        try:
            assert not suite_conftest._close_completed(probe), (
                f"[round {round_number}] an unclosed probe was treated as "
                "having completed its close")
            started = time.monotonic()
            reported = await suite_conftest.unaccounted_live_workers(db, [probe])
            elapsed = time.monotonic() - started
            assert [c is probe for c in reported] == [True], (
                f"[round {round_number}] an unclosed probe was not reported: "
                f"{reported}")
            # Immediately — a live worker that never completed a close gets no
            # part of the confirmation window. Spending it here is how a real
            # orphan would be hidden behind a sleep.
            assert elapsed < suite_conftest._SETTLE_BOUND / 2, (
                f"[round {round_number}] an unclosed probe was given "
                f"{elapsed:.3f}s of settling time")
        finally:
            await asyncio.wait_for(probe.close(), timeout=5)
            probe.join(timeout=5)


async def test_a_published_live_connection_is_never_granted_the_confirmation(
    fresh_db, tmp_path
):
    # E, control. The published-connection gate reads the thread directly, and
    # a connection the database is still serving from can never satisfy the
    # completed-close test that the confirmation is spent on.
    import tests.conftest as suite_conftest

    published = await db._get_conn()
    for round_number in range(10):
        assert suite_conftest.worker_alive(published), round_number
        assert not suite_conftest._close_completed(published), (
            f"[round {round_number}] a live published connection was treated "
            "as having completed its close")
        await asyncio.sleep(0)


async def test_raise_from_private_never_publishes_the_classification_it_names():
    # A(3), at the SOURCE. The central boundary sanitises what reaches a
    # caller, but the raise sites must not create the problem in the first
    # place: an exception that has not passed the boundary yet is still read by
    # this module's own handlers, and `raise failure from <private>` is how the
    # classification became the direct cause on the BEGIN and COMMIT paths.
    for label, private in (("_OutcomeUnknown", db._OutcomeUnknown("probe")),
                           ("_ChildCancelled", db._ChildCancelled("probe"))):
        failure = db.DatabaseFailStop("database is restart-required: probe")
        try:
            raise private
        except BaseException as exc:
            with pytest.raises(db.DatabaseFailStop) as excinfo:
                db._raise_from_private(failure, exc)
        assert excinfo.value.__cause__ is None, (
            f"[{label}] the classification was published as the direct cause: "
            f"{excinfo.value.__cause__!r}")

    # A worker error is a PUBLIC fact and stays the direct cause.
    worker_error = RuntimeError("the worker failed")
    failure = db.DatabaseFailStop("database is restart-required: probe")
    try:
        raise db._CancelledAfterWorkerError(worker_error)
    except BaseException as exc:
        with pytest.raises(db.DatabaseFailStop) as excinfo:
            db._raise_from_private(failure, exc)
    assert excinfo.value.__cause__ is worker_error, (
        f"the worker error was not carried through: {excinfo.value.__cause__!r}")

    # And an ordinary exception is neither replaced nor dropped.
    ordinary = ValueError("an ordinary failure")
    failure = db.DatabaseFailStop("database is restart-required: probe")
    try:
        raise ordinary
    except BaseException as exc:
        with pytest.raises(db.DatabaseFailStop) as excinfo:
            db._raise_from_private(failure, exc)
    assert excinfo.value.__cause__ is ordinary, (
        f"a public cause was disturbed: {excinfo.value.__cause__!r}")


async def test_a_cancelled_after_worker_error_repr_names_its_reason():
    # super().__init__() takes no args, so the DEFAULT repr is an empty class
    # name — and every `%r` of this exception (adjudicate_unresolved's settled
    # detail, the late-connection join warning) would then record a
    # restart-required database whose reason names nothing.
    worker_error = RuntimeError("the worker failed")
    internal = db._CancelledAfterWorkerError(worker_error)
    assert repr(worker_error) in repr(internal), (
        f"the repr names no reason at all: {internal!r}")
    assert repr(worker_error) in str(internal), (
        f"the str names no reason at all: {internal}")


# ═════════════════════════════════════════════════════
# Final closure round
#   A. a MIXED ending loses neither half
#   B. the private vocabulary is unreachable, not merely un-caused
# ═════════════════════════════════════════════════════


def _fail_stop_details(caplog):
    """Every log line that DESCRIBES an incident already on the record.

    The counterpart of _fail_stop_events: these are the follow-on observations
    an operator still has to be able to read, and they must never be counted
    as further failures."""
    return [r.getMessage() for r in caplog.records
            if "same incident" in r.getMessage()]


async def test_a_mixed_ending_reports_both_halves_to_the_caller(
    fresh_db, monkeypatch, caplog
):
    # A. The real four-step flow, in order:
    #   1. the statement's own cursor close fails,
    #   2. the boundary's cleanup RETRIES that cursor and it fails again,
    #   3. the aiosqlite worker is occupied, so the rollback cannot resolve,
    #   4. the transaction is still open afterwards BECAUSE of (3).
    #
    # Steps 2 and 3/4 land in ONE outcome with opposite recorded states, and
    # settle() branches on `unrecorded or ...` — so the recorded half is
    # dropped from the operator record, while _abort_transaction throws away
    # the exception settle() built and _reclassify hands back the global latch
    # text instead. The caller is told about neither.
    monkeypatch.setattr(db, "_CLEANUP_TIMEOUT", 0.2)
    monkeypatch.setattr(db, "_CLEANUP_CANCEL_BUDGET", 1)
    conn = await db._get_conn()
    _poison_cursor_close(conn, "INSERT INTO token_blacklist")
    caplog.set_level(logging.ERROR, logger="glassops.db")
    probe = holder = None
    try:
        with pytest.raises(db.DatabaseFailStop) as excinfo:
            async with db.write_transaction() as tx:
                # (1) and the cursor stays tracked for (2).
                with pytest.raises(db.DatabaseFailStop):
                    await tx.execute(
                        "INSERT INTO token_blacklist VALUES (?, ?)", ("x", 1.0))
                # (3): from here the worker cannot answer the rollback.
                probe, holder = await _occupy_worker(conn)
                raise RuntimeError("the body failed")

        message = str(excinfo.value)
        for expected in ("cursor close",
                         "rollback outcome unknown",
                         "transaction still open after rollback"):
            assert expected in message, (
                f"the caller was never told {expected!r}: {message}")
        for private in _PRIVATE_NAMES:
            assert private not in message, (
                f"{private} reached the caller's message: {message}")

        # The follow-on observation of an incident ALREADY recorded is what an
        # operator needs to read; it must survive as detail.
        detail = " | ".join(_fail_stop_details(caplog))
        assert "transaction still open after rollback" in detail, (
            "the recorded incident's follow-on vanished from the operator "
            f"record: {[r.getMessage() for r in caplog.records]}")

        # ...but it is the SAME incident as the unresolved rollback, so it must
        # not also be counted as a further failure.
        events = _fail_stop_events(caplog)
        assert not [e for e in events
                    if "transaction still open after rollback" in e], (
            f"a follow-on observation was recorded as its own event: {events}")
    finally:
        if "execute" in conn.__dict__:
            del conn.execute
        await _release_worker(conn, probe, holder)


async def test_two_separate_failures_that_read_alike_stay_two_events(
    monkeypatch, caplog
):
    # A, provenance. Identity, not text: a follow-on observation shares its
    # incident with the failure that caused it, while a genuinely separate
    # failure that happens to read the SAME way is still its own event.
    monkeypatch.setattr(db, "_fail_stop", "database is restart-required: seed")
    caplog.set_level(logging.ERROR, logger="glassops.db")
    outcome = db._Outcome()
    incident = outcome.unsafe("rollback outcome unknown: rollback",
                              already_recorded=True)
    assert incident is not None, (
        "unsafe() does not hand back an incident identity to attach a "
        "follow-on observation to")
    outcome.unsafe("transaction still open after rollback", incident=incident)
    outcome.unsafe("transaction still open after rollback")   # a SEPARATE one

    failure = outcome.settle()

    assert isinstance(failure, db.DatabaseFailStop), repr(failure)
    events = _fail_stop_events(caplog)
    assert len(events) == 1, (
        f"one separate new failure produced {len(events)} events: {events}")
    detail = " | ".join(_fail_stop_details(caplog))
    assert "rollback outcome unknown" in detail, (
        f"the recorded incident was dropped instead of described: {detail}")
    assert str(failure).count("transaction still open after rollback") == 2, (
        "two separate observations were merged because they read the same: "
        f"{failure}")


def _exception_graph(exc):
    """Every exception reachable from `exc`, following __cause__ AND
    __context__ to any depth.

    The direct cause alone is not enough. `raise public` inside an
    `except <private>` block leaves the private object as the implicit CONTEXT
    of the very exception being handed out, and a DatabaseFailStop built from
    another public exception can carry one two links down — both of which
    traceback.format_exception() prints by name."""
    seen, order, stack = set(), [], [exc]
    while stack:
        current = stack.pop()
        if current is None or id(current) in seen:
            continue
        seen.add(id(current))
        order.append(current)
        stack.append(current.__cause__)
        stack.append(current.__context__)
    return order


def _assert_no_private_vocabulary(label, exc):
    """Not one private classification anywhere a caller can reach OR print."""
    for node in _exception_graph(exc):
        assert type(node).__name__ not in _PRIVATE_NAMES, (
            f"[{label}] {type(node).__name__} is reachable from the exception "
            f"a caller receives: {node!r}")
        assert not isinstance(node, _PRIVATE), (
            f"[{label}] a private classification is reachable: {node!r}")
    formatted = "".join(
        traceback.format_exception(type(exc), exc, exc.__traceback__))
    for name in _PRIVATE_NAMES:
        assert name not in formatted, (
            f"[{label}] {name} appears in the formatted traceback:\n{formatted}")


async def _unresolved_path(path, monkeypatch):
    """One real public call taken to the point where its worker submission
    cannot reach a terminal outcome.

    Returns (call, submitted, cleanup): `submitted` proves the work really
    reached the aiosqlite worker, so a cancellation lands while _submit is
    waiting rather than before it ever got there."""
    if path in ("begin", "read"):
        conn = await db._get_conn()
        probe, holder = await _occupy_worker(conn)
        queued = "BEGIN IMMEDIATE" if path == "begin" else "read execute"
        call = ((lambda: db.blacklist_token("t", 1.0)) if path == "begin"
                else (lambda: db.get_recent_metrics("a1")))

        async def submitted():
            await _wait_until_queued(conn, queued)

        async def cleanup():
            await _release_worker(conn, probe, holder)

        return call, submitted, cleanup

    if path == "commit":
        conn = await db._get_conn()
        state, release = await _commit_blocked_in_worker(conn)

        async def submitted():
            loop = asyncio.get_running_loop()
            deadline = loop.time() + 15
            while not state.get("submitted") and loop.time() < deadline:
                await asyncio.sleep(0.005)
            assert state.get("submitted"), "COMMIT never reached the worker queue"

        return (lambda: db.blacklist_token("t", 1.0)), submitted, release

    assert path == "connect", path
    assert db._metric_conn is None, "the metric connection is already built"
    gate = ConnectGate(monkeypatch, db._db_path)

    async def submitted():
        await gate.wait_entered()

    async def cleanup():
        gate.let_go()
        late = await _collect_late([entry[1] for entry in db._unresolved])
        db.adjudicate_unresolved()
        await _force_close(late)

    return (lambda: db.store_metric("a1", 1.0, _metric())), submitted, cleanup


@pytest.mark.parametrize("cancel", [False, True], ids=["plain", "cancelled"])
@pytest.mark.parametrize("path", ["begin", "commit", "connect", "read"])
async def test_no_public_path_can_reach_the_private_vocabulary(
    fresh_db, monkeypatch, path, cancel
):
    # B. Eight real paths. _sanitise_cause only ever cleared the DIRECT cause,
    # and it MOVED the private object to __context__ — which is exactly where
    # a traceback prints it — so every one of these handed a caller an object
    # graph containing this module's internal vocabulary.
    monkeypatch.setattr(db, "_CLEANUP_TIMEOUT", 1.0 if cancel else 0.2)
    monkeypatch.setattr(db, "_CLEANUP_CANCEL_BUDGET", 4 if cancel else 1)
    call, submitted, cleanup = await _unresolved_path(path, monkeypatch)
    try:
        if cancel:
            task = asyncio.create_task(call())
            await submitted()
            task.cancel()
            with pytest.raises(BaseException) as excinfo:
                await asyncio.wait_for(task, timeout=30)
        else:
            with pytest.raises(BaseException) as excinfo:
                await asyncio.wait_for(call(), timeout=30)
            await submitted()
        _assert_no_private_vocabulary(
            f"{path} unresolved, cancel={cancel}", excinfo.value)
    finally:
        await cleanup()


async def test_a_public_worker_error_and_its_public_context_are_kept(fresh_db):
    # B, control. The scrub must remove the private vocabulary and NOTHING
    # else: a real worker error is the fact a caller needs, and a public
    # context that was already attached is diagnostic an operator reads.
    worker_error = RuntimeError("the worker failed")
    earlier = ValueError("an earlier public failure")
    internal = db._CancelledAfterWorkerError(worker_error)
    internal.__context__ = earlier

    with pytest.raises(asyncio.CancelledError) as excinfo:
        db._raise_public(internal)

    public = excinfo.value
    assert public.__cause__ is worker_error, (
        f"the worker error was replaced instead of carried: {public.__cause__!r}")
    assert any(node is earlier for node in _exception_graph(public)), (
        "an already-public context was discarded by the scrub: "
        f"{[repr(n) for n in _exception_graph(public)]}")
    _assert_no_private_vocabulary("public worker error control", public)


async def test_a_private_link_two_levels_down_is_scrubbed():
    # B, control. public -> public cause -> PRIVATE cause. Only the innermost
    # link is private, so a scrub that inspects one level finds nothing wrong
    # and hands the caller a graph it can still print the private name from.
    for label, private in (("_OutcomeUnknown", db._OutcomeUnknown("probe")),
                           ("_ChildCancelled", db._ChildCancelled("probe")),
                           ("_CancelledAfterWorkerError",
                            db._CancelledAfterWorkerError(RuntimeError("boom")))):
        middle = db.DatabaseFailStop("database is restart-required: middle")
        middle.__cause__ = private
        top = db.DatabaseFailStop("database is restart-required: top")
        top.__cause__ = middle

        with pytest.raises(db.DatabaseFailStop) as excinfo:
            db._raise_public(top)

        _assert_no_private_vocabulary(f"two levels down: {label}", excinfo.value)
        assert any(node is middle for node in _exception_graph(excinfo.value)), (
            "the public link in between was discarded rather than kept")


async def test_the_connect_wrapper_test_leaves_pythonpath_exactly_as_it_found_it(
    tmp_path
):
    # C. The test above prepends its own directory to PYTHONPATH so the child
    # driver can import the depth probe, and never puts it back — so every test
    # that runs after it in the SAME process inherits an interpreter path
    # pointing at a tmp_path that is about to be deleted, and each further run
    # prepends again. Run it twice and require exact equality both times:
    # "starts with what it had" is what a leaking prepend also satisfies.
    missing = object()
    for run in (1, 2):
        target = tmp_path / f"run-{run}"
        target.mkdir()
        before = os.environ.get("PYTHONPATH", missing)
        await test_the_connect_wrapper_is_undone_by_monkeypatch_not_by_hand(target)
        after = os.environ.get("PYTHONPATH", missing)
        assert after == before, (
            f"[run {run}] PYTHONPATH was left as {after!r}, not the {before!r} "
            "it was found with")


async def test_as_public_hands_back_the_same_object_for_a_public_exception():
    # The invariant _public_form's transplant rests on. _as_public replaces the
    # VALUE only for a private classification; for anything already public it
    # hands back the identical object. That is why the transplant does not need
    # to re-check privateness — and it is load-bearing: if _as_public ever
    # started replacing a public exception too, the transplant would overwrite
    # that exception's OWN context with somebody else's.
    private_cause = db.DatabaseFailStop("restart-required: probe")
    nested = ValueError("a public failure with a public cause")
    nested.__cause__ = private_cause
    for label, exc in (
        ("RuntimeError", RuntimeError("boom")),
        ("DatabaseFailStop", db.DatabaseFailStop("restart-required")),
        ("CancelledError", asyncio.CancelledError()),
        ("TransactionHandleMisuse", db.TransactionHandleMisuse("misused")),
        ("ReadOnlyViolation", db.ReadOnlyViolation("not a read")),
        ("public exception carrying a public cause", nested),
    ):
        assert db._as_public(exc) is exc, (
            f"[{label}] a public exception was REPLACED rather than handed "
            f"back: {db._as_public(exc)!r}")
    # And its own context is never taken from it.
    earlier = ValueError("an earlier public failure")
    carrier = RuntimeError("the failure a caller sees")
    carrier.__context__ = earlier
    assert db._public_form(carrier).__context__ is earlier, (
        "a public exception's own context was overwritten by the transplant")


# ═════════════════════════════════════════════════════
# Final limit round
#   A. a PUBLIC context must still PRINT after the scrub
#   B. one unreadable transaction state is one incident
# ═════════════════════════════════════════════════════


def _formatted(exc):
    """What an operator actually reads: the rendered traceback, not the graph.

    An assertion that the context object is still *reachable* passes even when
    the traceback no longer prints it — which is precisely the failure here."""
    return "".join(
        traceback.format_exception(type(exc), exc, exc.__traceback__))


def _observable(exc):
    """Everything about an exception graph a caller or an operator can see.

    Identity of each link, the display flag, and the rendered text — because a
    scrub that leaves the objects alone but flips __suppress_context__ has
    still changed what the process reports."""
    return ([(type(n).__name__, str(n), n.__suppress_context__,
              id(n.__cause__), id(n.__context__)) for n in _exception_graph(exc)],
            _formatted(exc))


async def test_a_public_context_still_prints_after_the_scrub():
    # A. `exc.__cause__ = <anything>` sets __suppress_context__ in CPython —
    # INCLUDING `= None`. _public_graph re-assigns the cause of every public
    # link it walks, so a perfectly public LookupError that the traceback used
    # to print under "During handling of the above exception" silently stopped
    # being printed. The object is still reachable, which is why a graph-only
    # assertion cannot see this at all.
    inner = LookupError("public-inner")
    outer = RuntimeError("public-outer")
    outer.__context__ = inner
    suppress_before = outer.__suppress_context__
    before = _formatted(outer)
    assert "public-inner" in before, (
        f"the fixture never printed the context to begin with:\n{before}")

    result = db._public_graph(outer)

    assert result is outer, (
        f"an already-public exception was replaced rather than scrubbed: {result!r}")
    assert result.__context__ is inner, (
        f"the public context lost its identity: {result.__context__!r}")
    assert result.__suppress_context__ == suppress_before, (
        "__suppress_context__ was changed from "
        f"{suppress_before} to {result.__suppress_context__}")
    after = _formatted(result)
    assert "public-inner" in after, (
        f"the public context stopped printing.\nBEFORE:\n{before}\nAFTER:\n{after}")
    assert "LookupError" in after, (
        f"the public context's TYPE stopped printing:\n{after}")
    for name in _PRIVATE_NAMES:
        assert name not in after, f"{name} reached the traceback:\n{after}"


async def test_a_public_only_graph_is_left_observably_untouched():
    # A, control. The scrub exists for private links. Handed a graph that has
    # none, it must be a no-op in every respect a caller can observe — not
    # merely "the same objects".
    root = ValueError("public-root")
    middle = RuntimeError("public-middle")
    middle.__context__ = root
    top = OSError("public-top")
    top.__cause__ = middle
    before = _observable(top)

    result = db._public_graph(top)

    assert result is top
    assert _observable(result) == before, (
        f"a public-only graph changed observably.\nBEFORE: {before}\n"
        f"AFTER : {_observable(result)}")


async def test_a_public_error_handled_by_another_keeps_both_through_the_boundary(
    fresh_db
):
    # A, the real path. The body raises a public error WHILE handling another
    # public error, so __context__ is set by the interpreter itself. Everything
    # the boundary does to that exception on the way out has to leave both
    # printable.
    inner_seen = {}

    with pytest.raises(RuntimeError) as excinfo:
        async with db.write_transaction() as tx:
            await tx.execute(
                "INSERT INTO token_blacklist VALUES (?, ?)", ("x", 1.0))
            try:
                raise LookupError("public-inner")
            except LookupError as inner:
                inner_seen["exc"] = inner
                raise RuntimeError("public-outer")

    raised = excinfo.value
    assert raised.__context__ is inner_seen["exc"], (
        f"the body's own context was replaced: {raised.__context__!r}")
    assert raised.__suppress_context__ is False, (
        "the boundary suppressed a context the body never suppressed")
    rendered = _formatted(raised)
    for expected in ("LookupError", "public-inner",
                     "RuntimeError", "public-outer"):
        assert expected in rendered, (
            f"{expected!r} is missing from what a caller prints:\n{rendered}")
    for name in _PRIVATE_NAMES:
        assert name not in rendered, f"{name} reached the traceback:\n{rendered}"
    for node in _exception_graph(raised):
        assert type(node).__name__ not in _PRIVATE_NAMES, (
            f"a private classification is reachable: {node!r}")


class _UnreadableTransactionState:
    """A connection whose transaction state cannot be read, ever.

    The real shape of the failure: aiosqlite raises once it has dropped its
    connection, and the raw sqlite3 handle can still hold an open transaction
    at that moment — so the state is UNKNOWN, never 'safely closed'."""

    @property
    def in_transaction(self):
        raise sqlite3.ProgrammingError("cannot read the transaction state")


async def test_an_unreadable_state_belongs_to_the_incident_that_sent_it():
    # B. The COMMIT could not read the transaction state, reported it, and
    # handed that incident to the cleanup as `caused_by`. The cleanup then
    # could not read the state either — the SAME condition, observed a second
    # time — but the `state is None` branch at its entry ignores `caused_by`
    # and opens an incident of its own. One stuck transaction, two incidents,
    # two fail-stop events.
    conn = _UnreadableTransactionState()
    outcome = db._Outcome()
    caused = outcome.unsafe(
        "commit reported success but the transaction state could not be read")
    try:
        await db._force_close_transaction(conn, outcome, caused_by=caused)

        assert [p.message for p in outcome.problems] == [
            "commit reported success but the transaction state could not be read",
            "transaction state unreadable; cannot prove the transaction is closed",
        ], f"a problem message was dropped: {[p.message for p in outcome.problems]}"
        incidents = {id(p.incident) for p in outcome.problems}
        assert len(incidents) == 1, (
            f"one unreadable transaction state became {len(incidents)} incidents: "
            f"{[(p.message, id(p.incident)) for p in outcome.problems]}")
        assert all(p.incident is caused for p in outcome.problems), (
            "the second observation was not attributed to the incident that "
            "sent the cleanup there")
    finally:
        db._unclosed[:] = [c for c in db._unclosed if c is not conn]


async def test_an_unreadable_state_with_no_cause_is_still_its_own_incident():
    # B, control. Attributing a repeat observation to the incident that caused
    # it must NOT collapse an unreadable state that arrived on its own. With no
    # `caused_by` there is nothing to attach it to, and it is a new failure.
    conn = _UnreadableTransactionState()
    outcome = db._Outcome()
    outcome.unsafe("an earlier, unrelated failure")
    try:
        await db._force_close_transaction(conn, outcome)

        incidents = {id(p.incident) for p in outcome.problems}
        assert len(incidents) == 2, (
            "an independent unreadable state was folded into an unrelated "
            f"incident: {[(p.message, id(p.incident)) for p in outcome.problems]}")
    finally:
        db._unclosed[:] = [c for c in db._unclosed if c is not conn]


@pytest.mark.expect_db_leftovers
async def test_an_unreadable_state_seen_twice_is_one_fail_stop_event(
    fresh_db, monkeypatch, caplog
):
    # B, through the real COMMIT path. Both observations must reach the caller,
    # and an operator counting fail-stop events must see ONE — there is one
    # transaction whose state nobody can read, not two failures.
    monkeypatch.setattr(db, "_transaction_state", lambda conn: None)
    caplog.set_level(logging.ERROR, logger="glassops.db")

    with pytest.raises(db.DatabaseFailStop) as excinfo:
        await asyncio.wait_for(db.blacklist_token("t", 1.0), timeout=20)

    message = str(excinfo.value)
    for expected in (
        "commit reported success but the transaction state could not be read",
        "transaction state unreadable; cannot prove the transaction is closed",
    ):
        assert expected in message, (
            f"the caller was never told {expected!r}: {message}")
    events = _fail_stop_events(caplog)
    assert len(events) == 1, (
        f"one unreadable transaction state produced {len(events)} fail-stop "
        f"events: {events}")


# ═════════════════════════════════════════════════════
# Final limit round, A
#   removing the internal wrapper must not take another
#   public branch out of the graph with it
# ═════════════════════════════════════════════════════


def _public_nodes(exc):
    """Every PUBLIC exception reachable from `exc`, in graph order."""
    return [n for n in _exception_graph(exc)
            if type(n).__name__ not in _PRIVATE_NAMES]


def _private_nodes(exc):
    return [n for n in _exception_graph(exc)
            if type(n).__name__ in _PRIVATE_NAMES]


def _self_cycles(exc):
    """Nodes that are their own cause or their own context."""
    return [n for n in _exception_graph(exc)
            if n.__cause__ is n or n.__context__ is n]


def _assert_every_public_branch_survived(before, result, label):
    """The whole contract in one place: nothing public leaves the graph, no
    private classification stays in it, and the scrub invents no self-cycle."""
    after = _exception_graph(result)
    for node in before:
        if type(node).__name__ in _PRIVATE_NAMES:
            continue
        assert any(n is node for n in after), (
            f"[{label}] a PUBLIC exception left the graph with the private "
            f"object that was removed: {node!r}\n"
            f"before: {[repr(n) for n in before]}\n"
            f"after : {[repr(n) for n in after]}")
    assert _private_nodes(result) == [], (
        f"[{label}] a private classification is still reachable: "
        f"{[repr(n) for n in _private_nodes(result)]}")
    rendered = _formatted(result)
    for name in _PRIVATE_NAMES:
        assert name not in rendered, (
            f"[{label}] {name} reached the traceback:\n{rendered}")
    assert _self_cycles(result) == [], (
        f"[{label}] the scrub created a self-cycle: "
        f"{[repr(n) for n in _self_cycles(result)]}")


async def test_a_public_branch_beside_a_shared_stand_in_survives_the_scrub():
    # A. The wrapper's stand-in is the worker error, and the fail-stop was
    # raised `from` that same worker error — so by the time the scrub reaches
    # the wrapper, its stand-in is ALREADY in the graph. _public_graph handed
    # that object back for the slot the wrapper vacated and skipped the
    # transplant entirely, so the LookupError the wrapper was carrying beside
    # it — reachable through the wrapper and nowhere else — left the graph with
    # the wrapper. Both public errors are diagnostic an operator reads.
    worker_error = RuntimeError("worker-public")
    earlier = LookupError("earlier-public")
    internal = db._CancelledAfterWorkerError(worker_error)
    internal.__cause__ = worker_error      # `raise ... from worker_error`
    internal.__context__ = earlier         # raised while handling `earlier`
    failure = db.DatabaseFailStop("restart-required: shared stand-in")
    failure.__cause__ = worker_error       # `raise failure from worker_error`
    failure.__context__ = internal
    before = _exception_graph(failure)
    assert any(n is earlier for n in before), "the fixture never carried it"

    result = db._public_graph(failure)

    _assert_every_public_branch_survived(before, result, "assembled")
    assert any(n is worker_error for n in _exception_graph(result)), (
        "the worker error a caller acts on was dropped")
    assert any(n is earlier for n in _exception_graph(result)), (
        "the public error that was being handled when the wrapper was raised "
        "disappeared with the wrapper")


async def test_the_real_raise_chain_keeps_every_public_branch_through_the_boundary():
    # A, control. Same shape, but nothing is assembled by hand: Python's own
    # `raise ... from ...` sets every link, _raise_from_private chooses the
    # fail-stop's cause, and _public_form is the boundary a caller reaches
    # through. If the assembled fixture and the real helper chain disagree, the
    # assembled one is the wrong test.
    worker_error = RuntimeError("worker-public")
    failure = None
    try:
        try:
            raise LookupError("earlier-public")
        except LookupError as earlier_exc:
            earlier = earlier_exc
            try:
                raise db._CancelledAfterWorkerError(worker_error) from worker_error
            except db._CancelledAfterWorkerError as internal:
                try:
                    db._raise_from_private(
                        db.DatabaseFailStop("restart-required: real chain"),
                        internal)
                except db.DatabaseFailStop as raised:
                    failure = raised
                    raise
    except db.DatabaseFailStop:
        pass

    before = _exception_graph(failure)
    # The chain Python built for us really is the shape under test.
    assert failure.__cause__ is worker_error, (
        f"_raise_from_private chose a different cause: {failure.__cause__!r}")
    assert isinstance(failure.__context__, db._CancelledAfterWorkerError), (
        f"the wrapper is not the implicit context: {failure.__context__!r}")
    assert failure.__context__.__cause__ is worker_error
    assert failure.__context__.__context__ is earlier
    assert len(_private_nodes(failure)) == 1
    assert len(_public_nodes(failure)) == 3

    result = db._public_form(failure)

    _assert_every_public_branch_survived(before, result, "real chain")
    assert result is failure, (
        f"a public fail-stop was replaced rather than scrubbed: {result!r}")
    assert any(n is worker_error for n in _exception_graph(result)), (
        "the worker error a caller acts on was dropped")
    assert any(n is earlier for n in _exception_graph(result)), (
        "the public error that was being handled when the wrapper was raised "
        "disappeared with the wrapper")
    assert len(_public_nodes(result)) == 3, (
        "the scrub changed how many public exceptions a caller can reach: "
        f"{[repr(n) for n in _public_nodes(result)]}")


# ═════════════════════════════════════════════════════
# F7 — the real COMMIT-response-lost path must keep the
#      driver's own exception OBJECT, not a copy of its text
# ═════════════════════════════════════════════════════


def _raise_lost_commit_response():
    """A named frame, so the traceback the caller keeps has something in it a
    test can point at by name."""
    error = RuntimeError("commit response lost: distinguishable-f7-marker")
    error.add_note("worker-note")
    raise error


async def test_a_lost_commit_response_keeps_the_drivers_own_error_object(
    fresh_db, tmp_path
):
    # F7, through the REAL worker path — the same fault injection as
    # test_durable_commit_with_lost_response_under_cancellation, not a
    # hand-assembled wrapper handed straight to _public_form.
    #
    # The COMMIT is durable, its response is lost, and the caller is cancelled
    # while the worker holds it. The boundary owes a plain CancelledError, and
    # the DatabaseFailStop explains why. But the driver's OWN exception carries
    # the frame it was raised in, its notes, and its own links — and quoting
    # `repr()` of it into the fail-stop message keeps none of those. The object
    # itself has to stay reachable.
    conn = await db._get_conn()
    probe = WorkerProbe("f7")
    await probe.install(conn)
    real_commit = conn.commit
    hit = []
    holder = {}
    submitted = asyncio.Event()
    rollbacks = []
    raised = {}
    real_rollback = conn.rollback

    async def counting_rollback():
        rollbacks.append(1)
        return await real_rollback()

    async def commit_then_lose_response():
        hit.append("commit")
        holder["task"] = asyncio.create_task(_run_probe(conn, probe.name))
        await probe.wait_entered()
        inner = asyncio.ensure_future(real_commit())
        await _wait_until_queued(conn, "the COMMIT")
        submitted.set()
        await inner                       # DURABLE once the gate releases
        hit.append("durable")
        try:
            _raise_lost_commit_response()
        except RuntimeError as error:
            raised["error"] = error
            raise

    conn.commit = commit_then_lose_response
    conn.rollback = counting_rollback
    task = asyncio.create_task(db.store_net_audit("a1", 1.0, [_event()], []))
    try:
        await asyncio.wait_for(submitted.wait(), timeout=15)
        task.cancel()
        probe.let_go()
        with pytest.raises(asyncio.CancelledError) as excinfo:
            await asyncio.wait_for(task, timeout=25)
    finally:
        probe.let_go()
        if "commit" in conn.__dict__:
            del conn.commit
        conn.rollback = real_rollback
        if holder.get("task"):
            await asyncio.gather(holder["task"], return_exceptions=True)

    assert hit == ["commit", "durable"], f"the fault sequence never ran: {hit}"
    driver_error = raised["error"]
    assert driver_error.__traceback__ is not None, "the fixture kept no traceback"

    public = excinfo.value
    graph = _exception_graph(public)

    # 1. the caller still gets a plain cancellation, exactly that type
    assert type(public) is asyncio.CancelledError, (
        f"an internal exception type reached a public caller: {public!r}")

    # 2. the fail-stop that explains WHY is still in the graph
    fail_stops = [n for n in graph if isinstance(n, db.DatabaseFailStop)]
    assert fail_stops, (
        f"the fail-stop left the graph: {[repr(n) for n in graph]}")

    # 3. the driver's OWN object — same identity, not a copy, not a quote
    assert any(n is driver_error for n in graph), (
        "the driver's exception object left the graph; only its text survived "
        f"in {[repr(n) for n in graph]}")

    # 4. its traceback survives, and names the frame it was raised in
    frames = [f.name for f in
              traceback.extract_tb(driver_error.__traceback__)]
    assert "_raise_lost_commit_response" in frames, (
        f"the driver error's own frame is gone: {frames}")

    # 5. its notes survive
    assert "worker-note" in getattr(driver_error, "__notes__", []), (
        f"the note was dropped: {getattr(driver_error, '__notes__', None)}")

    # 6. no private vocabulary anywhere — graph or rendered traceback
    _assert_no_private_vocabulary("lost commit response", public)

    # 7. no cycle, no node linked to itself, no slot duplicated on one parent
    for node in graph:
        assert node.__cause__ is not node and node.__context__ is not node, (
            f"the scrub created a self-cycle at {node!r}")
        assert not (node.__cause__ is not None
                    and node.__cause__ is node.__context__), (
            f"one node fills both slots of {node!r}")

    # 8. the durable COMMIT was never rolled back, and the latch holds
    assert rollbacks == [], "a ROLLBACK was submitted over a DURABLE commit"
    assert db._fail_stop is not None, "a lost commit response did not fail-stop"
    with pytest.raises(db.DatabaseFailStop):
        await db.store_metric("a1", 2.0, _metric())

    raw = sqlite3.connect(str(tmp_path / "t.db"))
    try:
        assert raw.execute(
            "SELECT COUNT(*) FROM net_conn_events").fetchone()[0] == 1
    finally:
        raw.close()
