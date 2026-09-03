"""Concurrency stress for the DB operation boundary — barrier-driven, not
seed-driven.

A fixed random seed does NOT make worker cancellation deterministic: whether a
cancel lands before or after the statement reaches aiosqlite's worker thread
depends on thread scheduling, which no seed controls. So the cancellations here
are synchronised on an explicit worker-entered event: the target is cancelled
only once its statement is provably owned by the worker.

Every count is asserted exactly, and the run is rejected if the interesting case
(a cancellation delivered while the worker held the statement) never happened.
"""

import asyncio
import threading

import aiosqlite
import pytest

import app.database as db

# Fixed clock: the maintenance paths bucket by timestamp, so a wall-clock read
# would make bucket boundaries — and therefore row counts — vary between runs.
T0 = 1787200500.0

RAW_PER_AGENT = 40
AUDIT_BATCHES = 25
MAINTENANCE_CYCLES = 6
READ_CYCLES = 25
WORKER_CANCELS = 6


@pytest.fixture
async def stress_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "_db_path", str(tmp_path / "stress.db"))
    monkeypatch.setattr(db, "_conn", None)
    monkeypatch.setattr(db, "_metric_conn", None, raising=False)
    monkeypatch.setattr(db, "_op_lock", asyncio.Lock(), raising=False)
    monkeypatch.setattr(db, "_fail_stop", None, raising=False)
    await db.init_db()
    yield tmp_path / "stress.db"
    if not db._closed:
        await db.close_db()
    db._closed = False


def _metric(i):
    return {"timestamp": T0, "cpu": {"percent_total": i % 100},
            "memory": {"percent": i % 50}, "disk": {"percent": i % 25}}


def _event(ts):
    return {"ts": ts, "event": "open", "proto": "tcp", "laddr": "127.0.0.1",
            "lport": 1024 + (int(ts) % 1000), "raddr": "10.0.0.1", "rport": 443,
            "status": "ESTABLISHED", "pid": 7, "pname": "svc", "duration": 0.5}


class _WorkerGate:
    """Blocks a statement INSIDE the aiosqlite worker thread and announces it."""

    def __init__(self):
        self.entered = threading.Event()
        self.release = threading.Event()
        self.hits = 0

    def __call__(self, value=0):
        self.hits += 1
        self.entered.set()
        self.release.wait(timeout=30)
        return value

    async def wait_entered(self, timeout=10.0):
        await asyncio.wait_for(asyncio.to_thread(self.entered.wait, timeout),
                               timeout=timeout + 2)
        assert self.entered.is_set(), "worker never entered the gated statement"

    def reset(self):
        self.entered.clear()
        self.release.clear()


async def test_concurrent_paths_never_wedge_the_file(stress_db):
    counters = {"attempted": 0, "completed": 0, "cancelled": 0,
                "worker_entered_cancellations": 0, "errors": 0}
    errors: list[tuple[str, str]] = []

    def record(where, exc):
        counters["errors"] += 1
        errors.append((where, repr(exc)))

    async def storer(agent, count):
        for i in range(count):
            counters["attempted"] += 1
            try:
                await db.store_metric(agent, T0 - 700 + i, _metric(i))
                counters["completed"] += 1
            except BaseException as exc:
                record("store_metric", exc)
            await asyncio.sleep(0)

    async def auditor(count):
        for i in range(count):
            counters["attempted"] += 2
            try:
                await db.store_net_audit(
                    "a1", T0 - 700 + i, [_event(T0 - 700 + i)], [{"ts": T0 - 700 + i}])
                await db.audit("operator", "stress")
                counters["completed"] += 2
            except BaseException as exc:
                record("net_audit/audit", exc)
            await asyncio.sleep(0)

    async def maintainer(cycles):
        for _ in range(cycles):
            counters["attempted"] += 5
            try:
                await db.downsample_metrics(60, "1m")
                await db.downsample_metrics(300, "5m")
                await db.cleanup_net_audit(7, 30)
                await db.cleanup_audit_log(90, 10_000)
                await db.cleanup_blacklist()
                counters["completed"] += 5
            except BaseException as exc:
                record("maintenance", exc)
            await asyncio.sleep(0)

    async def reader(cycles):
        for _ in range(cycles):
            counters["attempted"] += 5
            try:
                await db.get_recent_metrics("a1", 60)
                await db.get_metrics_range("a1", T0 - 7200, T0)
                await db.get_net_conn_events("a1")
                await db.get_max_metric_id()
                await db.get_audit_log(limit=20)
                counters["completed"] += 5
            except BaseException as exc:
                record("read", exc)
            await asyncio.sleep(0)

    await asyncio.gather(
        storer("a1", RAW_PER_AGENT), storer("a2", RAW_PER_AGENT),
        auditor(AUDIT_BATCHES), maintainer(MAINTENANCE_CYCLES),
        reader(READ_CYCLES), reader(READ_CYCLES),
    )

    # ── worker-entered cancellations, one at a time, each synchronised ──
    conn = await db._get_conn()
    gate = _WorkerGate()
    await conn.create_function("gate", 1, gate)

    for _ in range(WORKER_CANCELS):
        gate.reset()
        counters["attempted"] += 1
        task = asyncio.create_task(db._fetch_all("SELECT gate(1) AS v"))
        await gate.wait_entered()          # the worker provably owns it
        task.cancel()
        counters["worker_entered_cancellations"] += 1
        gate.release.set()                 # the worker runs to completion
        try:
            await asyncio.wait_for(task, timeout=15)
            counters["completed"] += 1
        except asyncio.CancelledError:
            counters["cancelled"] += 1
        except BaseException as exc:
            record("worker_cancel", exc)

    shared = await db._get_conn()
    metric = await db._get_metric_db()
    shared_txn, metric_txn = shared.in_transaction, metric.in_transaction

    probe = await aiosqlite.connect(str(stress_db))
    try:
        cursor = await probe.execute("PRAGMA wal_checkpoint(PASSIVE)")
        busy, log, checkpointed = await cursor.fetchone()
        await cursor.close()
        cursor = await probe.execute("PRAGMA integrity_check")
        integrity = (await cursor.fetchone())[0]
        await cursor.close()
    finally:
        await probe.close()

    print("\nstress counters:", counters)
    print("gate hits:", gate.hits, "| integrity:", integrity,
          "| wal busy/log/ckpt:", busy, log, checkpointed,
          "| unresolved:", len(db._unresolved), "| unclosed:", len(db._unclosed))

    expected_attempts = (
        RAW_PER_AGENT * 2 + AUDIT_BATCHES * 2 + MAINTENANCE_CYCLES * 5
        + READ_CYCLES * 5 * 2 + WORKER_CANCELS
    )
    assert counters["attempted"] == expected_attempts, counters
    assert counters["errors"] == 0, errors[:5]
    assert counters["worker_entered_cancellations"] == WORKER_CANCELS, counters
    assert counters["worker_entered_cancellations"] >= 1
    assert counters["cancelled"] == WORKER_CANCELS, (
        "a cancellation delivered while the worker held the statement did not "
        f"reach the caller: {counters}"
    )
    assert counters["completed"] == expected_attempts - WORKER_CANCELS, counters
    assert gate.hits == WORKER_CANCELS, f"gate hits {gate.hits} != {WORKER_CANCELS}"

    assert integrity == "ok", integrity
    assert db.db_fail_stop_reason() is None, db.db_fail_stop_reason()
    assert shared_txn is False, "shared connection left holding a transaction"
    assert metric_txn is False, "metric connection left holding a transaction"
    assert db._unresolved == [], db._unresolved
    assert db._unclosed == [], db._unclosed
    assert log > 0, "the workload produced no WAL frames — nothing was measured"
    assert busy == 0 and checkpointed == log, (
        f"WAL did not fully drain: busy={busy} checkpointed={checkpointed} log={log}"
    )


async def test_cancellation_is_synchronised_not_seeded(stress_db):
    # Guard the guard: prove the barrier is what makes the cancellation land
    # inside the worker, so this file cannot silently regress to a seeded coin
    # flip that usually cancels before submission.
    conn = await db._get_conn()
    gate = _WorkerGate()
    await conn.create_function("gate", 1, gate)
    task = asyncio.create_task(db._fetch_all("SELECT gate(1) AS v"))
    await gate.wait_entered()
    assert gate.entered.is_set()
    assert gate.hits == 1
    task.cancel()
    gate.release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=15)
