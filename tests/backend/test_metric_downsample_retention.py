"""Per-agent downsample progress + coverage-gated raw retention (CP-2 / CP-3).

CP-2: one global `MAX(timestamp)` watermark per resolution let a fast agent's
progress skip a slower agent's still-valid raw rows.
CP-3: the real maintenance order (1m -> 5m -> cleanup) then deleted those raw
rows purely on age, so the slow agent's data existed in NO table afterwards.

These tests drive the real product functions against a real SQLite file and
monkeypatch only the wall clock.
"""

import asyncio
import json
import sqlite3
import time as real_time

import aiosqlite
import pytest

import app.database as db


# CP-2 / CP-3 fixed inputs, taken from the Phase 0 causal proof (not imported:
# the runner is evidence, not a gate).
T0 = 1787200500.0
T1 = 1787201700.0          # T0 + 1200: pushes A_TS_LATE past the 1h raw cutoff
A_TS = 1787200101.0        # agent-a, completed 1m bucket 1787200080
B_TS = 1787197500.0        # agent-b, T0-3000: still inside raw retention at T0


class _Clock:
    """Wall clock only. monotonic() stays real so the DB boundary's own
    timeouts keep working."""

    def __init__(self, t: float) -> None:
        self.t = t

    def time(self) -> float:
        return self.t

    def monotonic(self) -> float:
        return real_time.monotonic()

    def sleep(self, s):  # pragma: no cover - not used by the paths under test
        return real_time.sleep(s)


@pytest.fixture
def clock(monkeypatch):
    c = _Clock(T0)
    monkeypatch.setattr(db, "time", c)
    return c


@pytest.fixture
def db_file(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "_db_path", str(tmp_path / "t.db"))
    monkeypatch.setattr(db, "_conn", None)
    monkeypatch.setattr(db, "_metric_conn", None, raising=False)
    monkeypatch.setattr(db, "_op_lock", asyncio.Lock(), raising=False)
    monkeypatch.setattr(db, "_fail_stop", None, raising=False)
    return str(tmp_path / "t.db")


@pytest.fixture
async def fresh_db(db_file):
    await db.init_db()
    yield db
    await db.close_db()
    db._closed = False


def _metric(cpu: float = 10.0) -> dict:
    return {
        "cpu": {"percent_total": cpu, "percent_per_core": [cpu]},
        "memory": {"percent": cpu},
        "disk": {"percent": cpu},
    }


async def _raw_count(agent_id: str) -> int:
    rows = await db._fetch_all(
        "SELECT COUNT(*) FROM metrics WHERE agent_id = ?", (agent_id,))
    return rows[0][0]


async def _ds_count(agent_id: str, resolution: str) -> int:
    rows = await db._fetch_all(
        "SELECT COUNT(*) FROM metrics_downsampled WHERE agent_id = ? AND resolution = ?",
        (agent_id, resolution))
    return rows[0][0]


async def _ds_rows(agent_id: str, resolution: str) -> list:
    return await db._fetch_all(
        "SELECT timestamp, data FROM metrics_downsampled "
        "WHERE agent_id = ? AND resolution = ? ORDER BY timestamp",
        (agent_id, resolution))


async def _proven_ids(agent_id: str) -> set:
    """(raw_id, resolution) pairs a published summary actually proves."""
    rows = await db._fetch_all(
        "SELECT c.raw_id AS raw_id, c.resolution AS resolution "
        "FROM metric_agg_coverage c JOIN metric_agg_bucket b "
        "  ON b.agent_id = c.agent_id AND b.resolution = c.resolution "
        " AND b.bucket_ts = c.bucket_ts "
        "WHERE c.agent_id = ? AND c.state = 'FOLDED' AND c.raw_id <= b.published_gen",
        (agent_id,))
    return {(r["raw_id"], r["resolution"]) for r in rows}


async def _coverage(agent_id: str) -> list:
    return await db._fetch_all(
        "SELECT raw_id, resolution, bucket_ts, state FROM metric_agg_coverage "
        "WHERE agent_id = ? ORDER BY raw_id, resolution", (agent_id,))


async def _progress(agent_id: str, resolution: str) -> int | None:
    rows = await db._fetch_all(
        "SELECT last_raw_id FROM metric_agg_progress WHERE agent_id = ? AND resolution = ?",
        (agent_id, resolution))
    return rows[0][0] if rows else None


async def _maintenance_cycle() -> int:
    """The real product order from main.py's periodic_maintenance."""
    await db.downsample_metrics(60, "1m")
    await db.downsample_metrics(300, "5m")
    return await db.cleanup_old_metrics(max_age_hours=1)


# ── CP-2 ────────────────────────────────────────────────────────────────


async def test_slow_agent_is_not_skipped_by_another_agents_progress(fresh_db, clock):
    # agent-a first: its completed bucket used to become the GLOBAL watermark.
    await db.store_metric("agent-a", A_TS, _metric(40))
    assert await db.downsample_metrics(60, "1m") == 1

    # agent-b's row is older but still inside the raw retention window.
    await db.store_metric("agent-b", B_TS, _metric(70))
    await db.downsample_metrics(60, "1m")

    assert await _ds_count("agent-b", "1m") == 1, (
        "agent-b's completed 1m bucket was skipped because agent-a had already "
        "moved a shared progress point past it")
    rows = await _ds_rows("agent-b", "1m")
    assert rows[0]["timestamp"] == float(int(B_TS // 60) * 60)
    assert json.loads(rows[0]["data"])["cpu"]["percent_total"] == 70


# ── CP-3 ────────────────────────────────────────────────────────────────


async def test_real_maintenance_order_never_loses_slow_agent_row(fresh_db, clock):
    await db.store_metric("agent-a", A_TS, _metric(40))
    await db.downsample_metrics(60, "1m")
    await db.downsample_metrics(300, "5m")

    # Positive control: an agent whose data is aggregated normally and is
    # equally past the 1h cutoff at T1.
    await db.store_metric("agent-c", B_TS + 500, _metric(55))

    # The slow agent arrives after the fast agent already ran both resolutions.
    await db.store_metric("agent-b", B_TS, _metric(70))

    clock.t = T1  # B_TS is now older than the 1h raw cutoff
    await _maintenance_cycle()

    b_raw = await _raw_count("agent-b")
    b_1m = await _ds_count("agent-b", "1m")
    b_5m = await _ds_count("agent-b", "5m")
    assert (b_raw, b_1m, b_5m) != (0, 0, 0), (
        "agent-b's sample exists in no table at all: permanent hole in the "
        "6h/24h/7d history")
    assert b_1m == 1 and b_5m == 1

    # Positive control: fully summarized raw IS reclaimed by the 1h retention.
    assert await _raw_count("agent-c") == 0
    assert await _ds_count("agent-c", "1m") == 1
    assert await _ds_count("agent-c", "5m") == 1


async def test_cleanup_waits_for_both_resolutions(fresh_db, clock):
    await db.store_metric("agent-b", B_TS, _metric(70))

    # Only the 1m pass ran; the 5m pass has not (crash / not yet its cycle).
    await db.downsample_metrics(60, "1m")

    clock.t = T1
    assert await db.cleanup_old_metrics(max_age_hours=1) == 0
    assert await _raw_count("agent-b") == 1, (
        "an age-eligible raw was deleted while only one of the two resolutions "
        "had summarized it")

    # Now the 5m pass completes; the same raw becomes eligible.
    await db.downsample_metrics(300, "5m")
    assert await db.cleanup_old_metrics(max_age_hours=1) == 1
    assert await _raw_count("agent-b") == 0
    assert await _ds_count("agent-b", "1m") == 1
    assert await _ds_count("agent-b", "5m") == 1


async def test_same_agent_out_of_order_higher_id_is_processed(fresh_db, clock):
    newest = await db.store_metric("agent-a", A_TS, _metric(40))
    await db.downsample_metrics(60, "1m")

    # SAME agent, HIGHER id, OLDER timestamp: an agent_id-scoped MAX(timestamp)
    # watermark would still skip this row.
    older = await db.store_metric("agent-a", B_TS, _metric(70))
    assert older > newest
    await db.downsample_metrics(60, "1m")

    buckets = {r["timestamp"] for r in await _ds_rows("agent-a", "1m")}
    assert float(int(A_TS // 60) * 60) in buckets
    assert float(int(B_TS // 60) * 60) in buckets


async def test_late_row_cannot_overwrite_a_pruned_bucket(fresh_db, clock):
    first = await db.store_metric("agent-b", B_TS, _metric(70))
    second = await db.store_metric("agent-b", B_TS + 1, _metric(80))

    clock.t = T1
    await _maintenance_cycle()
    assert await _raw_count("agent-b") == 0, "setup: the bucket's raw rows were pruned"
    before = json.loads((await _ds_rows("agent-b", "1m"))[0]["data"])
    assert before["cpu"]["percent_total"] == 75.0

    # A late row for the SAME, already-pruned bucket.
    late = await db.store_metric("agent-b", B_TS + 2, _metric(10))
    assert late > second > first
    await db.downsample_metrics(60, "1m")
    await db.downsample_metrics(300, "5m")

    after = json.loads((await _ds_rows("agent-b", "1m"))[0]["data"])
    assert after == before, "a late single row overwrote a summary it cannot reconstruct"

    deleted = await db.cleanup_old_metrics(max_age_hours=1)
    assert deleted == 0
    assert await _raw_count("agent-b") == 1, "the late row was silently dropped"
    proven = await _proven_ids("agent-b")
    assert (late, "1m") not in proven
    assert (late, "5m") not in proven


async def test_incomplete_bucket_does_not_authorize_cleanup(fresh_db, clock):
    # A row inside the CURRENT (still open) 1m and 5m buckets: T0 is exactly
    # a bucket start for both resolutions, so both buckets end in the future.
    open_bucket_ts = T0
    rid = await db.store_metric("agent-b", open_bucket_ts, _metric(70))
    await db.downsample_metrics(60, "1m")
    await db.downsample_metrics(300, "5m")

    assert await _ds_count("agent-b", "1m") == 0
    assert await _ds_count("agent-b", "5m") == 0
    proven = await _proven_ids("agent-b")
    assert (rid, "1m") not in proven
    assert (rid, "5m") not in proven

    # Even once the row is age-eligible, an incomplete bucket never authorized
    # its deletion — and nothing summarized it in the meantime.
    clock.t = open_bucket_ts + 3601
    assert await db.cleanup_old_metrics(max_age_hours=1) == 0
    assert await _raw_count("agent-b") == 1


async def test_summary_and_coverage_rollback_together(fresh_db, clock):
    rid = await db.store_metric("agent-b", B_TS, _metric(70))

    # A REAL SQLite failure at the coverage write, injected with a trigger.
    conn = await db._get_conn()
    await conn.execute(
        "CREATE TRIGGER agg_cov_fault BEFORE INSERT ON metric_agg_coverage "
        "BEGIN SELECT RAISE(ABORT, 'injected coverage fault'); END")
    await conn.commit()

    hit = False
    try:
        await db.downsample_metrics(60, "1m")
    except Exception as exc:  # noqa: BLE001
        hit = "injected coverage fault" in str(exc)
    assert hit, "fault was never reached: the coverage write did not run"

    assert await _ds_count("agent-b", "1m") == 0, "summary committed without coverage"
    assert await _coverage("agent-b") == []
    assert await _progress("agent-b", "1m") in (None, 0)
    assert await _raw_count("agent-b") == 1

    conn = await db._get_conn()
    await conn.execute("DROP TRIGGER agg_cov_fault")
    await conn.commit()

    assert await db.downsample_metrics(60, "1m") == 1
    assert await _ds_count("agent-b", "1m") == 1
    assert {(r["raw_id"], r["resolution"], r["state"]) for r in await _coverage("agent-b")} == {
        (rid, "1m", "FOLDED")}
    assert await _done_count() == 1


async def test_restart_does_not_double_process_raw(fresh_db, clock):
    await db.store_metric("agent-b", B_TS, _metric(70))
    await db.store_metric("agent-b", B_TS + 1, _metric(80))
    await db.downsample_metrics(60, "1m")
    first = json.loads((await _ds_rows("agent-b", "1m"))[0]["data"])
    assert first["cpu"]["percent_total"] == 75.0
    progress = await _progress("agent-b", "1m")
    assert progress is not None and progress > 0

    await db.close_db()
    db._closed = False
    db._conn = None
    db._metric_conn = None
    db._op_lock = asyncio.Lock()
    db._fail_stop = None
    await db.init_db()

    assert await _progress("agent-b", "1m") == progress, "durable progress was lost"
    await db.downsample_metrics(60, "1m")
    again = json.loads((await _ds_rows("agent-b", "1m"))[0]["data"])
    assert again["cpu"]["percent_total"] == 75.0, "the same raw rows were averaged twice"
    assert len(await _ds_rows("agent-b", "1m")) == 1


async def test_offline_agent_is_discovered_from_durable_rows(fresh_db, clock):
    # No websocket, no connection, no in-memory registry: the row is all there is.
    await db.store_metric("offline-agent", B_TS, _metric(70))

    await db.downsample_metrics(60, "1m")
    await db.downsample_metrics(300, "5m")

    assert await _ds_count("offline-agent", "1m") == 1
    assert await _ds_count("offline-agent", "5m") == 1


async def test_upgrade_preserves_legacy_history_without_false_coverage(db_file, clock):
    # A pre-upgrade database: old schema, one un-provable raw row and one
    # existing downsampled row.
    legacy = await aiosqlite.connect(db_file)
    try:
        await legacy.execute(
            "CREATE TABLE metrics (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "agent_id TEXT NOT NULL, timestamp REAL NOT NULL, data TEXT NOT NULL)")
        await legacy.execute(
            "CREATE TABLE metrics_downsampled (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "agent_id TEXT NOT NULL, timestamp REAL NOT NULL, resolution TEXT NOT NULL, "
            "data TEXT NOT NULL)")
        await legacy.execute(
            "INSERT INTO metrics (agent_id, timestamp, data) VALUES (?, ?, ?)",
            ("legacy-agent", B_TS, json.dumps(_metric(70))))
        for res in ("1m", "5m"):
            await legacy.execute(
                "INSERT INTO metrics_downsampled (agent_id, timestamp, resolution, data) "
                "VALUES (?, ?, ?, ?)",
                ("legacy-agent", 1787190000.0, res, json.dumps(_metric(33))))
        await legacy.commit()
    finally:
        await legacy.close()

    await db.init_db()
    try:
        # Legacy downsampled history stays queryable.
        history = await db.get_metrics_range("legacy-agent", 1787180000.0, 1787195000.0)
        assert len(history) == 1
        assert history[0]["cpu"]["percent_total"] == 33

        clock.t = T1
        for _ in range(2):
            await _maintenance_cycle()

        assert await _raw_count("legacy-agent") == 1, (
            "a pre-cutover raw whose aggregation cannot be proven was auto-deleted")
        assert [r for r in await _coverage("legacy-agent") if r["state"] == "DONE"] == [], (
            "false coverage was invented for a pre-cutover raw")

        # A post-cutover row in its own bucket is processed normally.
        await db.store_metric("legacy-agent", T1 - 400, _metric(90))
        clock.t = T1 + 4000
        await _maintenance_cycle()
        assert await _ds_count("legacy-agent", "1m") == 2
        assert await _raw_count("legacy-agent") == 1  # only the legacy row remains
    finally:
        await db.close_db()
        db._closed = False


async def test_bounded_batch_resumes_without_gaps_or_duplicates(fresh_db, clock):
    base = T0 - 3600
    expected = {}
    for i in range(6):
        ts = base + i * 60          # one row per distinct, completed 1m bucket
        await db.store_metric("agent-b", ts, _metric(10 + i))
        expected[float(int(ts // 60) * 60)] = float(10 + i)

    made = []
    for _ in range(10):
        made.append(await db.downsample_metrics(60, "1m", batch_limit=2))
    assert max(made) <= 2, "a bounded batch processed more rows than its limit"

    rows = await _ds_rows("agent-b", "1m")
    assert len(rows) == len(expected), "a bucket was skipped or duplicated"
    got = {r["timestamp"]: json.loads(r["data"])["cpu"]["percent_total"] for r in rows}
    assert got == expected


async def test_pending_marker_is_not_provenance_for_a_legacy_summary(db_file, clock):
    # The realistic post-upgrade state: a downsampled summary survives, but the
    # raw rows that produced it were pruned by the OLD system before migration,
    # so nothing can prove what went into it.
    legacy_bucket = 1787190000.0
    legacy = await aiosqlite.connect(db_file)
    try:
        await legacy.execute(
            "CREATE TABLE metrics (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "agent_id TEXT NOT NULL, timestamp REAL NOT NULL, data TEXT NOT NULL)")
        await legacy.execute(
            "CREATE TABLE metrics_downsampled (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "agent_id TEXT NOT NULL, timestamp REAL NOT NULL, resolution TEXT NOT NULL, "
            "data TEXT NOT NULL)")
        await legacy.execute(
            "INSERT INTO metrics_downsampled (agent_id, timestamp, resolution, data) "
            "VALUES (?, ?, ?, ?)",
            ("legacy-agent", legacy_bucket, "1m", json.dumps(_metric(33))))
        await legacy.commit()
    finally:
        await legacy.close()

    await db.init_db()
    try:
        # Two post-cutover rows land in the SAME bucket as that legacy summary,
        # and a bounded batch splits them across two passes — so the second pass
        # sees the PENDING marker the first pass wrote.
        await db.store_metric("legacy-agent", legacy_bucket + 10, _metric(90))
        await db.store_metric("legacy-agent", legacy_bucket + 20, _metric(95))

        await db.downsample_metrics(60, "1m", batch_limit=1)
        await db.downsample_metrics(60, "1m", batch_limit=1)

        rows = await _ds_rows("legacy-agent", "1m")
        assert len(rows) == 1
        assert json.loads(rows[0]["data"])["cpu"]["percent_total"] == 33, (
            "a PENDING marker this code wrote itself was accepted as provenance, "
            "and stray rows overwrote a legacy summary they cannot reconstruct")
        assert [r for r in await _coverage("legacy-agent") if r["state"] == "DONE"] == []

        clock.t = legacy_bucket + 7200
        assert await db.cleanup_old_metrics(max_age_hours=1) == 0
        assert await _raw_count("legacy-agent") == 2
    finally:
        await db.close_db()
        db._closed = False


# ── A. a stale DONE label must not authorize deleting the last copy ──────


async def _ds_total() -> int:
    rows = await db._fetch_all("SELECT COUNT(*) FROM metrics_downsampled")
    return rows[0][0]


async def _done_count() -> int:
    """Raw rows a published summary provably covers: folded, and at or below
    the generation their bucket has actually published."""
    rows = await db._fetch_all(
        "SELECT COUNT(*) FROM metric_agg_coverage c JOIN metric_agg_bucket b "
        "  ON b.agent_id = c.agent_id AND b.resolution = c.resolution "
        " AND b.bucket_ts = c.bucket_ts "
        "WHERE c.state = 'FOLDED' AND c.raw_id <= b.published_gen")
    return rows[0][0]


async def test_backward_wall_clock_never_strands_a_done_label(fresh_db, clock):
    raw_ts = T0 - 7200
    await db.store_metric("agent-b", raw_ts, _metric(70))
    await db.downsample_metrics(60, "1m")
    await db.downsample_metrics(300, "5m")

    assert await _raw_count("agent-b") == 1
    assert await _ds_total() == 2
    assert await _done_count() == 2

    # The wall clock steps a long way BACKWARDS (NTP correction, VM restore).
    # Every valid summary is now "in the future" relative to the clock.
    clock.t = T0 - 30 * 86400
    await db.cleanup_old_metrics(max_age_hours=1)
    assert await _ds_total() == 2, (
        "valid downsampled summaries were deleted as 'future' data purely "
        "because the wall clock moved backwards")
    assert await _raw_count("agent-b") == 1

    # Clock returns to normal and the real maintenance order runs.
    clock.t = T0 + 7200
    await _maintenance_cycle()

    raw = await _raw_count("agent-b")
    ds = await _ds_total()
    assert not (raw == 0 and ds == 0), (
        "the raw row was deleted on a DONE label whose summary no longer "
        "exists: the sample's last copy is gone")
    assert ds == 2


async def test_cleanup_requires_the_summary_row_to_still_exist(fresh_db, clock):
    await db.store_metric("agent-b", B_TS, _metric(70))
    await db.downsample_metrics(60, "1m")
    await db.downsample_metrics(300, "5m")
    assert await _done_count() == 2

    # The 5m summary disappears (any cause: retention, corruption, operator).
    conn = await db._get_conn()
    await conn.execute("DELETE FROM metrics_downsampled WHERE resolution = '5m'")
    await conn.commit()

    clock.t = T1
    assert await db.cleanup_old_metrics(max_age_hours=1) == 0, (
        "a DONE coverage label authorized deletion with no 5m summary behind it")
    assert await _raw_count("agent-b") == 1


async def test_cleanup_rejects_coverage_belonging_to_another_agent(fresh_db, clock):
    rid = await db.store_metric("agent-b", B_TS, _metric(70))
    await db.downsample_metrics(60, "1m")
    await db.downsample_metrics(300, "5m")

    # Re-point one coverage row at a different agent. It must stop counting as
    # proof for THIS raw row, whatever summary happens to sit at that bucket.
    conn = await db._get_conn()
    await conn.execute(
        "UPDATE metric_agg_coverage SET agent_id = 'agent-x' "
        "WHERE raw_id = ? AND resolution = '5m'", (rid,))
    await conn.execute(
        "INSERT INTO metrics_downsampled (agent_id, timestamp, resolution, data) "
        "SELECT 'agent-x', timestamp, resolution, data FROM metrics_downsampled "
        "WHERE agent_id = 'agent-b' AND resolution = '5m'")
    await conn.commit()

    clock.t = T1
    assert await db.cleanup_old_metrics(max_age_hours=1) == 0, (
        "coverage recorded against a different agent authorized the delete")
    assert await _raw_count("agent-b") == 1


async def test_coverage_state_and_resolution_are_constrained(fresh_db):
    conn = await db._get_conn()
    for sql, params in (
        ("INSERT INTO metric_agg_coverage (raw_id, resolution, agent_id, bucket_ts, state) "
         "VALUES (?, '1m', 'a', 0.0, 'WHATEVER')", (9001,)),
        ("INSERT INTO metric_agg_coverage (raw_id, resolution, agent_id, bucket_ts, state) "
         "VALUES (?, '15m', 'a', 0.0, 'DONE')", (9002,)),
        ("INSERT INTO metric_agg_coverage (raw_id, resolution, agent_id, bucket_ts, state) "
         "VALUES (?, '', 'a', 0.0, 'DONE')", (9003,)),
    ):
        with pytest.raises(sqlite3.IntegrityError):
            await conn.execute(sql, params)
    await conn.rollback()


# ── B. bounded means bounded rows, not a bounded candidate SELECT ────────


async def _partial_rows(resolution: str) -> list:
    return await db._fetch_all(
        "SELECT agent_id, bucket_ts, n FROM metric_agg_partial WHERE resolution = ?",
        (resolution,))


async def test_batch_limit_bounds_the_raw_rows_actually_processed(fresh_db, clock):
    bucket = T0 - 3600
    for i in range(10):
        await db.store_metric("agent-b", bucket + i * 5, _metric(10 + i))

    made = await db.downsample_metrics(60, "1m", batch_limit=2)

    cov = await db._fetch_all("SELECT COUNT(*) FROM metric_agg_coverage")
    assert cov[0][0] <= 2, (
        "one call folded and recorded more raw rows than batch_limit: the limit "
        f"only bounds the candidate SELECT (recorded {cov[0][0]} of 10)")
    assert made == 0, "a bucket was published before all of its rows were folded in"
    assert await _ds_count("agent-b", "1m") == 0
    assert await _done_count() == 0

    partial = await _partial_rows("1m")
    assert len(partial) == 1 and partial[0]["n"] == 2

    # And it resumes: repeated bounded calls finish the bucket exactly once.
    for _ in range(10):
        await db.downsample_metrics(60, "1m", batch_limit=2)
    rows = await _ds_rows("agent-b", "1m")
    assert len(rows) == 1
    assert json.loads(rows[0]["data"])["cpu"]["percent_total"] == 14.5  # mean(10..19)
    assert await _partial_rows("1m") == []
    assert await _done_count() == 10


async def test_partial_bucket_is_not_deletable_and_resumes_after_restart(fresh_db, clock):
    bucket = T0 - 7200
    for i in range(6):
        await db.store_metric("agent-b", bucket + i * 5, _metric(10 + i))

    await db.downsample_metrics(60, "1m", batch_limit=2)
    await db.downsample_metrics(300, "5m", batch_limit=2)

    clock.t = T0
    assert await db.cleanup_old_metrics(max_age_hours=1) == 0, (
        "raw rows of a half-folded bucket were deleted")
    assert await _raw_count("agent-b") == 6

    await db.close_db()
    db._closed = False
    db._conn = None
    db._metric_conn = None
    db._op_lock = asyncio.Lock()
    db._fail_stop = None
    await db.init_db()

    for _ in range(20):
        await db.downsample_metrics(60, "1m", batch_limit=2)
        await db.downsample_metrics(300, "5m", batch_limit=2)
    assert json.loads((await _ds_rows("agent-b", "1m"))[0]["data"])["cpu"]["percent_total"] == 12.5
    assert json.loads((await _ds_rows("agent-b", "5m"))[0]["data"])["cpu"]["percent_total"] == 12.5
    assert len(await _ds_rows("agent-b", "1m")) == 1
    assert await db.cleanup_old_metrics(max_age_hours=1) == 6
    assert await _raw_count("agent-b") == 0


# ── C. coverage capacity and cleanup cost ───────────────────────────────


async def _count(table: str) -> int:
    rows = await db._fetch_all(f"SELECT COUNT(*) FROM {table}")  # noqa: S608 - fixed literals
    return rows[0][0]


async def test_coverage_does_not_outlive_the_raw_it_covers(fresh_db, clock):
    base = float(int((T0 - 7200) // 300) * 300)
    n = 40
    for i in range(n):
        await db.store_metric("agent-b", base + i * 5, _metric(10 + i % 7))

    clock.t = base + 7200
    for _ in range(30):
        await _maintenance_cycle()

    assert await _raw_count("agent-b") == 0, "setup: every raw should be reclaimed"
    assert await _count("metric_agg_coverage") == 0, (
        "per-raw coverage survives its raw row and is retained for 7 days: "
        f"{await _count('metric_agg_coverage')} rows for {n} reclaimed raws")

    long_lived = await _count("metric_agg_bucket")
    distinct_1m = len({int((base + i * 5) // 60) for i in range(n)})
    distinct_5m = len({int((base + i * 5) // 300) for i in range(n)})
    assert long_lived == distinct_1m + distinct_5m
    assert long_lived < 2 * n


async def test_periodic_cleanup_does_not_scan_summary_or_coverage_tables(fresh_db, clock):
    await db.store_metric("agent-b", B_TS, _metric(70))
    await db.downsample_metrics(60, "1m")
    await db.downsample_metrics(300, "5m")

    # EXPLAIN the real statements the periodic cleanup runs, verbatim.
    conn = await db._get_conn()
    plans = []
    for sql, params in db._cleanup_query_plans(now=T1, max_age_hours=1):
        cursor = await conn.execute("EXPLAIN QUERY PLAN " + sql, params)
        try:
            plans.extend(str(row[-1]) for row in await cursor.fetchall())
        finally:
            await cursor.close()

    scans = [p for p in plans
             if p.startswith("SCAN") and any(t in p for t in (
                 "metric_agg_coverage", "metric_agg_bucket", "metric_agg_partial",
                 "metrics_downsampled"))]
    assert not scans, f"periodic cleanup full-scans coverage state: {scans}\nall: {plans}"


# ── D. small integrity corrections ──────────────────────────────────────


@pytest.mark.parametrize("seconds,label", [(60, "5m"), (300, "1m"), (60, "1h"),
                                           (900, "15m"), (60, "")])
async def test_downsample_rejects_mismatched_resolution(fresh_db, clock, seconds, label):
    await db.store_metric("agent-b", B_TS, _metric(70))
    before = (await _count("metrics_downsampled"), await _count("metric_agg_coverage"),
              await _count("metric_agg_progress"))

    with pytest.raises(db.InvalidAggregationResolution):
        await db.downsample_metrics(seconds, label)

    assert (await _count("metrics_downsampled"), await _count("metric_agg_coverage"),
            await _count("metric_agg_progress")) == before


def _shaped(cpu, host, status, gpu_util, container_cpu):
    return {
        "host": host,
        "cpu": {"percent_total": cpu, "percent_per_core": [cpu, cpu / 2]},
        "memory": {"percent": cpu}, "disk": {"percent": cpu},
        "gpu": [{"gpu_util": gpu_util, "mem_util": gpu_util, "temperature": 0,
                 "power_watts": 0, "clock_sm_mhz": 0}],
        "containers": [{"name": "w", "id": "c1", "image": "img", "status": status,
                        "state": "running", "ports": [], "cpu_percent": container_cpu,
                        "mem_usage": 1.0, "mem_limit": 100}],
    }


async def test_bucket_recompute_follows_timestamp_order_not_arrival_order(fresh_db, clock):
    bucket = T0 - 7200
    earlier = _shaped(10, "first", "created", 10, 1.0)
    later = _shaped(30, "second", "running", 30, 3.0)

    # Arrival order is REVERSED: the later timestamp gets the LOWER raw id.
    await db.store_metric("agent-b", bucket + 20, later)
    await db.store_metric("agent-b", bucket + 10, earlier)

    await db.downsample_metrics(60, "1m")
    stored = json.loads((await _ds_rows("agent-b", "1m"))[0]["data"])

    assert stored == db._average_metrics([earlier, later]), (
        "the bucket was recomputed in arrival order, not timestamp order")
    assert stored["host"] == "first", "template came from the later-timestamped row"
    assert stored["containers"][0]["status"] == "running", (
        "container metadata did not take the latest-by-timestamp sample")
    assert stored["cpu"]["percent_total"] == 20.0
    assert stored["gpu"][0]["gpu_util"] == 20.0


# ── 1. a not-yet-due row must not starve the rows behind it ──────────────


async def test_future_row_cannot_starve_later_completed_rows(fresh_db, clock):
    # id 1 is far in the future; ids 2..4 sit in buckets that closed long ago.
    future_ts = T0 + 30 * 86400
    future_id = await db.store_metric("agent-b", future_ts, _metric(99))
    past_ids = [await db.store_metric("agent-b", T0 - 7200 + i * 120, _metric(10 + i))
                for i in range(3)]
    assert future_id < min(past_ids)

    for _ in range(12):
        await db.downsample_metrics(60, "1m", batch_limit=2)
        await db.downsample_metrics(300, "5m", batch_limit=2)

    assert await _ds_count("agent-b", "1m") == 3, (
        "a single not-yet-due row held the scan window shut: the closed buckets "
        "behind it were never aggregated")
    assert await _ds_count("agent-b", "5m") == 1

    # The future row is preserved and carries no coverage of its own.
    assert await _raw_count("agent-b") == 4
    cov = {r["raw_id"] for r in await _coverage("agent-b")}
    assert future_id not in cov

    clock.t = T0 + 7200
    assert await db.cleanup_old_metrics(max_age_hours=1) == 3
    assert await _raw_count("agent-b") == 1

    # Once it is genuinely due it is processed like any other row.
    clock.t = future_ts + 7200
    for _ in range(6):
        await db.downsample_metrics(60, "1m", batch_limit=2)
        await db.downsample_metrics(300, "5m", batch_limit=2)
    assert await _ds_count("agent-b", "1m") == 4, "the deferred row was forgotten"


# ── 2. continuous behind-cursor arrivals must not restart forever ────────


async def test_continuous_late_arrivals_do_not_restart_generation_forever(fresh_db, clock):
    bucket = T0 - 7200                       # 1m-aligned and long closed
    for i in range(6):
        await db.store_metric("agent-b", bucket + 30 + i, _metric(10))

    published = 0
    for i in range(12):
        published += await db.downsample_metrics(60, "1m", batch_limit=2)
        # every call is followed by a row BEHIND the resume cursor
        await db.store_metric("agent-b", bucket + 1 + i * 0.1, _metric(50))
    assert published >= 1, (
        "no frozen generation ever reached publication: each late arrival "
        "restarted the accumulation from scratch")

    # Input stops: the bucket converges and every row counts exactly once.
    for _ in range(40):
        await db.downsample_metrics(60, "1m", batch_limit=2)
    rows = await _ds_rows("agent-b", "1m")
    assert len(rows) == 1
    total = await _raw_count("agent-b")
    assert total == 18
    assert json.loads(rows[0]["data"])["cpu"]["percent_total"] == pytest.approx(
        (6 * 10 + 12 * 50) / total)


# ── 3. publication must not rewrite the whole bucket's coverage ──────────


async def _install_coverage_audit() -> None:
    conn = await db._get_conn()
    await conn.execute("CREATE TABLE IF NOT EXISTS cov_audit (n INTEGER)")
    await conn.execute(
        "CREATE TRIGGER cov_audit_ins AFTER INSERT ON metric_agg_coverage "
        "BEGIN INSERT INTO cov_audit (n) VALUES (1); END")
    await conn.execute(
        "CREATE TRIGGER cov_audit_upd AFTER UPDATE ON metric_agg_coverage "
        "BEGIN INSERT INTO cov_audit (n) VALUES (1); END")
    await conn.commit()


async def _coverage_writes() -> int:
    return (await db._fetch_all("SELECT COUNT(*) FROM cov_audit"))[0][0]


async def test_finalization_never_mutates_more_than_batch_limit_coverage_rows(fresh_db, clock):
    bucket = T0 - 7200
    for i in range(10):
        await db.store_metric("agent-b", bucket + i, _metric(10 + i))
    await _install_coverage_audit()

    per_call = []
    for _ in range(12):
        before = await _coverage_writes()
        await db.downsample_metrics(60, "1m", batch_limit=2)
        per_call.append(await _coverage_writes() - before)

    assert await _ds_count("agent-b", "1m") == 1, "setup: the bucket must finish"
    assert sum(per_call) >= 10, "setup: every row must have been recorded"
    assert max(per_call) <= 2, (
        "a call wrote more coverage rows than batch_limit — the publishing call "
        f"rewrites the whole bucket: {per_call}")


# ── 4. the 7-day sweep must not strand raws it still owes a summary ──────


async def test_seven_day_gc_waits_for_remaining_raw_coverage(fresh_db, clock):
    old = T0 - 8 * 86400
    for i in range(6):
        await db.store_metric("agent-b", old + i, _metric(10 + i))
    await db.downsample_metrics(60, "1m")
    await db.downsample_metrics(300, "5m")
    assert await _raw_count("agent-b") == 6
    assert await _ds_count("agent-b", "1m") == 1
    assert await _ds_count("agent-b", "5m") == 1

    deleted = 0
    for _ in range(10):
        deleted += await db.cleanup_old_metrics(max_age_hours=1, batch_limit=2)

    assert await _raw_count("agent-b") == 0, (
        "raw rows were stranded: the 7-day sweep pruned the summary their "
        "deletion proof depends on while they were still waiting their turn")
    assert deleted == 6
    # ...and once nothing depends on them, normal 7-day retention does run.
    assert await _count("metric_agg_coverage") == 0
    assert await _ds_count("agent-b", "1m") == 0
    assert await _ds_count("agent-b", "5m") == 0
    assert await _count("metric_agg_bucket") == 0


# ── WIP schema migration: an unfinished PARTIAL is not a finished fold ────


# The immediately-previous WIP shape: coverage carried a PARTIAL state, the
# bucket had no published_gen, the accumulator had no through_raw_id.
_WIP_COVERAGE_DDL = (
    "CREATE TABLE metric_agg_coverage ("
    " raw_id INTEGER NOT NULL,"
    " resolution TEXT NOT NULL CHECK (resolution IN ('1m', '5m')),"
    " agent_id TEXT NOT NULL,"
    " bucket_ts REAL NOT NULL,"
    " state TEXT NOT NULL CHECK (state IN ('PARTIAL', 'DONE', 'PENDING')),"
    " PRIMARY KEY (raw_id, resolution))"
)
_WIP_BUCKET_DDL = (
    "CREATE TABLE metric_agg_bucket ("
    " agent_id TEXT NOT NULL,"
    " resolution TEXT NOT NULL CHECK (resolution IN ('1m', '5m')),"
    " bucket_ts REAL NOT NULL,"
    " state TEXT NOT NULL CHECK (state IN ('OPEN', 'SEALED')),"
    " PRIMARY KEY (agent_id, resolution, bucket_ts))"
)
_WIP_PARTIAL_DDL = (
    "CREATE TABLE metric_agg_partial ("
    " agent_id TEXT NOT NULL, resolution TEXT NOT NULL, bucket_ts REAL NOT NULL,"
    " n INTEGER NOT NULL, last_ts REAL NOT NULL, last_raw_id INTEGER NOT NULL,"
    " acc TEXT NOT NULL, PRIMARY KEY (agent_id, resolution, bucket_ts))"
)

_WIP_BUCKET_TS = float(int((T0 - 7200) // 60) * 60)
# ids 1-3 are in the stored summary (mean 20); id 4 arrived late and is not.
_WIP_CPUS = {1: 10.0, 2: 20.0, 3: 30.0, 4: 99.0}
_WIP_FINAL_MEAN = 39.75          # (10 + 20 + 30 + 99) / 4


async def _seed_wip_database(path: str) -> None:
    wip = await aiosqlite.connect(path)
    try:
        await wip.execute(
            "CREATE TABLE metrics (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "agent_id TEXT NOT NULL, timestamp REAL NOT NULL, data TEXT NOT NULL)")
        await wip.execute(
            "CREATE TABLE metrics_downsampled (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "agent_id TEXT NOT NULL, timestamp REAL NOT NULL, resolution TEXT NOT NULL, "
            "data TEXT NOT NULL)")
        await wip.execute(
            "CREATE TABLE runtime_config (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        await wip.execute(_WIP_COVERAGE_DDL)
        await wip.execute(_WIP_BUCKET_DDL)
        await wip.execute(_WIP_PARTIAL_DDL)
        await wip.execute(
            "CREATE TABLE metric_agg_progress (agent_id TEXT NOT NULL, "
            "resolution TEXT NOT NULL, last_raw_id INTEGER NOT NULL, "
            "PRIMARY KEY (agent_id, resolution))")

        await wip.execute(
            "INSERT INTO runtime_config VALUES ('metric_agg_cutover_raw_id', '0')")
        for rid, cpu in _WIP_CPUS.items():
            await wip.execute(
                "INSERT INTO metrics (id, agent_id, timestamp, data) VALUES (?, 'agent-b', ?, ?)",
                (rid, _WIP_BUCKET_TS + rid, json.dumps(_metric(cpu))))
        await wip.execute(
            "INSERT INTO metrics_downsampled (agent_id, timestamp, resolution, data) "
            "VALUES ('agent-b', ?, '1m', ?)", (_WIP_BUCKET_TS, json.dumps(_metric(20.0))))
        for rid, state in ((1, "PARTIAL"), (2, "DONE"), (3, "DONE"), (4, "PARTIAL")):
            await wip.execute(
                "INSERT INTO metric_agg_coverage VALUES (?, '1m', 'agent-b', ?, ?)",
                (rid, _WIP_BUCKET_TS, state))
        await wip.execute(
            "INSERT INTO metric_agg_bucket VALUES ('agent-b', '1m', ?, 'OPEN')",
            (_WIP_BUCKET_TS,))
        await wip.execute(
            "INSERT INTO metric_agg_partial VALUES ('agent-b', '1m', ?, 2, ?, 4, ?)",
            (_WIP_BUCKET_TS, _WIP_BUCKET_TS + 4, json.dumps({"n": 2})))
        # An adversarial-but-durable scan floor: already past every raw row, so
        # the migration cannot rely on progress to re-surface what it drops.
        await wip.execute(
            "INSERT INTO metric_agg_progress VALUES ('agent-b', '1m', 4)")
        await wip.commit()
    finally:
        await wip.close()


async def _cov_states(resolution: str) -> dict:
    rows = await db._fetch_all(
        "SELECT raw_id, state FROM metric_agg_coverage WHERE resolution = ? ORDER BY raw_id",
        (resolution,))
    return {r["raw_id"]: r["state"] for r in rows}


async def _published_gen(resolution: str) -> int | None:
    rows = await db._fetch_all(
        "SELECT published_gen FROM metric_agg_bucket WHERE resolution = ? AND bucket_ts = ?",
        (resolution, _WIP_BUCKET_TS))
    return rows[0][0] if rows else None


async def test_wip_migration_does_not_promote_unfinished_partial_coverage(db_file, clock):
    await _seed_wip_database(db_file)
    await db.init_db()
    try:
        # 1-3. Only a FINISHED fold becomes FOLDED. The two rows that were
        # mid-accumulation carry no coverage, so the candidate scan finds them.
        assert await _cov_states("1m") == {2: "FOLDED", 3: "FOLDED"}, (
            "an unfinished PARTIAL fold was promoted to a finished one")
        assert await _published_gen("1m") == 3
        assert await _count("metric_agg_partial") == 0

        # 6. The durable scan floor must not hide the rows whose coverage was
        # just dropped.
        floor = await _progress("agent-b", "1m")
        assert floor is None or floor < 1, (
            f"progress floor {floor} skips the raw rows the migration dropped")

        # Raw rows and the existing summary are untouched by the migration.
        assert await _raw_count("agent-b") == 4
        assert await _ds_count("agent-b", "1m") == 1
        assert json.loads((await _ds_rows("agent-b", "1m"))[0]["data"])[
            "cpu"]["percent_total"] == 20.0

        # init_db is idempotent.
        await db.init_db()
        assert await _cov_states("1m") == {2: "FOLDED", 3: "FOLDED"}
        assert await _published_gen("1m") == 3
        assert await _raw_count("agent-b") == 4

        # 5. The bucket is recomputed from scratch, every raw exactly once.
        for _ in range(10):
            await db.downsample_metrics(60, "1m", batch_limit=2)
            await db.downsample_metrics(300, "5m", batch_limit=2)

        rows = await _ds_rows("agent-b", "1m")
        assert len(rows) == 1
        assert json.loads(rows[0]["data"])["cpu"]["percent_total"] == _WIP_FINAL_MEAN, (
            "the late row was never folded in: the summary still reflects only "
            "the rows the old build had finished")
        assert await _cov_states("1m") == {1: "FOLDED", 2: "FOLDED",
                                           3: "FOLDED", 4: "FOLDED"}
        assert await _published_gen("1m") == 4
        assert json.loads((await _ds_rows("agent-b", "5m"))[0]["data"])[
            "cpu"]["percent_total"] == _WIP_FINAL_MEAN

        # id 4 was neither dropped while unrepresented nor kept forever.
        clock.t = T0 + 7200
        deleted = 0
        for _ in range(6):
            deleted += await db.cleanup_old_metrics(max_age_hours=1, batch_limit=2)
        assert deleted == 4
        assert await _raw_count("agent-b") == 0
    finally:
        await db.close_db()
        db._closed = False


async def test_wip_migration_keeps_pending_coverage_pending(db_file, clock):
    await _seed_wip_database(db_file)
    conn = await aiosqlite.connect(db_file)
    try:
        await conn.execute(
            "UPDATE metric_agg_coverage SET state = 'PENDING' WHERE raw_id = 3")
        await conn.commit()
    finally:
        await conn.close()

    await db.init_db()
    try:
        states = await _cov_states("1m")
        assert states.get(3) == "PENDING", "a preserved row lost its PENDING marker"
        assert states.get(2) == "FOLDED"
        assert 1 not in states and 4 not in states
        assert await _published_gen("1m") == 2
    finally:
        await db.close_db()
        db._closed = False


# ── malformed sample containment ────────────────────────────────────────


@pytest.mark.parametrize("bad", [
    pytest.param({"cpu": "40", "memory": {"percent": 1}}, id="cpu-is-a-scalar"),
    pytest.param({"cpu": {"percent_total": 1}, "containers": {"web": {}}},
                 id="containers-is-a-mapping"),
    pytest.param({"cpu": {"percent_total": 1}, "gpu": ["nvidia0"]},
                 id="gpu-entry-is-a-scalar"),
])
async def test_one_malformed_sample_does_not_stop_aggregation_for_anyone(
        fresh_db, clock, bad):
    """A sample whose "cpu" (or a "gpu" entry, or "containers") is a scalar
    instead of a mapping must not take the fold down with it.

    Nothing between the agent websocket and store_metric checks the payload's
    shape, so a buggy or hand-rolled agent can store one. The fold already
    carries per-sample latches for exactly this; they only have to catch it.
    If it escapes, downsample_metrics rolls back for EVERY agent — and because
    retention now reclaims a raw row only once a published summary covers it,
    nothing is ever deleted again either. That is the unbounded growth this
    slice exists to stop, so it must not be reachable from one bad row."""
    # B_TS-based, so that at T1 both rows really are past the 1h raw cutoff
    # (A_TS is only 1599s behind T1 and would still be inside the window).
    await db.store_metric("agent-bad", B_TS, bad)
    await db.store_metric("agent-good", B_TS + 500, _metric(55))

    clock.t = T1                      # both rows are now past the 1h raw cutoff
    await _maintenance_cycle()        # must not raise

    # The healthy agent is unaffected: summarized at both resolutions and its
    # raw row reclaimed on schedule.
    assert await _ds_count("agent-good", "1m") == 1
    assert await _ds_count("agent-good", "5m") == 1
    assert await _raw_count("agent-good") == 0, (
        "a malformed sample from ANOTHER agent stopped retention")

    # The malformed row is contained, not lost: it still exists somewhere.
    bad_state = (await _raw_count("agent-bad"),
                 await _ds_count("agent-bad", "1m"),
                 await _ds_count("agent-bad", "5m"))
    assert bad_state != (0, 0, 0), "the malformed sample exists in no table at all"

    # And the boundary is still open for business.
    assert db._fail_stop is None
    assert db.readiness()["status"] == "ready"
