"""Metric ingest seam — server-assigned identity, forged-metadata strip,
ephemeral fallback, and regression pins for the extracted validation logic.

ingest_metric is imported at module level ON PURPOSE: while Task 2 is
unimplemented this whole file REDs as a collection ImportError without
touching test_metric_identity_db.py."""

import asyncio
import json
import threading
import time

import pytest

import app.websocket.agent_ws as agent_ws
from app.websocket.agent_ws import ingest_metric


@pytest.fixture
def fanout(monkeypatch):
    """Capture the store/broadcast/alert fan-out; store succeeds with row id 7.
    Captured dicts are deep-copied (json round-trip): ingest_metric mutates
    `data` in place after the store, and a live reference would let the
    later identity attach leak into the captured store snapshot. The
    module-level after_seq tracker is reset so tests are order-independent."""
    calls = {"stored": [], "broadcast": [], "alerts": []}

    async def fake_store(agent_id, timestamp, data):
        calls["stored"].append((agent_id, timestamp, json.loads(json.dumps(data))))
        return 7

    async def fake_broadcast(agent_id, data):
        calls["broadcast"].append((agent_id, json.loads(json.dumps(data))))

    async def fake_alert(agent_id, data):
        calls["alerts"].append(agent_id)

    async def fake_net_audit(agent_id, timestamp, data):
        pass

    monkeypatch.setattr(agent_ws, "store_metric", fake_store)
    monkeypatch.setattr(agent_ws, "broadcast_to_clients", fake_broadcast)
    monkeypatch.setattr(agent_ws, "check_and_alert", fake_alert)
    monkeypatch.setattr(agent_ws, "extract_and_store_net_audit", fake_net_audit)
    # Tracker starts at 0 (ingest never seeds — seeding is a startup step,
    # prime_last_assigned_id, covered by its own tests below). These tests
    # exercise the tracker mechanics in isolation without a real DB.
    monkeypatch.setattr(agent_ws, "_last_assigned_id", 0, raising=False)
    return calls


async def test_ingest_overwrites_agent_supplied_identity(fanout):
    now = time.time()
    data = {"timestamp": now, "cpu": {"percent_total": 5},
            "sample_id": "raw:999999", "arrival_seq": 999999,
            "persisted": True, "after_seq": 999998}

    await ingest_metric("a1", data)

    agent_id, timestamp, stored = fanout["stored"][0]
    assert agent_id == "a1"
    assert timestamp == pytest.approx(now)
    # The stored data JSON never carries identity — forged or server-assigned.
    assert all(key not in stored
               for key in ("sample_id", "arrival_seq", "persisted", "after_seq"))

    _, payload = fanout["broadcast"][0]
    assert payload["sample_id"] == "raw:7"
    assert payload["arrival_seq"] == 7
    assert payload["persisted"] is True
    assert "after_seq" not in payload  # durable samples carry no anchor


async def test_ingest_broadcasts_ephemeral_when_store_fails(fanout, monkeypatch):
    async def failing_store(agent_id, timestamp, data):
        raise RuntimeError("db unavailable")

    monkeypatch.setattr(agent_ws, "store_metric", failing_store)

    # Forged reserved keys on the input must be stripped on the ephemeral
    # path too — the server identity replaces them, never the agent's.
    await ingest_metric("a1", {
        "timestamp": time.time(), "cpu": {"percent_total": 5},
        "sample_id": "raw:999999", "arrival_seq": 999999,
        "persisted": True, "after_seq": 999998,
    })

    _, payload = fanout["broadcast"][0]
    assert payload["sample_id"].startswith("ephemeral:")  # not the forged raw:999999
    assert payload["persisted"] is False                  # not the forged True
    assert "arrival_seq" not in payload                   # forged 999999 stripped, none added
    assert payload["after_seq"] == 0  # tracker 0 in this fixture (prime tested separately)
    assert fanout["alerts"] == ["a1"]  # availability path stays alive


async def test_ingest_ephemeral_after_seq_tracks_last_stored_id(fanout, monkeypatch):
    # A successful store advances the tracker; the next failure anchors the
    # ephemeral right after that id — the frontend cannot reconstruct this
    # (its buffer may be empty mid-fetch), so the server must issue it.
    await ingest_metric("a1", {"timestamp": time.time(), "cpu": {"percent_total": 5}})

    async def failing_store(agent_id, timestamp, data):
        raise RuntimeError("db unavailable")

    monkeypatch.setattr(agent_ws, "store_metric", failing_store)
    await ingest_metric("a1", {"timestamp": time.time(), "cpu": {"percent_total": 6}})

    _, ephemeral_payload = fanout["broadcast"][1]
    assert ephemeral_payload["sample_id"].startswith("ephemeral:")
    assert ephemeral_payload["after_seq"] == 7


@pytest.fixture
async def seeded_db(tmp_path, monkeypatch):
    """Real DB with ids 1..3 (newest deleted so sqlite_sequence=3, MAX(id)=2),
    plus captured broadcast/alert. Used by the prime + no-regress tests."""
    import app.database as db

    monkeypatch.setattr(db, "_db_path", str(tmp_path / "t.db"))
    monkeypatch.setattr(db, "_conn", None)
    monkeypatch.setattr(db, "_metric_conn", None, raising=False)
    monkeypatch.setattr(db, "_metric_write_lock", asyncio.Lock(), raising=False)
    monkeypatch.setattr(agent_ws, "_last_assigned_id", 0, raising=False)
    await db.init_db()
    for i in range(3):
        await db.store_metric("a1", 100.0 + i, {"cpu": {"percent_total": i}})
    conn = await db.get_db()
    await conn.execute("DELETE FROM metrics WHERE id = 3")  # cleanup drops the newest
    await conn.commit()

    broadcasts = []

    async def capture(agent_id, data):
        broadcasts.append(json.loads(json.dumps(data)))

    async def noop(*args, **kwargs):
        pass

    monkeypatch.setattr(agent_ws, "broadcast_to_clients", capture)
    monkeypatch.setattr(agent_ws, "check_and_alert", noop)
    monkeypatch.setattr(agent_ws, "extract_and_store_net_audit", noop)
    yield db, broadcasts, monkeypatch
    await db.close_db()


async def test_prime_seeds_after_seq_from_sqlite_sequence(seeded_db):
    # r3.3 #1: prime at startup seeds the tracker from sqlite_sequence (last
    # issued = 3), NOT MAX(id)=2. A store failure right after then anchors the
    # ephemeral at 3, after existing history.
    db, broadcasts, monkeypatch = seeded_db
    await agent_ws.prime_last_assigned_id()

    async def failing_store(agent_id, timestamp, data):
        raise RuntimeError("disk full")

    monkeypatch.setattr(agent_ws, "store_metric", failing_store)
    await ingest_metric("a1", {"timestamp": time.time(), "cpu": {"percent_total": 9}})

    assert broadcasts[0]["after_seq"] == 3


async def test_ephemeral_after_seq_does_not_regress(seeded_db):
    # r3.3 #1: after prime (tracker 3), a successful store advances the tracker
    # to the next id (4); a subsequent failure must anchor at 4, never regress
    # to 3 (the seed-race bug rolled it backwards).
    db, broadcasts, mp = seeded_db
    await agent_ws.prime_last_assigned_id()

    await ingest_metric("a1", {"timestamp": time.time(), "cpu": {"percent_total": 8}})
    assert broadcasts[0]["sample_id"] == "raw:4" and broadcasts[0]["arrival_seq"] == 4

    async def failing_store(agent_id, timestamp, data):
        raise RuntimeError("disk full")

    mp.setattr(agent_ws, "store_metric", failing_store)
    await ingest_metric("a1", {"timestamp": time.time(), "cpu": {"percent_total": 9}})

    assert broadcasts[1]["after_seq"] == 4  # advanced, not the seed 3


async def test_lifespan_fails_startup_and_closes_db_when_prime_raises(monkeypatch):
    # r3.3 #1 + r3.4 #2: the lifespan seeds after init_db and fails startup on
    # a seed error — but must STILL close the DB it opened, or a non-daemon
    # aiosqlite worker survives. Pins init->prime order, propagation, no yield,
    # no maintenance task, and cleanup.
    import app.main as main_mod

    calls = []

    async def fake_init_db():
        calls.append("init")

    async def boom_prime():
        calls.append("prime")
        raise RuntimeError("prime failed at startup")

    async def fake_close_db():
        calls.append("close")

    created = []
    real_create_task = asyncio.create_task

    def tracking_create_task(coro, *a, **k):
        created.append(getattr(coro, "__name__", ""))
        return real_create_task(coro, *a, **k)

    monkeypatch.setattr(main_mod, "init_db", fake_init_db)
    monkeypatch.setattr(main_mod, "close_db", fake_close_db)
    monkeypatch.setattr("app.websocket.agent_ws.prime_last_assigned_id", boom_prime)
    monkeypatch.setattr(main_mod.asyncio, "create_task", tracking_create_task)

    entered = False
    with pytest.raises(RuntimeError, match="prime failed at startup"):
        async with main_mod.lifespan(main_mod.app):
            entered = True  # yield body — must never run

    assert calls == ["init", "prime", "close"]  # order + close ran despite failure
    assert entered is False                       # requests never accepted
    assert "periodic_maintenance" not in created  # maintenance never started


async def test_lifespan_runs_maintenance_and_cleans_up(tmp_path, monkeypatch):
    # r3.5 #2 / r3.6 #3: the NORMAL path — the real lifespan must create the
    # maintenance task, let it actually run into its sleep loop, then on
    # shutdown cancel/await it and close BOTH DB connections. Uses the real
    # init_db/prime/close_db against a temp DB.
    import app.database as db
    import app.main as main_mod

    monkeypatch.setattr(db, "_db_path", str(tmp_path / "t.db"))
    monkeypatch.setattr(db, "_conn", None)
    monkeypatch.setattr(db, "_metric_conn", None, raising=False)
    monkeypatch.setattr(db, "_metric_write_lock", asyncio.Lock(), raising=False)
    monkeypatch.setattr(agent_ws, "_last_assigned_id", 0, raising=False)

    before = set(asyncio.all_tasks())
    async with main_mod.lifespan(main_mod.app):
        assert db._conn is not None  # init_db ran
        # Hand control to the loop so the task really starts executing. A
        # `...`/no-op body would RUN TO COMPLETION here (done() is True); the
        # real body parks in `await asyncio.sleep(60)` and stays pending.
        for _ in range(3):
            await asyncio.sleep(0)
        maint = [t for t in asyncio.all_tasks() - before
                 if "periodic_maintenance" in t.get_coro().__qualname__]
        assert len(maint) == 1
        assert not maint[0].done()  # parked in the real maintenance loop
        # Open the metric connection too, so close_db has both to clean up.
        await db.store_metric("a1", 100.0, {"cpu": {"percent_total": 1}})
        assert db._metric_conn is not None

    # After shutdown: maintenance cancelled/awaited, both connections closed.
    assert maint[0].done()
    assert db._conn is None and db._metric_conn is None


async def test_ingest_drains_durable_path_when_cancelled_early(seeded_db):
    # Commit-wins, EARLY cancel: the cancellation lands before the store's
    # INSERT/COMMIT even begin. The durable unit must still run to completion
    # (row 4 + tracker + broadcast agree) and the caller still sees the cancel.
    # (The post-COMMIT boundary is the next test.) seeded_db has ids 1,2
    # (3 deleted), so the store gets id 4; prime seeds the tracker to 3.
    db, broadcasts, mp = seeded_db
    await agent_ws.prime_last_assigned_id()  # tracker 3

    started = asyncio.Event()
    real_store = agent_ws.store_metric

    async def gated_store(agent_id, timestamp, data):
        started.set()
        await asyncio.sleep(0)  # cancel lands here — before INSERT/COMMIT
        return await real_store(agent_id, timestamp, data)

    mp.setattr(agent_ws, "store_metric", gated_store)

    task = asyncio.create_task(
        ingest_metric("a1", {"timestamp": time.time(), "cpu": {"percent_total": 8}}))
    await asyncio.wait_for(started.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=5)

    assert broadcasts and broadcasts[-1]["sample_id"] == "raw:4"
    assert broadcasts[-1]["arrival_seq"] == 4
    assert agent_ws._last_assigned_id == 4
    conn = await db.get_db()
    cursor = await conn.execute("SELECT COUNT(*) FROM metrics WHERE id = 4")
    assert (await cursor.fetchone())[0] == 1


async def test_ingest_drains_durable_path_when_worker_is_executing_commit(seeded_db):
    # r3.7 #2: the TRUE boundary — the COMMIT callable is submitted to and
    # running on the aiosqlite worker thread when the cancellation lands. This
    # is the case a submitted COMMIT cannot undo, so commit-wins must hold: the
    # outer task cannot finish while the worker holds the commit, and once
    # released the DB row / tracker / broadcast identity all agree before the
    # caller finally sees CancelledError.
    # (Uses aiosqlite's private _execute to gate exactly at the worker
    # boundary; aiosqlite is pinned at 0.21.0 in backend/requirements.txt.)
    db, broadcasts, mp = seeded_db
    await agent_ws.prime_last_assigned_id()  # tracker 3
    conn = await db._get_metric_db()

    entered = threading.Event()   # worker thread signals it is inside COMMIT
    release = threading.Event()   # main thread releases it after cancelling
    original_commit = conn.commit         # aiosqlite's bound coroutine
    real_commit_fn = conn._conn.commit    # the underlying sqlite3 commit

    async def gated_commit():
        def worker():
            entered.set()
            release.wait()        # hold the worker inside the commit step
            return real_commit_fn()

        return await conn._execute(worker)

    conn.commit = gated_commit

    # Capture the INNER durable task: the outer ingest task defers its own
    # cancellation until the durable unit finishes, so cancelling only the
    # outer task can never bound this test — cleanup must cancel the durable
    # one directly (r3.8 #1).
    durable_task: asyncio.Task | None = None
    real_persist = agent_ws._persist_and_broadcast

    async def capturing_persist(*args, **kwargs):
        nonlocal durable_task
        durable_task = asyncio.current_task()
        return await real_persist(*args, **kwargs)

    mp.setattr(agent_ws, "_persist_and_broadcast", capturing_persist)

    task = None
    try:
        task = asyncio.create_task(
            ingest_metric("a1", {"timestamp": time.time(), "cpu": {"percent_total": 8}}))
        # Bounded: if COMMIT never reaches the worker, fail fast instead of
        # blocking the suite forever.
        entered_ok = await asyncio.wait_for(asyncio.to_thread(entered.wait, 5), timeout=6)
        assert entered_ok, "COMMIT never reached the aiosqlite worker"
        task.cancel()
        for _ in range(3):
            await asyncio.sleep(0)
        assert not task.done()  # commit-wins: cannot finish before the drain does

        release.set()
        # Bounded completion check: asyncio.wait ALWAYS returns control after
        # the timeout, even if the task refuses to finish — unlike
        # wait_for(task), which on timeout cancels the task and then blocks
        # awaiting a cancellation the drain loop keeps deferring.
        _, pending = await asyncio.wait({task}, timeout=5)
        assert not pending, "ingest task did not finish after COMMIT was released"
        assert task.cancelled()  # caller receives the deferred cancellation
    finally:
        release.set()                 # never leave the worker thread blocked
        conn.commit = original_commit
        for t in (durable_task, task):  # inner first, then outer
            if t is not None and not t.done():
                t.cancel()
        cleanup = [t for t in (durable_task, task) if t is not None]
        if cleanup:
            _, cleanup_pending = await asyncio.wait(set(cleanup), timeout=5)  # bounded
            assert not cleanup_pending, "metric ingest cleanup left pending tasks"

    assert broadcasts and broadcasts[-1]["sample_id"] == "raw:4"
    assert broadcasts[-1]["arrival_seq"] == 4
    assert agent_ws._last_assigned_id == 4
    check = await db.get_db()
    cursor = await check.execute("SELECT COUNT(*) FROM metrics WHERE id = 4")
    assert (await cursor.fetchone())[0] == 1


async def test_ingest_propagates_child_cancellation_without_spinning(fanout, monkeypatch):
    # r3.6 #1 regression: when the durable task is cancelled itself, the drain
    # loop must propagate at once. This pins the COMBINATION of the
    # `while not core.done()` loop condition and the `core.cancelled()` guard:
    # either one alone already stops the spin, so this only catches the
    # historical regression shape where BOTH are gone (`while True:`, no
    # done()-check, no guard) — that shape re-shields the already-cancelled
    # future synchronously forever, a tight spin that never yields, so an
    # outer wait_for could never fire (its timeout callback never runs).
    # Count shield calls instead: the fixed loop shields exactly once, and a
    # regression trips the assert on the second call — fast RED, no hang.
    async def self_cancelling(agent_id, timestamp, data):
        asyncio.current_task().cancel()
        await asyncio.sleep(0)

    monkeypatch.setattr(agent_ws, "_persist_and_broadcast", self_cancelling)

    real_shield = asyncio.shield
    calls = 0

    def counting_shield(arg, **kwargs):
        nonlocal calls
        calls += 1
        assert calls <= 1, "drain loop re-shielded a cancelled child (infinite spin)"
        return real_shield(arg, **kwargs)

    monkeypatch.setattr(agent_ws.asyncio, "shield", counting_shield)

    with pytest.raises(asyncio.CancelledError):
        await ingest_metric("a1", {"timestamp": time.time(), "cpu": {"percent_total": 5}})
    assert calls == 1  # propagated on the first shield, never re-shielded


async def test_broadcast_evicts_stalled_client_and_stays_fast(monkeypatch):
    # r3.7 #1: a stalled tab must be evicted by the per-client timeout, so it
    # cannot make every later metric wait that timeout again. A bulk timeout
    # around the whole gather would cancel the eviction and strand it.
    import app.websocket.client_ws as client_ws

    monkeypatch.setattr(client_ws, "CLIENT_SEND_TIMEOUT", 0.05)

    class Healthy:
        def __init__(self):
            self.got = 0

        async def send_text(self, payload):
            self.got += 1

    class Stalled:
        def __init__(self):
            self.calls = 0

        async def send_text(self, payload):
            self.calls += 1
            await asyncio.sleep(3600)

    healthy, stalled = Healthy(), Stalled()
    monkeypatch.setattr(client_ws, "_clients", {healthy, stalled})

    # wait_for bounds the call: without the per-client timeout the stalled
    # fake sleeps 3600s and this step would hang instead of failing.
    await asyncio.wait_for(
        client_ws.broadcast_to_clients("a1", {"cpu": {"percent_total": 1}}), timeout=1)
    assert healthy.got == 1                      # healthy client received it
    assert stalled not in client_ws._clients     # stalled evicted despite timing out
    assert healthy in client_ws._clients

    await asyncio.wait_for(
        client_ws.broadcast_to_clients("a1", {"cpu": {"percent_total": 2}}), timeout=1)
    assert healthy.got == 2
    assert stalled.calls == 1  # never retried — no repeated timeout wait


async def test_ingest_clamps_future_timestamp(fanout):
    # Regression pin for the extracted clamp (agent_ws.py:148-149 semantics).
    # +400 (not +301) keeps the clamp condition true even if the test runner
    # stalls up to 100s between this line and ingest's own time.time().
    before = time.time()
    await ingest_metric("a1", {"timestamp": before + 400, "cpu": {"percent_total": 5}})

    _, timestamp, stored = fanout["stored"][0]
    assert before <= timestamp <= time.time()
    # Pin the canonicalization write-back itself (agent_ws.py's
    # `data["timestamp"] = timestamp`), not just the argument passed
    # alongside it — the stored data JSON and the broadcast payload must
    # both carry the clamped value, not the raw (forged/skewed) one.
    assert before <= stored["timestamp"] <= time.time()

    _, broadcast_data = fanout["broadcast"][0]
    assert before <= broadcast_data["timestamp"] <= time.time()


async def test_ingest_rejects_nonpositive_timestamp(fanout):
    # Regression pin for the extracted validation (agent_ws.py:143-145 semantics).
    await ingest_metric("a1", {"timestamp": 0, "cpu": {"percent_total": 5}})
    assert fanout["stored"] == []
    assert fanout["broadcast"] == []
