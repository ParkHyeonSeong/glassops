import os

# 32+ chars, not a placeholder -> resolve_secret() accepts it and writes no key file.
os.environ.setdefault("GLASSOPS_SECRET_KEY", "test-secret-key-for-pytest-0123456789abcdef")


import asyncio  # noqa: E402
import time  # noqa: E402

import aiosqlite  # noqa: E402
import pytest  # noqa: E402


def worker_alive(conn) -> bool:
    """True while this connection's aiosqlite worker THREAD is still running.

    `_running` is the wrapper's own flag and aiosqlite clears it before the
    thread has gone — a cancelled connect clears it while the worker is still
    blocked inside sqlite3.connect, and close() clears it while the worker has
    only been handed a stop sentinel. The thread is the thing that keeps a
    process from exiting, so the thread is what this asks about."""
    if getattr(conn, "ident", None) is None:
        return False                      # never started: there is no worker
    return bool(conn.is_alive())


# Marks a connection whose close() ran to completion. Recorded from the close
# itself rather than inferred from the wrapper's flags: a CANCELLED connect
# also clears `_running` and `_connection` while its worker is still blocked
# inside sqlite3.connect, and that thread is a real orphan. Stamped on the
# object, not into a set of ids — a collected connection's id is reused, and a
# recycled id would hand a brand-new orphan someone else's clean bill.
_CLOSE_DONE = "_glassops_test_close_completed"


def _instrument_close(conn):
    """Record that THIS connection's close finished, so a worker thread that
    has not quite exited yet can be told from one that never will."""
    real_close = conn.close

    async def close():
        result = await real_close()      # only a close that RETURNED counts
        setattr(conn, _CLOSE_DONE, True)
        return result

    conn.close = close


def _close_completed(conn) -> bool:
    """True only for a connection whose close was submitted AND completed, and
    whose wrapper has already given up its raw sqlite handle.

    All three, because no one of them is enough. aiosqlite's close() clears
    `_running` and `_connection` in a `finally`, and a cancelled CONNECT clears
    exactly the same two — so the flags alone would hand a genuinely orphaned
    connector thread the same grace as a clean shutdown."""
    return (getattr(conn, _CLOSE_DONE, False) is True
            and getattr(conn, "_running", True) is False
            and getattr(conn, "_connection", True) is None)


# aiosqlite 0.21.0, measured 30/30: `is_alive()` is still True the instant
# `await conn.close()` returns, and False after a single `await sleep(0)` —
# the worker has been handed its stop sentinel and has not been scheduled yet.
# So a connection a test closed CORRECTLY reads as a live thread, and the
# orphan gate would fail the test that did the right thing.
#
# This is a confirmation, NOT a grace period: it is spent only on connections
# whose close is already known to have completed, and any other live worker is
# reported immediately. Giving every live worker a sleep would hide exactly the
# leak this gate exists to catch.
_SETTLE_POLL = 0.005
_SETTLE_BOUND = 0.25


async def unaccounted_live_workers(db, created):
    """Live aiosqlite workers that NO app.database registry knows about.

    Deliberately driven by a registry the test suite keeps itself: a leak the
    product forgot to record is exactly the leak worth catching, and asking the
    product whether it leaked would answer with the same blind spot."""
    accounted = {id(o) for o in (db._conn, db._metric_conn) if o is not None}
    accounted |= {id(o) for o in db._unclosed}
    accounted |= {id(e.obj) for e in db._late_results}
    # A late CURSOR's `obj` is the cursor; the thing that owns a worker thread
    # is its `owner` connection. Counting only `obj` leaves that connection
    # named by no registry the gate consults, so a correctly tracked late
    # result was reported as a connection nobody remembered.
    accounted |= {id(e.owner) for e in db._late_results if e.owner is not None}
    accounted |= {id(e.owner) for e in db._unresolved
                  if getattr(e, "owner", None) is not None}
    suspects = [c for c in created if worker_alive(c) and id(c) not in accounted]
    if not suspects:
        return []
    orphans = [c for c in suspects if not _close_completed(c)]
    settling = [c for c in suspects if _close_completed(c)]
    if settling:
        deadline = time.monotonic() + _SETTLE_BOUND
        while any(worker_alive(c) for c in settling):
            if time.monotonic() >= deadline:
                break
            await asyncio.sleep(_SETTLE_POLL)
        orphans += [c for c in settling if worker_alive(c)]
    return orphans


@pytest.fixture(autouse=True)
async def reset_db_module_state(request, monkeypatch):
    """Reset app.database's process-global state around every test, and keep an
    INDEPENDENT record of every aiosqlite connection the test created.

    Two things are global by design and would otherwise leak between tests:
    the single DB-file operation lock (an asyncio.Lock binds to the loop that
    first acquires it, and pytest-asyncio gives each test its own loop), and
    the admission latches — fail-stop, closing/closed and the unresolved-worker
    registry — which are deliberately sticky for the life of a real process.
    Without this, one test that exercises any of them wedges every test after it.
    """
    import app.database as db

    # `monkeypatch` is requested BOTH for its finalisation order and to install
    # the tracking wrapper below. Every db fixture publishes through
    # monkeypatch.setattr(db, "_conn", ...), and pytest finalises a fixture
    # before the fixtures it depends on — so without this, monkeypatch.undo()
    # has already restored _conn to None by the time the teardown below reads
    # it, and a shutdown that left a live published connection behind is
    # invisible.
    _reset(db)

    # Every aiosqlite.Connection built during this test, recorded here rather
    # than read back out of app.database: the failure this guards against is a
    # connection the product no longer has a reference to.
    created: list = []
    real_connect = aiosqlite.connect

    def tracking_connect(*args, **kwargs):
        conn = real_connect(*args, **kwargs)
        created.append(conn)
        _instrument_close(conn)
        return conn

    # Through monkeypatch, and NEVER restored by hand. A test that patches
    # aiosqlite.connect itself uses the SAME monkeypatch fixture, which records
    # `tracking_connect` as the value to put back. Assigning the original here
    # and letting undo() run afterwards restored the wrapper instead — so the
    # next test inherited a closure over a dead `created` list, connections it
    # built were recorded nowhere, and aiosqlite.connect never came back.
    monkeypatch.setattr(aiosqlite, "connect", tracking_connect)
    yield

    # ── 1. OBSERVE first. ──
    # A teardown that force-closes connections and empties the registries would
    # make a genuine production leak look GREEN. So record what the product left
    # behind BEFORE touching anything, and expose it to the test that asked for
    # it via `expected_db_leftovers`.
    leftover_unclosed = list(db._unclosed)
    leftover_unresolved = list(db._unresolved)
    leftover_late = list(db._late_results)
    still_open = [c for c in (db._conn, db._metric_conn) if c is not None]
    # A connection the PRODUCT published, still running. fresh_db (and every
    # other db fixture) closes the database before this autouse teardown runs,
    # so a live _conn/_metric_conn at this moment is not a fixture still in
    # flight — it is a shutdown that left a connection behind. Observed here,
    # before anything is cleaned up.
    published_live = [c for c in still_open if worker_alive(c)]
    orphan_workers = await unaccounted_live_workers(db, created)
    expected = getattr(request.node, "_expected_db_leftovers", None)

    # ── 2. Only then clean up the TEST PROCESS. ──
    # Production deliberately leaves connections open on a restart-required
    # verdict (the process is going away); a test has no process to end, so its
    # non-daemon aiosqlite worker would otherwise outlive the run.
    for entry in leftover_unresolved:
        entry.task.cancel()
    if leftover_unresolved:
        # A submission that already finished cannot be cancelled, and a connect
        # that finished late hands back a LIVE aiosqlite connection whose worker
        # is a non-daemon thread. Left running it would block interpreter exit
        # and hang the whole session.
        await asyncio.gather(*(e.task for e in leftover_unresolved),
                             return_exceptions=True)
    for conn in [*still_open, *leftover_unclosed,
                 *(e.obj for e in leftover_late), *created]:
        if not isinstance(conn, aiosqlite.Connection):
            continue
        try:
            await asyncio.wait_for(conn.close(), timeout=5)
        except BaseException:
            pass
    db._conn = None
    db._metric_conn = None
    _reset(db)

    # ── 3. Report the observation. ──
    # Marker-independent failures first: `expect_db_leftovers` speaks for
    # resources the product is TRACKING as restart-required, and neither of
    # these is one of those.
    # Both are collected and reported TOGETHER: they are different failures
    # with different fixes, and raising on the first would swallow the second.
    live_worker_faults = []
    if published_live:
        live_worker_faults.append(
            f"{len(published_live)} connection(s) the database had published "
            "were still running at teardown — the database is closed before "
            "this runs, so a live _conn/_metric_conn here is a shutdown that "
            "left a connection behind, not a fixture still in flight: "
            f"{[getattr(c, 'name', c) for c in published_live]}"
        )
    if orphan_workers:
        live_worker_faults.append(
            f"{len(orphan_workers)} aiosqlite worker thread(s) were still "
            "running that no app.database registry accounts for — a connection "
            "was created and then dropped without being closed or recorded: "
            f"{[getattr(c, 'name', c) for c in orphan_workers]}"
        )
    if live_worker_faults:
        # Deliberately NOT excused by `expect_db_leftovers`. That marker says
        # "this test leaves a resource the product is TRACKING" — an unclosed
        # connection, an unresolved submission, a late result — which is a
        # restart-required state the product can report. A live worker it
        # forgot, or one it published and never closed, is a different failure
        # and nothing can report it.
        raise AssertionError("; ".join(live_worker_faults))

    leftovers = len(leftover_unclosed) + len(leftover_unresolved) + len(leftover_late)
    if expected is None and leftovers:
        raise AssertionError(
            "the database was left in a restart-required state and the test did "
            "not declare it: "
            f"unclosed={len(leftover_unclosed)} unresolved={len(leftover_unresolved)} "
            f"late_results={len(leftover_late)}. "
            "Use the `expect_db_leftovers` marker if that is the behaviour under test."
        )
    if expected is not None and not leftovers:
        # The marker suppresses the check above, so a stale one silently
        # pre-authorises a leak the test no longer produces. Requiring a REAL
        # leftover keeps the marker evidence of observed behaviour rather than
        # a blanket exemption.
        raise AssertionError(
            "this test carries the `expect_db_leftovers` marker but left the "
            "database clean (unclosed=0 unresolved=0 late_results=0). Remove the "
            "marker: it must document a restart-required state that actually "
            "happened, not pre-authorise one that never does."
        )


def _reset(db):
    db._op_lock = asyncio.Lock()
    db._fail_stop = None
    db._closing = False
    db._closed = False
    db._unresolved.clear()
    db._unclosed.clear()
    db._late_results.clear()
    # A fresh process has never built a connection. Leaving earlier tests'
    # entries in place would let one test's leak decide another test's verdict.
    db._connections.clear()


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "expect_db_leftovers: this test deliberately leaves the database in a "
        "restart-required state the product is TRACKING (unclosed connections, "
        "unresolved worker submissions, late results); the teardown records it "
        "instead of failing. It never excuses a worker thread no app.database "
        "registry accounts for, nor a published connection left running.",
    )


def pytest_runtest_setup(item):
    if item.get_closest_marker("expect_db_leftovers"):
        item._expected_db_leftovers = True
