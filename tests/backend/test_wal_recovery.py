"""Rehearsal suite for the WAL-only-commit recovery procedure (CP-5A).

This is a LOCAL, DISPOSABLE rehearsal. Every database it touches is built
inside pytest's own `tmp_path` by this file, killed with SIGKILL by this file,
and thrown away by pytest. Nothing here reaches dev9, `/app/data`, a Docker
volume, or the repository's `data/`, and passing it is NOT an approval to run
the procedure against a production database.

What is under test: `app.wal_recovery`, a standalone primitive that takes a
SQLite database whose last commits exist only in its `-wal` sidecar — the state
a hard kill leaves behind — and folds those commits into the main database file
WITHOUT losing them and WITHOUT destroying either the original or the backup if
any step fails.

The oracle this suite is built on, and why it needs its own fixture:

    A checkpoint that "succeeds" on a database with an empty WAL proves
    nothing. To prove data was *rescued*, the fixture must first be shown to
    hold a commit that is genuinely absent from the main database file and
    present only in the WAL. `_build_fixture` produces that by committing from
    a child process that is SIGKILLed with its connection still open, and
    `assert_wal_only_commit` measures it: the same commit read back from a
    db-only copy (absent) and from a db+wal copy (present). A fixture built by
    closing the connection normally is checkpointed by that close — measured,
    not assumed — so it carries no WAL-only commit at all, and this suite
    requires the oracle to REJECT it as `ORACLE_INVALID` rather than let it
    stand in as a passing case.
"""

import ast
import contextlib
import dataclasses
import hashlib
import json
import shutil
import itertools
import os
import signal
import sqlite3
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

import app.wal_fence as wal_fence
import app.wal_recovery as wal_recovery
from tests.backend.conftest import (
    AGENT_ID,
    AGENT_SERVICE,
    BACKEND_ID,
    DATA_DESTINATION,
    PROJECT,
    SERVICE,
    FakeContainer,
    FakeDocker,
    ACK_KEY,
    HOST_ID,
    ScriptedWitness,
    bind_mount,
    external_ack,
)

MODULE_PY = Path(wal_recovery.__file__)

SENTINEL = wal_recovery.Sentinel(
    table="events", column="name", value="wal-only-sentinel-cp5a"
)
PROBES = (wal_recovery.Probe(table="events", id_column="id", timestamp_column="ts"),)

BASELINE_ROWS = 200
CRASH_ROWS = 50


# ── the hard-crash fixture ───────────────────────────
#
# The child commits and then blocks forever. The parent SIGKILLs it. Both the
# crash and the clean-close variants are killed the same way; the ONLY
# difference is whether `conn.close()` ran first, so the oracle's verdict is
# attributable to the close and to nothing else.
#
# `synchronous=FULL` so the commit is fsynced into the WAL before the kill, and
# `wal_autocheckpoint=0` so nothing folds it into the main file behind our back.

_CHILD_SOURCE = r'''
import sqlite3, sys, time

db_path, sentinel, extra, mode = sys.argv[1], sys.argv[2], int(sys.argv[3]), sys.argv[4]
conn = sqlite3.connect(db_path, isolation_level=None)
conn.execute("PRAGMA wal_autocheckpoint=0")
conn.execute("PRAGMA synchronous=FULL")
conn.execute("BEGIN IMMEDIATE")
conn.execute("INSERT INTO events(name, ts) VALUES (?, ?)", (sentinel, 9000000.0))
for i in range(extra):
    conn.execute("INSERT INTO events(name, ts) VALUES (?, ?)",
                 ("crash-%d" % i, 8000000.0 + i))
conn.execute("COMMIT")
if mode == "checkpointed":
    # sentinel folded into the main file, then a second commit left in the WAL
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.execute("BEGIN IMMEDIATE")
    for i in range(extra):
        conn.execute("INSERT INTO events(name, ts) VALUES (?, ?)",
                     ("after-checkpoint-%d" % i, 7000000.0 + i))
    conn.execute("COMMIT")
if mode == "clean":
    conn.close()
sys.stdout.write("COMMITTED\n")
sys.stdout.flush()
time.sleep(600)
'''


@dataclass(frozen=True)
class Fixture:
    db: Path
    wal: Path
    child_returncode: int
    total_rows: int
    max_id: int
    max_ts: float


@pytest.fixture
def children():
    """Every child this test spawned, checked for leaks at teardown."""
    procs = []
    yield procs
    leaked = [p for p in procs if p.poll() is None]
    for proc in leaked:
        proc.kill()
        proc.wait(timeout=10)
    assert not leaked, f"{len(leaked)} fixture child process(es) outlived the test"


def _build_fixture(root: Path, children, *, mode: str = "crash") -> Fixture:
    db = root / "app.db"
    conn = sqlite3.connect(db, isolation_level=None)
    try:
        journal = conn.execute("PRAGMA journal_mode=WAL").fetchone()[0]
        assert journal == "wal", f"fixture is not in WAL mode: {journal!r}"
        conn.execute("PRAGMA wal_autocheckpoint=0")
        conn.execute(
            "CREATE TABLE events("
            "id INTEGER PRIMARY KEY, name TEXT NOT NULL, ts REAL NOT NULL)"
        )
        # Deliberately outside PROBES. A late write here must still invalidate
        # the byte-identical backup even though the semantic readback would not
        # notice it.
        conn.execute(
            "CREATE TABLE audit(id INTEGER PRIMARY KEY, note TEXT NOT NULL)"
        )
        for i in range(BASELINE_ROWS):
            conn.execute(
                "INSERT INTO events(name, ts) VALUES (?, ?)", (f"baseline-{i}", float(i))
            )
        # Baseline into the main file, so anything still in the WAL afterwards
        # is unambiguously the child's commit.
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        conn.close()

    proc = subprocess.Popen(
        [
            sys.executable,
            "-c",
            _CHILD_SOURCE,
            str(db),
            SENTINEL.value,
            str(CRASH_ROWS),
            mode,
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    children.append(proc)
    try:
        handshake = proc.stdout.readline()
        assert handshake.strip() == "COMMITTED", (
            f"fixture child never confirmed its commit: {handshake!r}"
        )
        proc.kill()
        returncode = proc.wait(timeout=30)
    finally:
        proc.stdout.close()

    assert returncode == -signal.SIGKILL, (
        f"fixture child was not SIGKILLed (returncode={returncode})"
    )
    extra = CRASH_ROWS if mode == "checkpointed" else 0
    return Fixture(
        db=db,
        wal=Path(f"{db}-wal"),
        child_returncode=returncode,
        total_rows=BASELINE_ROWS + 1 + CRASH_ROWS + extra,
        max_id=BASELINE_ROWS + 1 + CRASH_ROWS + extra,
        max_ts=9000000.0,
    )


@pytest.fixture
def crashed(tmp_path, children):
    source = tmp_path / "source"
    source.mkdir()
    return _build_fixture(source, children)


def _sha256(path: Path) -> str | None:
    if not path.exists():
        return None
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _source_digests(fixture: Fixture) -> tuple[str | None, str | None]:
    return _sha256(fixture.db), _sha256(fixture.wal)


class NeverAsked(wal_recovery.SourceApplyAuthority):
    """A fence that fails the test if anything ever asks it to authorise.

    The runs below must refuse before the source is even a question. Reaching
    the fence would mean the refusal came too late to be the one under test.
    """

    failures: list = []

    def check_before_source_open(self, *, db_path):  # pragma: no cover
        raise AssertionError(
            f"this run should have been refused long before the fence was asked "
            f"about {db_path!r}"
        )

    def begin_apply(self):  # pragma: no cover
        raise AssertionError("nothing here should have begun an apply")

    def complete(self):  # pragma: no cover
        raise AssertionError("nothing here should have reached a completion")

    def fail(self, reason):
        # Recorded, not refused. Every failed run now closes out its authority
        # on the way past, and that is not the same as being asked to
        # authorise anything — which is what these runs must never do.
        self.failures.append(reason)


NEVER_ASKED = NeverAsked()


class FakeClock:
    def __init__(self, now=1_700_000_000.0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def _fake_host(fixture: Fixture) -> FakeDocker:
    """A scripted Compose project whose data mount is the fixture's directory."""
    mount = bind_mount(fixture.db.parent, destination=DATA_DESTINATION)
    return FakeDocker(
        [
            FakeContainer(container_id=BACKEND_ID, service=SERVICE, mounts=[mount]),
            FakeContainer(container_id=AGENT_ID, service=AGENT_SERVICE, mounts=[mount]),
        ]
    )


_QUIESCE_SEQUENCE = itertools.count()


def _quiesce(fixture: Fixture, tmp_path: Path, *, run_id="run-1", host=None, clock=None):
    """Fence the scripted host over the crash fixture, exactly as CP-5B does.

    A real `ManifestFence` over a real manifest, rather than a stand-in: the
    point of the wiring is that CP-5A refuses to act on anything less.
    """
    host = host if host is not None else _fake_host(fixture)
    clock = clock if clock is not None else FakeClock()
    manifest = wal_fence.fence(
        project=PROJECT,
        service=SERVICE,
        container_id=BACKEND_ID,
        scope=(
            wal_fence.ScopedContainer(service=AGENT_SERVICE, container_id=AGENT_ID),
            wal_fence.ScopedContainer(service=SERVICE, container_id=BACKEND_ID),
        ),
        data_destination=DATA_DESTINATION,
        db_relpath=fixture.db.name,
        host_id=HOST_ID,
        authority_verifier=ACK_KEY,
        visibility_witness=ScriptedWitness(host),
        external_authority_ack=external_ack(
            container_ids=[AGENT_ID, BACKEND_ID], data_dir=fixture.db.parent
        ),
        # A fresh manifest per quiesce: a lease is never reused, and a retry
        # of the same run id is still a new fence.
        manifest_path=str(
            tmp_path / "fence" / f"{run_id}-{next(_QUIESCE_SEQUENCE)}.json"
        ),
        runner=host,
        clock=clock,
    )
    guard = wal_fence.ManifestFence(
        manifest,
        runner=host,
        clock=clock,
        authority_verifier=ACK_KEY,
        visibility_witness=ScriptedWitness(host),
    )
    return guard.claim(), host, clock


def _rehearse(fixture: Fixture, tmp_path: Path, *, run_id="run-1", **kwargs):
    backups = tmp_path / "backups"
    backups.mkdir(exist_ok=True)
    if "fence" not in kwargs:
        kwargs["fence"] = _quiesce(fixture, tmp_path, run_id=run_id)[0]
    kwargs.setdefault("sentinel", SENTINEL)
    kwargs.setdefault("probes", PROBES)
    return wal_recovery.rehearse_wal_recovery(
        db_path=fixture.db,
        backup_root=backups,
        run_id=run_id,
        **kwargs,
    )


# ── 1. the oracle: is this fixture actually a WAL-only commit? ───

def test_hard_crash_fixture_keeps_the_commit_only_in_the_wal(crashed, tmp_path):
    evidence = wal_recovery.assert_wal_only_commit(
        db_path=crashed.db, sentinel=SENTINEL, workspace=tmp_path / "oracle"
    )

    assert evidence.db.size_bytes > 0
    assert evidence.wal.size_bytes > 0, "the crash left no WAL to recover from"
    # The commit is absent from the main file and present once the WAL is
    # carried along with it — that gap IS the thing recovery has to close.
    assert evidence.db_only_sentinel_rows == 0
    assert evidence.db_plus_wal_sentinel_rows == 1
    assert evidence.db_only_row_count == BASELINE_ROWS
    assert evidence.db_plus_wal_row_count == crashed.total_rows


def test_clean_close_fixture_is_refused_as_an_invalid_oracle(tmp_path, children):
    source = tmp_path / "source"
    source.mkdir()
    clean = _build_fixture(source, children, mode="clean")

    with pytest.raises(wal_recovery.OracleInvalid) as caught:
        wal_recovery.assert_wal_only_commit(
            db_path=clean.db, sentinel=SENTINEL, workspace=tmp_path / "oracle"
        )

    assert caught.value.code == "ORACLE_INVALID"
    assert caught.value.reason == "no-wal"
    # Closing the connection checkpointed the commit into the main file, so
    # there is no WAL-only commit here and this fixture may not be counted as
    # a passing recovery.
    assert caught.value.evidence.db_only_sentinel_rows == 1
    assert caught.value.evidence.wal.size_bytes == 0


def test_a_wal_that_postdates_the_sentinel_is_refused(tmp_path, children):
    """A non-empty WAL is not the same thing as a WAL-only commit.

    Here the child checkpointed the sentinel into the database file and then
    committed more rows, so the WAL is large and busy and contains none of
    what recovery would be asked to rescue. Accepting this would let a
    checkpoint that rescued nothing be reported as a success.
    """
    source = tmp_path / "source"
    source.mkdir()
    fixture = _build_fixture(source, children, mode="checkpointed")
    assert fixture.wal.stat().st_size > 0

    with pytest.raises(wal_recovery.OracleInvalid) as caught:
        wal_recovery.assert_wal_only_commit(
            db_path=fixture.db, sentinel=SENTINEL, workspace=tmp_path / "oracle"
        )

    assert caught.value.reason == "already-checkpointed"
    assert caught.value.evidence.wal.size_bytes > 0
    assert caught.value.evidence.db_only_sentinel_rows == 1


def test_recovery_entrypoint_rejects_a_clean_close_before_touching_source(
    tmp_path, children, monkeypatch
):
    """The CP-5A entrypoint refuses a source with no WAL before asking anyone.

    Whether a clean close leaves an empty `-wal` sidecar behind or removes it
    is the OS's choice (macOS keeps it, Linux deletes it), so the absent shape
    is made explicit here rather than inherited from the platform. That also
    keeps the real CP-5B fence out of the picture — it needs a sidecar to
    open — because what is under test is the recovery oracle, not the probe.
    """
    source = tmp_path / "source"
    source.mkdir()
    clean = _build_fixture(source, children, mode="clean")
    clean.wal.unlink(missing_ok=True)
    assert not clean.wal.exists()
    before = _source_digests(clean)

    def checkpoint_must_not_run(conn):
        pytest.fail("checkpoint ran before the WAL-only oracle accepted the source")

    monkeypatch.setattr(wal_recovery, "_checkpoint_pragma", checkpoint_must_not_run)

    class CountingNeverAsked(NeverAsked):
        source_open_checks = 0

        def check_before_source_open(self, *, db_path):
            self.source_open_checks += 1
            return super().check_before_source_open(db_path=db_path)

    authority = CountingNeverAsked()

    with pytest.raises(wal_recovery.OracleInvalid) as caught:
        _rehearse(clean, tmp_path, fence=authority)

    assert caught.value.reason == "no-wal"
    assert caught.value.report.checkpoint_started is False
    assert _source_digests(clean) == before
    assert authority.source_open_checks == 0


def test_recovery_entrypoint_rejects_a_wal_that_does_not_hold_the_sentinel(
    tmp_path, children, monkeypatch
):
    source = tmp_path / "source"
    source.mkdir()
    fixture = _build_fixture(source, children, mode="checkpointed")
    before = _source_digests(fixture)

    def checkpoint_must_not_run(conn):
        pytest.fail("checkpoint ran before the WAL-only oracle accepted the source")

    monkeypatch.setattr(wal_recovery, "_checkpoint_pragma", checkpoint_must_not_run)

    with pytest.raises(wal_recovery.OracleInvalid) as caught:
        _rehearse(fixture, tmp_path)

    assert caught.value.reason == "already-checkpointed"
    assert caught.value.evidence.wal.size_bytes > 0
    assert caught.value.report.checkpoint_started is False
    assert _source_digests(fixture) == before


# ── 2. input validation ──────────────────────────────

def test_missing_database_is_refused_and_creates_no_database(tmp_path):
    missing = tmp_path / "source" / "app.db"
    missing.parent.mkdir()

    with pytest.raises(wal_recovery.SourceRejected) as caught:
        wal_recovery.rehearse_wal_recovery(
            db_path=missing,
            backup_root=tmp_path,
            run_id="run-1",
            sentinel=SENTINEL,
            probes=PROBES,
            fence=NEVER_ASKED,
        )

    assert caught.value.code == "SOURCE_REJECTED"
    # SQLite happily conjures a database out of nothing; this procedure must
    # never take that path, so nothing at all may appear on disk.
    assert list(missing.parent.iterdir()) == []


def test_relative_database_path_is_refused(crashed, tmp_path, monkeypatch):
    # Deliberately a relative path that DOES resolve: what makes it
    # unacceptable is that its meaning moves with the working directory, not
    # that it happens to be missing.
    monkeypatch.chdir(crashed.db.parent)

    with pytest.raises(wal_recovery.SourceRejected) as caught:
        wal_recovery.rehearse_wal_recovery(
            db_path=Path(crashed.db.name),
            backup_root=tmp_path,
            run_id="run-1",
            sentinel=SENTINEL,
            probes=PROBES,
            fence=NEVER_ASKED,
        )

    assert "absolute" in str(caught.value)


def test_a_database_path_that_is_not_a_regular_file_is_refused(tmp_path):
    directory = tmp_path / "app.db"
    directory.mkdir()

    with pytest.raises(wal_recovery.SourceRejected) as caught:
        wal_recovery.rehearse_wal_recovery(
            db_path=directory,
            backup_root=tmp_path,
            run_id="run-1",
            sentinel=SENTINEL,
            probes=PROBES,
            fence=NEVER_ASKED,
        )

    assert "regular file" in str(caught.value)


def test_symlinked_database_is_refused_and_leaves_the_target_untouched(
    crashed, tmp_path
):
    before = _source_digests(crashed)
    link = tmp_path / "link.db"
    link.symlink_to(crashed.db)

    with pytest.raises(wal_recovery.SourceRejected) as caught:
        wal_recovery.rehearse_wal_recovery(
            db_path=link,
            backup_root=tmp_path / "backups",
            run_id="run-1",
            sentinel=SENTINEL,
            probes=PROBES,
            fence=NEVER_ASKED,
        )

    assert caught.value.code == "SOURCE_REJECTED"
    assert _source_digests(crashed) == before


def test_symlinked_wal_is_refused_and_leaves_the_target_untouched(crashed, tmp_path):
    # Move the real WAL aside and leave a symlink in its place: following it
    # would checkpoint into a file the operator never named.
    elsewhere = tmp_path / "elsewhere-wal"
    shutil.move(str(crashed.wal), str(elsewhere))
    crashed.wal.symlink_to(elsewhere)
    db_before, wal_before = _sha256(crashed.db), _sha256(elsewhere)

    backups = tmp_path / "backups"
    backups.mkdir()
    with pytest.raises(wal_recovery.SourceRejected) as caught:
        wal_recovery.rehearse_wal_recovery(
            db_path=crashed.db,
            backup_root=backups,
            run_id="run-1",
            sentinel=SENTINEL,
            probes=PROBES,
            fence=NEVER_ASKED,
        )

    assert caught.value.code == "SOURCE_REJECTED"
    assert _sha256(crashed.db) == db_before
    assert _sha256(elsewhere) == wal_before
    assert list(backups.iterdir()) == []


def test_a_wal_path_that_is_not_a_regular_file_is_refused(crashed, tmp_path):
    aside = tmp_path / "aside-wal"
    shutil.move(str(crashed.wal), str(aside))
    crashed.wal.mkdir()

    with pytest.raises(wal_recovery.SourceRejected) as caught:
        wal_recovery.rehearse_wal_recovery(
            db_path=crashed.db,
            backup_root=tmp_path,
            run_id="run-1",
            sentinel=SENTINEL,
            probes=PROBES,
            fence=NEVER_ASKED,
        )

    assert "regular file" in str(caught.value)


def test_a_symlinked_backup_root_is_refused(crashed, tmp_path):
    real = tmp_path / "real-backups"
    real.mkdir()
    link = tmp_path / "backups"
    link.symlink_to(real)

    with pytest.raises(wal_recovery.BackupRejected) as caught:
        wal_recovery.rehearse_wal_recovery(
            db_path=crashed.db,
            backup_root=link,
            run_id="run-1",
            sentinel=SENTINEL,
            probes=PROBES,
            fence=NEVER_ASKED,
        )

    assert caught.value.code == "BACKUP_REJECTED"
    assert list(real.iterdir()) == []


def test_a_run_id_that_escapes_the_backup_root_is_refused(crashed, tmp_path):
    backups = tmp_path / "backups"
    backups.mkdir()

    # The run id becomes a path component. Anything that can steer that
    # component somewhere else is refused outright rather than normalised.
    for bad in ("../escape", "nested/run", "..", ".", "", "-leading"):
        with pytest.raises(wal_recovery.BackupRejected):
            wal_recovery.rehearse_wal_recovery(
                db_path=crashed.db,
                backup_root=backups,
                run_id=bad,
                sentinel=SENTINEL,
                probes=PROBES,
                fence=NEVER_ASKED,
            )

    assert list(backups.iterdir()) == []
    assert not (tmp_path / "escape").exists()


def test_run_directory_that_already_exists_is_refused(crashed, tmp_path):
    backups = tmp_path / "backups"
    (backups / "run-1").mkdir(parents=True)
    (backups / "run-1" / "keep.txt").write_text("earlier run")
    before = _source_digests(crashed)

    with pytest.raises(wal_recovery.BackupRejected) as caught:
        wal_recovery.rehearse_wal_recovery(
            db_path=crashed.db,
            backup_root=backups,
            run_id="run-1",
            sentinel=SENTINEL,
            probes=PROBES,
            fence=NEVER_ASKED,
        )

    assert caught.value.code == "BACKUP_REJECTED"
    assert (backups / "run-1" / "keep.txt").read_text() == "earlier run"
    assert _source_digests(crashed) == before


# ── 3. space preflight ───────────────────────────────

def test_space_plan_is_itemised_from_measured_bytes(crashed, tmp_path):
    backups = tmp_path / "backups"
    backups.mkdir()

    plan = wal_recovery.plan_space(db_path=crashed.db, backup_root=backups)

    assert plan.db_bytes == crashed.db.stat().st_size
    assert plan.wal_bytes == crashed.wal.stat().st_size
    assert plan.source_free_bytes > 0
    assert plan.backup_free_bytes > 0
    # Every item is one measured file size, never a multiplier or a percentage
    # of anything, and the totals are exactly the sum of the named items.
    assert set(plan.items) == {
        "snapshot_db",
        "snapshot_wal",
        "restore_db",
        "restore_wal",
        "restore_checkpoint_db_growth",
        "source_checkpoint_db_growth",
    }
    assert set(plan.items.values()) <= {plan.db_bytes, plan.wal_bytes}
    assert plan.backup_required_bytes == sum(
        plan.items[k] for k in plan.items if k != "source_checkpoint_db_growth"
    )
    assert plan.source_required_bytes == plan.items["source_checkpoint_db_growth"]
    assert plan.sufficient is True


def test_insufficient_space_refuses_before_any_copy_or_checkpoint(
    crashed, tmp_path, monkeypatch
):
    before = _source_digests(crashed)
    backups = tmp_path / "backups"
    backups.mkdir()
    monkeypatch.setattr(wal_recovery, "_free_bytes", lambda path: 4096)

    with pytest.raises(wal_recovery.InsufficientSpace) as caught:
        wal_recovery.rehearse_wal_recovery(
            db_path=crashed.db,
            backup_root=backups,
            run_id="run-1",
            sentinel=SENTINEL,
            probes=PROBES,
            fence=NEVER_ASKED,
        )

    assert caught.value.code == "INSUFFICIENT_SPACE"
    assert caught.value.report.space.sufficient is False
    assert caught.value.report.checkpoint_started is False
    # Neither the copy nor the checkpoint may be attempted "to see how far it
    # gets": a half-written backup on a full disk is worse than no backup.
    assert list(backups.iterdir()) == []
    assert _source_digests(crashed) == before


def test_free_space_that_covers_only_the_source_half_is_refused(
    crashed, tmp_path, monkeypatch
):
    """A shared filesystem cannot spend the same free bytes twice.

    Here there is room for the checkpoint beside the source, and there would
    be room for the backup — but not for both, and both come out of one pool.
    Approving this is how a recovery gets halfway and strands the operator
    with a truncated backup and a database in mid-checkpoint.
    """
    backups = tmp_path / "backups"
    backups.mkdir()
    plan = wal_recovery.plan_space(db_path=crashed.db, backup_root=backups)
    assert plan.same_filesystem is True
    monkeypatch.setattr(
        wal_recovery, "_free_bytes", lambda path: plan.pooled_required_bytes - 1
    )

    with pytest.raises(wal_recovery.InsufficientSpace) as caught:
        wal_recovery.rehearse_wal_recovery(
            db_path=crashed.db,
            backup_root=backups,
            run_id="run-1",
            sentinel=SENTINEL,
            probes=PROBES,
            fence=NEVER_ASKED,
        )

    assert caught.value.report.space.shortfall_bytes == 1
    assert list(backups.iterdir()) == []


def test_space_requirements_are_pooled_when_backup_shares_the_source_filesystem(
    crashed, tmp_path
):
    backups = tmp_path / "backups"
    backups.mkdir()

    plan = wal_recovery.plan_space(db_path=crashed.db, backup_root=backups)

    assert plan.same_filesystem is True
    assert plan.pooled_required_bytes == (
        plan.backup_required_bytes + plan.source_required_bytes
    )


# ── 4. the snapshot must be provably good before anything is touched ───

def test_a_corrupted_snapshot_copy_stops_before_any_checkpoint(
    crashed, tmp_path, monkeypatch
):
    before = _source_digests(crashed)
    real_copy = wal_recovery._copy_file

    def truncating_copy(src, dst):
        real_copy(src, dst)
        if str(dst).endswith("-wal"):
            with open(dst, "r+b") as handle:
                handle.truncate(os.path.getsize(dst) - 1)

    monkeypatch.setattr(wal_recovery, "_copy_file", truncating_copy)

    with pytest.raises(wal_recovery.SnapshotFailed) as caught:
        _rehearse(crashed, tmp_path)

    assert caught.value.code == "SNAPSHOT_FAILED"
    assert caught.value.report.checkpoint_started is False
    assert _source_digests(crashed) == before


def test_a_corrupted_snapshot_database_stops_before_any_checkpoint(
    crashed, tmp_path, monkeypatch
):
    before = _source_digests(crashed)
    real_copy = wal_recovery._copy_file

    def corrupting_copy(src, dst):
        real_copy(src, dst)
        if not str(dst).endswith("-wal"):
            with open(dst, "r+b") as handle:
                handle.seek(os.path.getsize(dst) - 1)
                handle.write(b"\x00")

    monkeypatch.setattr(wal_recovery, "_copy_file", corrupting_copy)

    with pytest.raises(wal_recovery.SnapshotFailed) as caught:
        _rehearse(crashed, tmp_path)

    assert caught.value.stage == "snapshot-verify"
    assert caught.value.report.checkpoint_started is False
    assert _source_digests(crashed) == before


def test_a_restore_copy_that_is_not_byte_identical_stops_before_checkpoint(
    crashed, tmp_path, monkeypatch
):
    before = _source_digests(crashed)
    real_copy = wal_recovery._copy_file
    checkpoint_calls = []

    def corrupt_restore_wal(src, dst):
        real_copy(src, dst)
        if "/snapshot/" in str(src) and str(dst).endswith("-wal"):
            with open(dst, "ab") as handle:
                handle.write(b"\x00" * 8)

    def record_checkpoint(conn):
        checkpoint_calls.append(conn)
        return (0, 0, 0)

    monkeypatch.setattr(wal_recovery, "_copy_file", corrupt_restore_wal)
    monkeypatch.setattr(wal_recovery, "_checkpoint_pragma", record_checkpoint)

    with pytest.raises(wal_recovery.SnapshotFailed) as caught:
        _rehearse(crashed, tmp_path)

    assert caught.value.stage == "restore-copy-verify"
    assert caught.value.report.checkpoint_started is False
    assert checkpoint_calls == []
    assert _source_digests(crashed) == before


def test_the_snapshot_is_flushed_to_disk_before_it_is_trusted(
    crashed, tmp_path, monkeypatch
):
    files, dirs = [], []
    real_file, real_dir = wal_recovery._fsync_file, wal_recovery._fsync_dir

    def record_file(path):
        files.append(path)
        real_file(path)

    def record_dir(path):
        dirs.append(path)
        real_dir(path)

    monkeypatch.setattr(wal_recovery, "_fsync_file", record_file)
    monkeypatch.setattr(wal_recovery, "_fsync_dir", record_dir)

    report = _rehearse(crashed, tmp_path)

    # Contents AND directory entries: a backup whose name never reached the
    # disk is not a backup, however durable its bytes are.
    assert report.snapshot_db.path in files
    assert report.snapshot_wal.path in files
    assert os.path.dirname(report.snapshot_db.path) in dirs
    assert report.run_dir in dirs
    assert report.backup_root in dirs


def test_a_failed_backup_root_fsync_stops_before_any_checkpoint(
    crashed, tmp_path, monkeypatch
):
    before = _source_digests(crashed)
    backup_root = tmp_path / "backups"
    real_fsync_dir = wal_recovery._fsync_dir
    real_connect = wal_recovery._connect_rw
    checkpoint_calls = []
    opened = []

    def fail_on_backup_root(path):
        if os.path.samefile(path, backup_root):
            raise OSError(5, "Input/output error")
        real_fsync_dir(path)

    def record_checkpoint(conn):
        checkpoint_calls.append(conn)
        return (0, 0, 0)

    def record_connect(path, busy_timeout_ms):
        opened.append(path)
        return real_connect(path, busy_timeout_ms)

    monkeypatch.setattr(wal_recovery, "_fsync_dir", fail_on_backup_root)
    monkeypatch.setattr(wal_recovery, "_checkpoint_pragma", record_checkpoint)
    monkeypatch.setattr(wal_recovery, "_connect_rw", record_connect)

    with pytest.raises(wal_recovery.SnapshotFailed) as caught:
        _rehearse(crashed, tmp_path)

    assert caught.value.stage == "run-dir-fsync"
    assert caught.value.report.checkpoint_started is False
    assert checkpoint_calls == []
    assert opened == []
    assert _source_digests(crashed) == before


def _refuse_to_fsync(monkeypatch):
    def refuse(path):
        raise OSError(5, "Input/output error")

    monkeypatch.setattr(wal_recovery, "_fsync_file", refuse)


def test_a_failed_snapshot_fsync_stops_before_any_checkpoint(
    crashed, tmp_path, monkeypatch
):
    before = _source_digests(crashed)
    _refuse_to_fsync(monkeypatch)

    with pytest.raises(wal_recovery.SnapshotFailed) as caught:
        _rehearse(crashed, tmp_path)

    assert caught.value.code == "SNAPSHOT_FAILED"
    assert caught.value.report.checkpoint_started is False
    assert _source_digests(crashed) == before


def test_a_failed_snapshot_preserves_whatever_it_had_already_written(
    crashed, tmp_path, monkeypatch
):
    _refuse_to_fsync(monkeypatch)

    with pytest.raises(wal_recovery.SnapshotFailed) as caught:
        _rehearse(crashed, tmp_path)

    # Nothing is swept up on failure: the operator gets to look at exactly what
    # the run produced before it stopped.
    assert caught.value.report.run_dir is not None
    assert Path(caught.value.report.run_dir).is_dir()


# ── 5. the checkpoint gate ───────────────────────────

def test_a_busy_checkpoint_is_reported_as_a_failure_by_the_primitive(crashed):
    """The checkpoint gate itself, exercised without the public entrypoint.

    A live reader is a genuine fence violation, so a run that reached the
    public entrypoint with one open would — correctly — be refused long before
    the gate under test could fire. Standing a fence down to get here would
    mean a test-only object walking through the front door, which is exactly
    what the interlock now forbids. So this drops to `_checkpoint`, which is
    the thing the assertion is about.
    """
    reader = sqlite3.connect(
        f"file:{crashed.db}?mode=rw", uri=True, isolation_level=None
    )
    checkpointer = wal_recovery._connect_rw(str(crashed.db), 200)
    try:
        reader.execute("PRAGMA wal_autocheckpoint=0")
        reader.execute("BEGIN")
        reader.execute("SELECT count(*) FROM events").fetchone()

        result = wal_recovery._checkpoint(checkpointer, str(crashed.db))
    finally:
        checkpointer.close()
        reader.close()

    assert result.busy == 1
    assert result.ok is False
    # It failed, and it had already moved pages. The WAL is still there, which
    # is why a checkpoint is never assumed to have removed one.
    assert result.db_bytes_after >= result.db_bytes_before
    assert result.wal_present_after is True
    assert result.wal_bytes_after > 0


def test_a_partial_checkpoint_of_the_backup_stops_before_the_source(
    crashed, tmp_path, monkeypatch
):
    """`checkpointed != log` means frames were left behind, and that is a
    failure even with `busy == 0`. Catching it on the disposable copy is the
    whole reason that copy is checkpointed first."""
    before = _source_digests(crashed)
    monkeypatch.setattr(wal_recovery, "_checkpoint_pragma", lambda conn: (0, 12, 7))

    with pytest.raises(wal_recovery.CheckpointFailed) as caught:
        _rehearse(crashed, tmp_path)

    result = caught.value.report.restore_checkpoint
    assert caught.value.stage == "restore-checkpoint"
    assert (result.busy, result.log, result.checkpointed) == (0, 12, 7)
    assert result.ok is False
    assert caught.value.report.checkpoint_started is False
    assert _source_digests(crashed) == before


def test_a_partial_checkpoint_of_the_source_is_refused(crashed, tmp_path, monkeypatch):
    real_pragma = wal_recovery._checkpoint_pragma
    calls = []

    def partial_on_the_second_database(conn):
        calls.append(conn)
        return real_pragma(conn) if len(calls) == 1 else (0, 12, 7)

    monkeypatch.setattr(
        wal_recovery, "_checkpoint_pragma", partial_on_the_second_database
    )

    with pytest.raises(wal_recovery.CheckpointFailed) as caught:
        _rehearse(crashed, tmp_path)

    result = caught.value.report.source_checkpoint
    assert caught.value.stage == "source-checkpoint"
    assert (result.busy, result.log, result.checkpointed) == (0, 12, 7)
    assert result.ok is False
    assert caught.value.report.checkpoint_started is True


# ── 6. the success path ──────────────────────────────

def test_recovery_folds_the_wal_only_commit_into_the_database(crashed, tmp_path):
    db_bytes_before = crashed.db.stat().st_size
    wal_bytes_before = crashed.wal.stat().st_size
    db_sha_before, wal_sha_before = _source_digests(crashed)

    report = _rehearse(crashed, tmp_path)

    evidence = report.wal_only_evidence
    assert evidence.db is report.source_before_db
    assert evidence.wal is report.source_before_wal
    assert evidence.db_only_sentinel_rows == 0
    assert evidence.db_plus_wal_sentinel_rows == 1
    assert report.space.db_bytes == evidence.db.size_bytes
    assert report.space.wal_bytes == evidence.wal.size_bytes
    assert report.stages.index("wal-only-oracle") < report.stages.index(
        "restore-checkpoint"
    )
    assert report.stages.index("restore-checkpoint") < report.stages.index(
        "source-checkpoint"
    )
    snapshot_dir = Path(report.snapshot_db.path).parent
    assert {path.name for path in snapshot_dir.iterdir()} == {
        Path(report.snapshot_db.path).name,
        Path(report.snapshot_wal.path).name,
    }
    assert _sha256(Path(report.snapshot_db.path)) == report.snapshot_db.sha256
    assert _sha256(Path(report.snapshot_wal.path)) == report.snapshot_wal.sha256
    assert not Path(f"{report.snapshot_db.path}-shm").exists()

    checkpoint = report.source_checkpoint
    assert checkpoint.busy == 0
    assert checkpoint.checkpointed == checkpoint.log
    assert checkpoint.ok is True
    assert checkpoint.wal_bytes_before == wal_bytes_before > 0
    # The WAL is not assumed to be gone; whether it was truncated in place or
    # removed is recorded, not presumed.
    assert checkpoint.wal_bytes_after == 0
    assert checkpoint.db_bytes_before == db_bytes_before
    assert checkpoint.db_bytes_after >= checkpoint.db_bytes_before

    readback = report.source_readback
    assert readback.integrity == "ok"
    assert readback.sentinel_rows == 1
    assert readback.tables["events"].row_count == crashed.total_rows
    assert readback.tables["events"].max_id == crashed.max_id
    assert readback.tables["events"].max_timestamp == crashed.max_ts
    assert readback.matches(report.baseline)

    # The snapshot is the source exactly as it was found, and it was never
    # written to afterwards.
    assert report.snapshot_db.sha256 == db_sha_before
    assert report.snapshot_wal.sha256 == wal_sha_before


def test_the_recovered_database_serves_the_rescued_row(crashed, tmp_path):
    _rehearse(crashed, tmp_path)

    conn = sqlite3.connect(f"file:{crashed.db}?mode=rw", uri=True)
    try:
        rows = conn.execute(
            "SELECT count(*) FROM events WHERE name = ?", (SENTINEL.value,)
        ).fetchone()[0]
        total = conn.execute("SELECT count(*) FROM events").fetchone()[0]
    finally:
        conn.close()

    assert (rows, total) == (1, crashed.total_rows)


def test_the_restore_copy_passes_integrity_and_an_exact_readback(crashed, tmp_path):
    report = _rehearse(crashed, tmp_path)

    assert report.restore_checkpoint.ok is True
    assert report.restore_readback.integrity == "ok"
    assert report.restore_readback.sentinel_rows == 1
    assert report.restore_readback.matches(report.baseline)
    # Restoring from the backup and recovering the original must land on the
    # same numbers; if they disagree the backup is not a backup.
    assert report.restore_readback.matches(report.source_readback)


def test_recovery_leaves_the_journal_mode_and_the_wal_file_alone(crashed, tmp_path):
    _rehearse(crashed, tmp_path)

    conn = sqlite3.connect(f"file:{crashed.db}?mode=rw", uri=True)
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    finally:
        conn.close()


def test_reusing_a_run_id_is_refused_without_disturbing_the_first_run(
    crashed, tmp_path, children
):
    first = _rehearse(crashed, tmp_path, run_id="run-1")
    run_dir = Path(first.run_dir)
    before = {
        str(p.relative_to(run_dir)): _sha256(p)
        for p in sorted(run_dir.rglob("*"))
        if p.is_file()
    }
    assert before, "the first run wrote no files to preserve"

    # The first run consumed the source's WAL, and whether an empty sidecar
    # survives that is the OS's choice (macOS keeps it, Linux deletes it). A
    # second source with its own hard crash carries a real WAL on every
    # platform, so the second run gets past the fence and the refusal under
    # test is the run directory, not the probe.
    second_source = tmp_path / "source-2"
    second_source.mkdir()
    second = _build_fixture(second_source, children)
    assert second.wal.stat().st_size > 0, "the second source has no WAL to offer"

    with pytest.raises(wal_recovery.BackupRejected) as caught:
        _rehearse(second, tmp_path, run_id="run-1")

    assert caught.value.code == "BACKUP_REJECTED"
    assert caught.value.stage == "run-dir"
    after = {
        str(p.relative_to(run_dir)): _sha256(p)
        for p in sorted(run_dir.rglob("*"))
        if p.is_file()
    }
    assert after == before


# ── 6b. the seams the whole procedure rests on ───────

def test_opening_a_missing_database_never_brings_one_into_being(tmp_path):
    missing = tmp_path / "gone.db"

    with pytest.raises(sqlite3.OperationalError):
        wal_recovery._connect_rw(str(missing), 200)

    # `mode=rw` is the only thing standing between a typo'd path and a brand
    # new empty database that would read back as a flawless recovery.
    assert list(tmp_path.iterdir()) == []


def test_copying_onto_an_existing_file_is_refused(tmp_path):
    src = tmp_path / "new"
    src.write_bytes(b"replacement")
    dst = tmp_path / "existing"
    dst.write_bytes(b"an earlier backup")

    with pytest.raises(FileExistsError):
        wal_recovery._copy_file(str(src), str(dst))

    assert dst.read_bytes() == b"an earlier backup"


def test_a_table_name_that_is_not_an_identifier_is_refused(crashed, tmp_path):
    backups = tmp_path / "backups"
    backups.mkdir()

    with pytest.raises(wal_recovery.ProbeRejected):
        wal_recovery.rehearse_wal_recovery(
            db_path=crashed.db,
            backup_root=backups,
            run_id="run-1",
            sentinel=SENTINEL,
            probes=(wal_recovery.Probe(table='events" --'),),
            fence=NEVER_ASKED,
        )

    assert list(backups.iterdir()) == []


def test_a_readback_that_disagrees_with_the_baseline_is_not_equal():
    tables = {"events": wal_recovery.TableFacts(10, 10, 1.0)}
    baseline = wal_recovery.Readback("ok", 1, tables)

    assert baseline.matches(wal_recovery.Readback("ok", 1, dict(tables)))
    assert not baseline.matches(
        wal_recovery.Readback("ok", 1, {"events": wal_recovery.TableFacts(9, 10, 1.0)})
    )
    assert not baseline.matches(
        wal_recovery.Readback("ok", 1, {"events": wal_recovery.TableFacts(10, 9, 1.0)})
    )
    assert not baseline.matches(
        wal_recovery.Readback("ok", 1, {"events": wal_recovery.TableFacts(10, 10, 0.5)})
    )
    assert not baseline.matches(wal_recovery.Readback("ok", 0, dict(tables)))


def _drift_readback(monkeypatch, nth, transform):
    """Let the nth read-back come back changed, and leave the rest alone.

    The reads happen in a fixed order — baseline, restore, source — so `nth`
    picks which database is made to look wrong without touching the others.
    """
    real = wal_recovery._read_back
    seen = []

    def wrapped(conn, sentinel, probes):
        result = real(conn, sentinel, probes)
        seen.append(result)
        return transform(result) if len(seen) == nth else result

    monkeypatch.setattr(wal_recovery, "_read_back", wrapped)


def _with_row_count(readback, row_count):
    events = readback.tables["events"]
    return wal_recovery.Readback(
        readback.integrity,
        readback.sentinel_rows,
        {
            "events": wal_recovery.TableFacts(
                row_count, events.max_id, events.max_timestamp
            )
        },
    )


def test_a_source_that_comes_back_short_fails_verification(
    crashed, tmp_path, monkeypatch
):
    _drift_readback(
        monkeypatch, 3, lambda rb: _with_row_count(rb, rb.tables["events"].row_count - 1)
    )

    with pytest.raises(wal_recovery.VerificationFailed) as caught:
        _rehearse(crashed, tmp_path)

    assert caught.value.code == "VERIFICATION_FAILED"
    assert caught.value.stage == "source-verify"
    # The checkpoint had already run by then; saying otherwise would be a lie.
    assert caught.value.report.checkpoint_started is True


def test_a_source_that_comes_back_without_the_sentinel_fails_verification(
    crashed, tmp_path, monkeypatch
):
    _drift_readback(
        monkeypatch,
        3,
        lambda rb: wal_recovery.Readback(rb.integrity, 0, rb.tables),
    )

    with pytest.raises(wal_recovery.VerificationFailed) as caught:
        _rehearse(crashed, tmp_path)

    assert "lost the sentinel" in str(caught.value)


def test_a_restore_copy_that_comes_back_short_fails_before_the_source_is_touched(
    crashed, tmp_path, monkeypatch
):
    before = _source_digests(crashed)
    _drift_readback(monkeypatch, 2, lambda rb: _with_row_count(rb, 0))

    with pytest.raises(wal_recovery.VerificationFailed) as caught:
        _rehearse(crashed, tmp_path)

    assert caught.value.stage == "restore-verify"
    assert caught.value.report.checkpoint_started is False
    assert _source_digests(crashed) == before


def test_a_source_that_changes_during_the_backup_is_refused(
    crashed, tmp_path, monkeypatch
):
    """Something else wrote to the database while it was being copied.

    The copy still matches what was hashed a moment earlier, so the snapshot
    looks perfect; only re-reading the SOURCE afterwards catches it. Carrying
    on here would checkpoint a database that no longer resembles the backup
    taken of it.
    """
    real_fsync_dir = wal_recovery._fsync_dir
    intruded = []

    def a_writer_slips_in(path):
        real_fsync_dir(path)
        if not intruded:
            intruded.append(path)
            with open(crashed.wal, "ab") as handle:
                handle.write(b"\x00" * 8)

    monkeypatch.setattr(wal_recovery, "_fsync_dir", a_writer_slips_in)

    with pytest.raises(wal_recovery.SnapshotFailed) as caught:
        _rehearse(crashed, tmp_path)

    assert caught.value.stage == "snapshot-verify"
    assert caught.value.report.checkpoint_started is False


def test_a_source_that_changes_after_restore_verification_is_refused_before_open(
    crashed, tmp_path, monkeypatch
):
    real_require_verified = wal_recovery._require_verified
    real_connect = wal_recovery._connect_rw
    late_write = []
    source_opens = []

    def write_after_restore_is_verified(report, readback, which):
        real_require_verified(report, readback, which)
        if which == "restore":
            conn = sqlite3.connect(crashed.db, isolation_level=None)
            try:
                conn.execute("PRAGMA wal_autocheckpoint=0")
                conn.execute("INSERT INTO audit(note) VALUES ('late writer')")
            finally:
                conn.close()
            late_write.append(True)

    monkeypatch.setattr(
        wal_recovery, "_require_verified", write_after_restore_is_verified
    )

    def record_source_open(path, busy_timeout_ms):
        if os.path.samefile(path, crashed.db):
            source_opens.append(path)
        return real_connect(path, busy_timeout_ms)

    monkeypatch.setattr(wal_recovery, "_connect_rw", record_source_open)

    with pytest.raises(wal_recovery.SnapshotFailed) as caught:
        _rehearse(crashed, tmp_path)

    assert late_write == [True]
    assert caught.value.stage == "source-revalidate"
    assert caught.value.report.checkpoint_started is False
    assert source_opens == []


@pytest.mark.parametrize("part", ["db", "wal"])
def test_byte_identical_source_replacement_is_refused_by_identity(
    crashed, tmp_path, monkeypatch, part
):
    real_require_verified = wal_recovery._require_verified
    target = crashed.db if part == "db" else crashed.wal
    inode_before = target.stat().st_ino
    size_before = target.stat().st_size
    sha_before = _sha256(target)
    replaced = []

    def replace_after_restore_is_verified(report, readback, which):
        real_require_verified(report, readback, which)
        if which == "restore":
            replacement = tmp_path / f"replacement-{part}"
            shutil.copyfile(target, replacement)
            os.replace(replacement, target)
            replaced.append(target.stat().st_ino)

    monkeypatch.setattr(
        wal_recovery, "_require_verified", replace_after_restore_is_verified
    )

    with pytest.raises(wal_recovery.SnapshotFailed) as caught:
        _rehearse(crashed, tmp_path)

    assert replaced and replaced[0] != inode_before
    assert target.stat().st_size == size_before
    assert _sha256(target) == sha_before
    assert caught.value.stage == "source-revalidate"
    assert caught.value.report.checkpoint_started is False


def test_a_run_directory_that_appears_during_the_preflight_is_still_refused(
    crashed, tmp_path, monkeypatch
):
    """Losing the race must not mean writing over the winner.

    Checking that a run directory is free and then creating it are two steps,
    and a second run can arrive in between. Creating it is what has to fail.
    """
    backups = tmp_path / "backups"
    backups.mkdir()
    real_plan = wal_recovery.plan_space

    def plan_then_lose_the_race(**kwargs):
        result = real_plan(**kwargs)
        (backups / "run-1").mkdir()
        return result

    monkeypatch.setattr(wal_recovery, "plan_space", plan_then_lose_the_race)

    with pytest.raises(wal_recovery.BackupRejected) as caught:
        wal_recovery.rehearse_wal_recovery(
            db_path=crashed.db,
            backup_root=backups,
            run_id="run-1",
            sentinel=SENTINEL,
            probes=PROBES,
            fence=NEVER_ASKED,
        )

    assert caught.value.stage == "run-dir"
    assert list((backups / "run-1").iterdir()) == []


def test_a_source_that_fails_its_integrity_check_is_refused(
    crashed, tmp_path, monkeypatch
):
    _drift_readback(
        monkeypatch,
        3,
        lambda rb: wal_recovery.Readback(
            "*** in database main *** page 3 is never used", rb.sentinel_rows, rb.tables
        ),
    )

    with pytest.raises(wal_recovery.VerificationFailed) as caught:
        _rehearse(crashed, tmp_path)

    assert "integrity" in str(caught.value)


def test_a_probe_naming_a_table_that_is_not_there_is_refused(crashed, tmp_path):
    before = _source_digests(crashed)
    backups = tmp_path / "backups"
    backups.mkdir()

    with pytest.raises(wal_recovery.VerificationFailed):
        wal_recovery.rehearse_wal_recovery(
            db_path=crashed.db,
            backup_root=backups,
            run_id="run-1",
            sentinel=SENTINEL,
            probes=(wal_recovery.Probe(table="no_such_table"),),
            fence=NEVER_ASKED,
        )

    assert _source_digests(crashed) == before


# ── 6c. the host fence CP-5B puts in front of the source ───

def test_a_rehearsal_without_a_fence_is_refused_before_anything_is_copied(
    crashed, tmp_path
):
    before = _source_digests(crashed)
    backups = tmp_path / "backups"
    backups.mkdir()

    with pytest.raises(wal_recovery.FenceRequired) as caught:
        wal_recovery.rehearse_wal_recovery(
            db_path=crashed.db,
            backup_root=backups,
            run_id="run-1",
            sentinel=SENTINEL,
            probes=PROBES,
            fence=None,
        )

    assert caught.value.code == "FENCE_REQUIRED"
    # Refused up front rather than after a full backup: there is no point
    # copying gigabytes to discover at the last step that nobody stopped the
    # service.
    assert list(backups.iterdir()) == []
    assert _source_digests(crashed) == before


def test_an_object_that_is_not_a_fence_is_refused(crashed, tmp_path):
    backups = tmp_path / "backups"
    backups.mkdir()

    with pytest.raises(wal_recovery.FenceRequired):
        wal_recovery.rehearse_wal_recovery(
            db_path=crashed.db,
            backup_root=backups,
            run_id="run-1",
            sentinel=SENTINEL,
            probes=PROBES,
            fence=object(),
        )

    assert list(backups.iterdir()) == []


def test_an_expired_lease_stops_the_run_before_the_source_is_opened(
    crashed, tmp_path
):
    before = _source_digests(crashed)
    fence, host, clock = _quiesce(crashed, tmp_path)
    clock.advance(10_000)

    with pytest.raises(wal_recovery.FenceRefused) as caught:
        _rehearse(crashed, tmp_path, fence=fence)

    # 1. the original refusal survives intact
    assert caught.value.code == "FENCE_REFUSED"
    assert caught.value.stage == "source-revalidate"
    assert isinstance(caught.value.__cause__, wal_fence.FenceStale)
    assert caught.value.report.checkpoint_started is False
    # 2. the database is byte-for-byte what it was
    assert _source_digests(crashed) == before
    # 3. the claim is finished, and finished as a failure. A claim left live
    #    after a run that will never resume is a fence nobody can release: the
    #    containers stay down and the policies stay pinned to `no` until
    #    somebody works out by hand what happened.
    assert fence.is_active is False
    assert caught.value.report.claim_outcome == "failed"
    done = json.loads(Path(f"{fence.manifest.path}.claim.done.json").read_text())
    assert done["state"] == "FAILED"
    assert done["claim_id"] == fence.claim_id
    # 4. so the host can be put back the ordinary way
    record = wal_fence.release(
        manifest_path=fence.manifest.path, runner=host, clock=clock
    )
    assert set(record.restored) == {AGENT_ID, BACKEND_ID}
    assert record.started_containers == ()


def test_a_container_that_came_back_up_stops_the_run(crashed, tmp_path):
    before = _source_digests(crashed)
    fence, host, _clock = _quiesce(crashed, tmp_path)
    host.container(AGENT_ID).status = "running"
    host.container(AGENT_ID).running = True

    with pytest.raises(wal_recovery.FenceRefused) as caught:
        _rehearse(crashed, tmp_path, fence=fence)

    assert isinstance(caught.value.__cause__, wal_fence.FenceBroken)
    assert caught.value.report.checkpoint_started is False
    assert _source_digests(crashed) == before


def test_a_descriptor_opened_behind_the_fence_stops_the_run(crashed, tmp_path):
    from tests.backend.conftest import OpenFile

    fence, host, _clock = _quiesce(crashed, tmp_path)
    host.open_files.append(OpenFile(pid=4711, fd=9, path=str(crashed.db)))

    with pytest.raises(wal_recovery.FenceRefused) as caught:
        _rehearse(crashed, tmp_path, fence=fence)

    assert isinstance(caught.value.__cause__, wal_fence.DescriptorsOpen)
    assert caught.value.report.checkpoint_started is False


def test_the_fence_is_consulted_after_the_backup_and_before_the_source_opens(
    crashed, tmp_path
):
    real, _host, _clock = _quiesce(crashed, tmp_path)
    seen = []

    class Recording(wal_recovery.SourceApplyAuthority):
        def check_before_source_open(self, *, db_path):
            seen.append((db_path, Path(tmp_path / "backups" / "run-1").is_dir()))
            return real.check_before_source_open(db_path=db_path)

        def begin_apply(self):
            return real.begin_apply()

        def complete(self):
            real.complete()

        def fail(self, reason):
            real.fail(reason)

    report = _rehearse(crashed, tmp_path, fence=Recording())

    # Asked once, at the last moment: after the verified backup exists, and
    # before anything has opened the original.
    assert seen == [(str(crashed.db), True)]
    assert report.stages.index("restore-verify") < report.stages.index(
        "source-revalidate"
    )
    assert report.stages.index("source-revalidate") < report.stages.index(
        "source-open"
    )
    assert report.stages.index("source-open") < report.stages.index(
        "source-checkpoint"
    )


def test_the_report_records_the_fence_that_authorised_the_checkpoint(
    crashed, tmp_path
):
    fence, _host, _clock = _quiesce(crashed, tmp_path)

    report = _rehearse(crashed, tmp_path, fence=fence)

    assert isinstance(report.fence_check, wal_recovery.SourceApplyGrant)
    assert report.fence_check.ok is True
    assert report.fence_check.lease_id == fence.lease_id
    assert report.fence_check.claim_id == fence.claim_id
    assert report.fence_check.db_inode == report.source_revalidated_db.inode
    assert report.claim_outcome == "completed"


# ── 6d. only a capability CP-5B issued may open the source ───

class AlwaysYes(wal_recovery.SourceApplyAuthority):
    """Says yes to everything and returns nothing to back it up."""

    def check_before_source_open(self, *, db_path):
        return None

    def begin_apply(self):
        return "always-yes-apply"

    def complete(self):
        pass

    def fail(self, reason):
        pass


class Shaped(wal_recovery.SourceApplyAuthority):
    """Returns a well-formed grant it has no authority to issue.

    Everything measurable about it is correct — it is built from the real file
    — and the only thing wrong is that no fence ever issued it. That is the
    whole point: a grant is only worth anything because of where it came from.
    """

    def __init__(self, db_path, *, claim_id="a-claim", lease_id="a-lease", ok=True):
        self.db_path = Path(db_path)
        self.claim_id = claim_id
        self.lease_id = lease_id
        self.ok = ok

    def check_before_source_open(self, *, db_path):
        facts = os.stat(self.db_path)
        wal = Path(f"{self.db_path}-wal")
        wal_stat = os.stat(wal) if wal.exists() else None
        return wal_recovery.SourceApplyGrant(
            claim_id=self.claim_id,
            lease_id=self.lease_id,
            ok=self.ok,
            db_path=str(self.db_path),
            wal_path=str(wal),
            db_inode=facts.st_ino,
            db_device=facts.st_dev,
            db_sha256=_sha256(self.db_path),
            wal_inode=wal_stat.st_ino if wal_stat else None,
            wal_device=wal_stat.st_dev if wal_stat else None,
            wal_sha256=_sha256(wal),
        )

    def begin_apply(self):
        return "shaped-apply"

    def complete(self):
        pass

    def fail(self, reason):
        pass


def test_a_fence_that_returns_nothing_is_refused_before_the_checkpoint(
    crashed, tmp_path
):
    """RED G. An object that answers "yes" is not a capability."""
    before = _source_digests(crashed)

    with pytest.raises(wal_recovery.FenceRefused) as caught:
        _rehearse(crashed, tmp_path, fence=AlwaysYes())

    assert caught.value.code == "FENCE_REFUSED"
    assert caught.value.report.checkpoint_started is False
    assert _source_digests(crashed) == before


@pytest.mark.parametrize(
    "flaw, expected",
    [
        ({"claim_id": ""}, "claim id"),
        ({"claim_id": "   "}, "claim id"),
        ({"lease_id": ""}, "lease id"),
        ({"ok": False}, "ok="),
    ],
)
def test_a_grant_missing_its_authority_is_refused(crashed, tmp_path, flaw, expected):
    """Each field on its own is enough to refuse.

    Otherwise one surviving check papers over the loss of the others, and the
    suite stops noticing which one is doing the work.
    """
    before = _source_digests(crashed)

    with pytest.raises(wal_recovery.FenceRefused) as caught:
        _rehearse(crashed, tmp_path, fence=Shaped(crashed.db, **flaw))

    assert expected in str(caught.value)
    assert caught.value.report.checkpoint_started is False
    assert _source_digests(crashed) == before


def test_a_grant_naming_a_different_database_is_refused(crashed, tmp_path):
    capability, _host, _clock = _quiesce(crashed, tmp_path)
    elsewhere = tmp_path / "elsewhere.db"
    elsewhere.write_bytes(crashed.db.read_bytes())

    class Misdirected(wal_recovery.SourceApplyAuthority):
        def check_before_source_open(self, *, db_path):
            grant = capability.check_before_source_open(db_path=db_path)
            return dataclasses.replace(grant, db_path=str(elsewhere))

        def begin_apply(self):

            return capability.begin_apply()


        def complete(self):
            capability.complete()

        def fail(self, reason):
            capability.fail(reason)

    with pytest.raises(wal_recovery.FenceRefused) as caught:
        _rehearse(crashed, tmp_path, fence=Misdirected())

    assert caught.value.report.checkpoint_started is False


def test_a_grant_whose_identity_disagrees_with_the_source_is_refused(
    crashed, tmp_path
):
    capability, _host, _clock = _quiesce(crashed, tmp_path)

    class Stale(wal_recovery.SourceApplyAuthority):
        def check_before_source_open(self, *, db_path):
            grant = capability.check_before_source_open(db_path=db_path)
            return dataclasses.replace(grant, db_inode=grant.db_inode + 1)

        def begin_apply(self):

            return capability.begin_apply()


        def complete(self):
            capability.complete()

        def fail(self, reason):
            capability.fail(reason)

    with pytest.raises(wal_recovery.FenceRefused) as caught:
        _rehearse(crashed, tmp_path, fence=Stale())

    # Caught when the grant is read, not later when the file is opened: a
    # grant that disagrees with the file is wrong on arrival.
    assert caught.value.stage == "source-revalidate"
    assert caught.value.report.checkpoint_started is False


def test_a_database_swapped_between_the_check_and_the_open_is_refused(
    crashed, tmp_path
):
    """RED H. The grant was true when it was issued and false a moment later."""
    before = _source_digests(crashed)
    capability, _host, _clock = _quiesce(crashed, tmp_path)

    class SwapsAfterChecking(wal_recovery.SourceApplyAuthority):
        def check_before_source_open(self, *, db_path):
            grant = capability.check_before_source_open(db_path=db_path)
            # A different file takes the database's place, atomically, in the
            # window between the last check and SQLite's open.
            impostor = tmp_path / "impostor.db"
            shutil.copy2(crashed.db, impostor)
            os.replace(impostor, crashed.db)
            return grant

        def begin_apply(self):

            return capability.begin_apply()


        def complete(self):
            capability.complete()

        def fail(self, reason):
            capability.fail(reason)

    with pytest.raises(wal_recovery.FenceRefused) as caught:
        _rehearse(crashed, tmp_path, fence=SwapsAfterChecking())

    assert caught.value.report.checkpoint_started is False
    # The bytes are the same; the file is not. Only inode and device say so.
    assert _sha256(crashed.db) == before[0]


def test_a_wal_swapped_between_the_check_and_the_open_is_refused(crashed, tmp_path):
    capability, _host, _clock = _quiesce(crashed, tmp_path)

    class SwapsTheWal(wal_recovery.SourceApplyAuthority):
        def check_before_source_open(self, *, db_path):
            grant = capability.check_before_source_open(db_path=db_path)
            impostor = tmp_path / "impostor-wal"
            shutil.copy2(crashed.wal, impostor)
            os.replace(impostor, crashed.wal)
            return grant

        def begin_apply(self):

            return capability.begin_apply()


        def complete(self):
            capability.complete()

        def fail(self, reason):
            capability.fail(reason)

    with pytest.raises(wal_recovery.FenceRefused) as caught:
        _rehearse(crashed, tmp_path, fence=SwapsTheWal())

    assert caught.value.report.checkpoint_started is False


def test_a_database_swapped_while_it_is_being_opened_is_refused(
    crashed, tmp_path, monkeypatch
):
    """The narrowest window there is: between our descriptor and SQLite's.

    Nothing in a single-threaded test reaches it on its own, so the swap is
    injected into the open itself. The check costs one `stat` and closes the
    last gap where a rename could still land.
    """
    capability, _host, _clock = _quiesce(crashed, tmp_path)
    real_connect = wal_recovery._connect_rw
    swapped = []

    def swap_then_connect(path, busy_timeout_ms):
        if str(path) == str(crashed.db) and not swapped:
            swapped.append(path)
            impostor = tmp_path / "impostor-mid-open.db"
            shutil.copy2(crashed.db, impostor)
            os.replace(impostor, crashed.db)
        return real_connect(path, busy_timeout_ms)

    monkeypatch.setattr(wal_recovery, "_connect_rw", swap_then_connect)

    with pytest.raises(wal_recovery.FenceRefused) as caught:
        _rehearse(crashed, tmp_path, fence=capability)

    assert caught.value.stage == "source-open"
    assert "while it was being opened" in str(caught.value)


def test_the_claim_is_completed_when_the_recovery_finishes(crashed, tmp_path):
    capability, _host, _clock = _quiesce(crashed, tmp_path)

    report = _rehearse(crashed, tmp_path, fence=capability)

    assert report.fence_check.ok is True
    assert capability.is_active is False


def test_the_claim_is_failed_when_the_recovery_does_not(crashed, tmp_path, monkeypatch):
    capability, _host, _clock = _quiesce(crashed, tmp_path)
    _drift_readback(monkeypatch, 3, lambda rb: _with_row_count(rb, 0))

    with pytest.raises(wal_recovery.VerificationFailed):
        _rehearse(crashed, tmp_path, fence=capability)

    # The lease is spent either way: a failed recovery must not leave a live
    # authorisation lying around for something else to pick up.
    assert capability.is_active is False


def test_a_failure_after_the_checkpoint_is_typed_and_keeps_the_report(
    crashed, tmp_path, monkeypatch
):
    real = wal_recovery._read_back
    calls = []

    def explode_after_the_checkpoint(conn, sentinel, probes):
        calls.append(1)
        if len(calls) == 3:
            raise RuntimeError("the disk went away")
        return real(conn, sentinel, probes)

    monkeypatch.setattr(wal_recovery, "_read_back", explode_after_the_checkpoint)

    with pytest.raises(wal_recovery.WalRecoveryError) as caught:
        _rehearse(crashed, tmp_path)

    # The source has already been written to. Whatever went wrong, the report
    # of what happened to it has to come back with the error.
    assert caught.value.report is not None
    assert caught.value.report.checkpoint_started is True
    assert caught.value.report.source_checkpoint is not None
    assert isinstance(caught.value.__cause__, RuntimeError)


# ── 6e. provenance: a grant is only worth where it came from ───

class Forged(wal_recovery.SourceApplyAuthority):
    """Every field correct, and no fence behind any of them.

    This is the counterexample the interlock exists for: a subclass can copy
    the shape of a real capability perfectly, because the shape is public. It
    cannot produce the claim record on disk that a real quiesce leaves behind,
    and that record is what the recovery goes and reads for itself.
    """

    def __init__(self, db_path, *, claim_record_path="", manifest_path=""):
        self.db_path = Path(db_path)
        self.claim_record_path = claim_record_path
        self.manifest_path = manifest_path
        self.began = 0

    def check_before_source_open(self, *, db_path):
        facts = os.stat(self.db_path)
        wal = Path(f"{self.db_path}-wal")
        wal_stat = os.stat(wal)
        return wal_recovery.SourceApplyGrant(
            claim_id="forged-claim",
            lease_id="forged-lease",
            ok=True,
            db_path=str(self.db_path),
            wal_path=str(wal),
            db_inode=facts.st_ino,
            db_device=facts.st_dev,
            db_sha256=_sha256(self.db_path),
            wal_inode=wal_stat.st_ino,
            wal_device=wal_stat.st_dev,
            wal_sha256=_sha256(wal),
            claim_record_path=self.claim_record_path,
            manifest_path=self.manifest_path,
        )

    def begin_apply(self):
        self.began += 1
        return "forged-apply"

    def complete(self):
        pass

    def fail(self, reason):
        pass


def test_a_forged_authority_never_reaches_the_source(crashed, tmp_path):
    """RED A. Correct fields, no provenance."""
    before = _source_digests(crashed)

    with pytest.raises(wal_recovery.ApplyRecordRejected) as caught:
        _rehearse(crashed, tmp_path, fence=Forged(crashed.db))

    assert caught.value.code == "APPLY_RECORD_REJECTED"
    assert caught.value.report.checkpoint_started is False
    assert _source_digests(crashed) == before


def test_a_forged_authority_never_gets_to_begin_an_apply(crashed, tmp_path):
    forged = Forged(crashed.db)

    with pytest.raises(wal_recovery.ApplyRecordRejected):
        _rehearse(crashed, tmp_path, fence=forged)

    # Refused while reading its papers, before it was ever asked to start.
    assert forged.began == 0


def test_a_capability_built_by_hand_without_a_claim_record_is_refused(
    crashed, tmp_path
):
    """RED A. The real class, constructed directly, with no `.claim.json`."""
    before = _source_digests(crashed)
    capability, host, clock = _quiesce(crashed, tmp_path)
    # Same manifest, same runner — but no claim was ever taken for this one.
    impostor = wal_fence.SourceApplyCapability(
        capability.manifest,
        claim_id="never-issued",
        runner=host,
        clock=clock,
        authority_verifier=capability.authority_verifier,
    )

    with pytest.raises(wal_recovery.WalRecoveryError) as caught:
        _rehearse(crashed, tmp_path, fence=impostor)

    assert caught.value.report.checkpoint_started is False
    assert _source_digests(crashed) == before


def test_a_grant_pointing_at_a_claim_record_for_another_database_is_refused(
    crashed, tmp_path
):
    capability, _host, _clock = _quiesce(crashed, tmp_path)
    elsewhere = tmp_path / "other.db"
    elsewhere.write_bytes(crashed.db.read_bytes())

    class Misbound(wal_recovery.SourceApplyAuthority):
        def check_before_source_open(self, *, db_path):
            grant = capability.check_before_source_open(db_path=db_path)
            return dataclasses.replace(grant, manifest_path=str(elsewhere))

        def begin_apply(self):
            return capability.begin_apply()

        def complete(self):
            capability.complete()

        def fail(self, reason):
            capability.fail(reason)

    with pytest.raises(wal_recovery.ApplyRecordRejected):
        _rehearse(crashed, tmp_path, fence=Misbound())


def _reseal(path: Path, **changes):
    """Rewrite a claim record so only the FIELDS are wrong, not the digest."""
    body = json.loads(path.read_text())
    body.pop("content_sha256")
    body.update(changes)
    body["content_sha256"] = hashlib.sha256(
        json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    path.write_text(json.dumps(body))


def test_a_claim_whose_manifest_has_gone_is_refused(crashed, tmp_path):
    """A claim with no quiesce behind it authorises nothing."""
    before = _source_digests(crashed)
    capability, _host, _clock = _quiesce(crashed, tmp_path)
    os.replace(capability.manifest.path, tmp_path / "manifest-moved-away.json")

    with pytest.raises(wal_recovery.ApplyRecordRejected) as caught:
        _rehearse(crashed, tmp_path, fence=capability)

    assert caught.value.report.checkpoint_started is False
    assert _source_digests(crashed) == before


def test_a_claim_record_edited_after_it_was_written_is_refused(crashed, tmp_path):
    before = _source_digests(crashed)
    capability, _host, _clock = _quiesce(crashed, tmp_path)
    record = Path(capability.claim_record_path)
    body = json.loads(record.read_text())
    body["db_path"] = "/somewhere/else.db"
    record.write_text(json.dumps(body))  # digest left stale on purpose

    with pytest.raises(wal_recovery.ApplyRecordRejected) as caught:
        _rehearse(crashed, tmp_path, fence=capability)

    assert "digest" in str(caught.value)
    assert _source_digests(crashed) == before


def test_a_claim_record_left_in_applying_is_refused(crashed, tmp_path):
    """An earlier apply began and never finished. That is not a fresh claim."""
    before = _source_digests(crashed)
    capability, _host, _clock = _quiesce(crashed, tmp_path)
    _reseal(Path(capability.claim_record_path), state="APPLYING", apply_id="earlier")

    with pytest.raises(wal_recovery.ApplyRecordRejected) as caught:
        _rehearse(crashed, tmp_path, fence=capability)

    assert "APPLYING" in str(caught.value) or "CLAIMED" in str(caught.value)
    assert _source_digests(crashed) == before


def test_a_claim_record_rebound_to_another_lease_is_refused(crashed, tmp_path):
    before = _source_digests(crashed)
    capability, _host, _clock = _quiesce(crashed, tmp_path)
    _reseal(Path(capability.claim_record_path), lease_id="someone-elses-lease")

    with pytest.raises(wal_recovery.FenceRefused) as caught:
        _rehearse(crashed, tmp_path, fence=capability)

    # The fence's own reason survives on __cause__; the recovery does not
    # adopt Docker-shaped exception types as part of its contract.
    assert isinstance(caught.value.__cause__, wal_fence.ClaimUnavailable)
    assert caught.value.report.checkpoint_started is False
    assert _source_digests(crashed) == before


def test_an_apply_that_was_never_written_down_is_refused(crashed, tmp_path):
    """Saying the apply began is not the same as it being durable.

    If the transition never reached disk, another process still sees a free
    claim — and two processes both believing they hold it is the one thing
    this whole mechanism exists to prevent.
    """
    before = _source_digests(crashed)
    capability, _host, _clock = _quiesce(crashed, tmp_path)

    class SaysItBegan(wal_recovery.SourceApplyAuthority):
        def check_before_source_open(self, *, db_path):
            return capability.check_before_source_open(db_path=db_path)

        def begin_apply(self):
            return "an-apply-id-nobody-recorded"

        def complete(self):
            capability.complete()

        def fail(self, reason):
            capability.fail(reason)

    with pytest.raises(wal_recovery.ApplyRecordRejected) as caught:
        _rehearse(crashed, tmp_path, fence=SaysItBegan())

    assert caught.value.report.checkpoint_started is False
    assert _source_digests(crashed) == before


def test_an_apply_id_that_does_not_match_the_record_is_refused(crashed, tmp_path):
    """The transition happened; the id handed back is not the one recorded.

    Separate from the state check on purpose: either one alone would let the
    other's failure through, and then neither is doing any work.
    """
    before = _source_digests(crashed)
    capability, _host, _clock = _quiesce(crashed, tmp_path)

    class RenamesItsOwnApply(wal_recovery.SourceApplyAuthority):
        def check_before_source_open(self, *, db_path):
            return capability.check_before_source_open(db_path=db_path)

        def begin_apply(self):
            capability.begin_apply()  # the record really does say APPLYING
            return "a-different-apply-id"

        def complete(self):
            capability.complete()

        def fail(self, reason):
            capability.fail(reason)

    with pytest.raises(wal_recovery.ApplyRecordRejected) as caught:
        _rehearse(crashed, tmp_path, fence=RenamesItsOwnApply())

    assert "apply" in str(caught.value)
    assert caught.value.report.checkpoint_started is False
    assert _source_digests(crashed) == before


def test_an_apply_recorded_without_the_transition_is_refused(crashed, tmp_path):
    """The id lines up and the state never moved. Still not an apply."""
    before = _source_digests(crashed)
    capability, _host, _clock = _quiesce(crashed, tmp_path)
    _reseal(Path(capability.claim_record_path), apply_id="claimed-but-not-applying")

    class NeverTransitions(wal_recovery.SourceApplyAuthority):
        def check_before_source_open(self, *, db_path):
            return capability.check_before_source_open(db_path=db_path)

        def begin_apply(self):
            return "claimed-but-not-applying"

        def complete(self):
            capability.complete()

        def fail(self, reason):
            capability.fail(reason)

    with pytest.raises(wal_recovery.ApplyRecordRejected) as caught:
        _rehearse(crashed, tmp_path, fence=NeverTransitions())

    assert "APPLYING" in str(caught.value)
    assert caught.value.report.checkpoint_started is False
    assert _source_digests(crashed) == before


def test_a_backup_failure_also_finishes_the_claim(crashed, tmp_path, monkeypatch):
    """The same contract for a failure long before the fence is consulted.

    A run that dies while taking its backup has still taken a claim, and the
    claim is just as stuck if nobody ends it.
    """
    before = _source_digests(crashed)
    fence, host, clock = _quiesce(crashed, tmp_path)
    _refuse_to_fsync(monkeypatch)

    with pytest.raises(wal_recovery.SnapshotFailed) as caught:
        _rehearse(crashed, tmp_path, fence=fence)

    assert caught.value.code == "SNAPSHOT_FAILED"
    assert caught.value.report.checkpoint_started is False
    assert _source_digests(crashed) == before
    assert fence.is_active is False
    assert caught.value.report.claim_outcome == "failed"
    monkeypatch.undo()
    record = wal_fence.release(
        manifest_path=fence.manifest.path, runner=host, clock=clock
    )
    assert set(record.restored) == {AGENT_ID, BACKEND_ID}


def test_a_failure_to_finish_the_claim_blocks_rather_than_pretends(
    crashed, tmp_path
):
    """If the claim cannot be ended, say so and stay blocked.

    Deleting the claim file, or reporting success, would hand the host back
    while nobody knows what state the authorisation is in. Refusing to release
    is the safe answer, and the reason belongs in the report.
    """
    before = _source_digests(crashed)
    capability, host, clock = _quiesce(crashed, tmp_path)
    clock.advance(10_000)

    class CannotFinish(wal_recovery.SourceApplyAuthority):
        def check_before_source_open(self, *, db_path):
            return capability.check_before_source_open(db_path=db_path)

        def begin_apply(self):
            return capability.begin_apply()

        def complete(self):
            capability.complete()

        def fail(self, reason):
            raise OSError(28, "No space left on device")

    with pytest.raises(wal_recovery.FenceRefused) as caught:
        _rehearse(crashed, tmp_path, fence=CannotFinish())

    # The original reason is still the reason.
    assert isinstance(caught.value.__cause__, wal_fence.FenceStale)
    assert _source_digests(crashed) == before
    # And the failure to close it out is recorded rather than swallowed.
    assert "No space left on device" in caught.value.report.claim_spend_error
    # Not recorded as an outcome: nothing was closed out, and saying otherwise
    # would be the report claiming a state the disk does not hold.
    assert caught.value.report.claim_outcome is None
    assert Path(capability.claim_record_path).exists()
    with pytest.raises(wal_fence.ClaimInFlight):
        wal_fence.release(
            manifest_path=capability.manifest.path, runner=host, clock=clock
        )


def test_a_completion_that_fails_is_not_downgraded_to_a_failure(
    crashed, tmp_path
):
    """The recovery finished. If closing the claim fails, say THAT.

    Writing a FAILED record over a run that actually succeeded would tell the
    next person the database was left alone when it was not.
    """
    capability, _host, _clock = _quiesce(crashed, tmp_path)

    class CannotComplete(wal_recovery.SourceApplyAuthority):
        def check_before_source_open(self, *, db_path):
            return capability.check_before_source_open(db_path=db_path)

        def begin_apply(self):
            return capability.begin_apply()

        def complete(self):
            raise OSError(28, "No space left on device")

        def fail(self, reason):
            capability.fail(reason)

    with pytest.raises(wal_recovery.SourceApplyFailed) as caught:
        _rehearse(crashed, tmp_path, fence=CannotComplete())

    report = caught.value.report
    assert report.checkpoint_started is True
    assert "No space left on device" in report.claim_spend_error
    assert report.claim_outcome is None
    # And no terminal record was written behind its back.
    assert not Path(f"{capability.manifest.path}.claim.done.json").exists()


def test_an_interrupt_before_the_source_keeps_the_report_and_ends_the_claim(
    crashed, tmp_path, monkeypatch
):
    """Ctrl-C during the backup: nothing touched, claim closed, report kept."""
    before = _source_digests(crashed)
    capability, host, clock = _quiesce(crashed, tmp_path)
    real_copy = wal_recovery._copy_file

    def interrupt(src, dst):
        real_copy(src, dst)
        raise KeyboardInterrupt()

    monkeypatch.setattr(wal_recovery, "_copy_file", interrupt)

    with pytest.raises(KeyboardInterrupt) as caught:
        _rehearse(crashed, tmp_path, fence=capability)

    report = getattr(caught.value, "recovery_report", None)
    assert report is not None, "the report was lost with the interrupt"
    assert report.checkpoint_started is False
    assert report.claim_outcome == "failed"
    assert _source_digests(crashed) == before
    monkeypatch.undo()
    assert capability.is_active is False
    wal_fence.release(manifest_path=capability.manifest.path, runner=host, clock=clock)


def test_a_lease_that_lapses_mid_apply_does_not_abandon_the_checkpoint(
    crashed, tmp_path
):
    """Once APPLYING has begun, the clock stops being a reason to stop.

    Abandoning a checkpoint half way because a lease lapsed would leave the
    database in exactly the state the whole procedure exists to avoid.
    """
    capability, _host, clock = _quiesce(crashed, tmp_path)
    real_begin = capability.begin_apply

    class LapsesOnceUnderWay(wal_recovery.SourceApplyAuthority):
        def check_before_source_open(self, *, db_path):
            return capability.check_before_source_open(db_path=db_path)

        def begin_apply(self):
            apply_id = real_begin()
            clock.advance(10_000)      # the lease runs out mid-apply
            return apply_id

        def complete(self):
            capability.complete()

        def fail(self, reason):
            capability.fail(reason)

    report = _rehearse(crashed, tmp_path, fence=LapsesOnceUnderWay())

    assert report.source_checkpoint.ok is True
    assert report.source_readback.sentinel_rows == 1
    assert report.claim_outcome == "completed"


# ── 6f. one apply, start to finish ───

def test_the_apply_is_begun_before_the_source_is_opened(crashed, tmp_path):
    capability, _host, _clock = _quiesce(crashed, tmp_path)
    order = []
    real_begin = capability.begin_apply
    real_connect = wal_recovery._connect_rw

    class Watching(wal_recovery.SourceApplyAuthority):
        def check_before_source_open(self, *, db_path):
            order.append("check")
            return capability.check_before_source_open(db_path=db_path)

        def begin_apply(self):
            order.append("begin")
            return real_begin()

        def complete(self):
            order.append("complete")
            capability.complete()

        def fail(self, reason):
            order.append("fail")
            capability.fail(reason)

    report = _rehearse(crashed, tmp_path, fence=Watching())

    assert order == ["check", "begin", "complete"]
    assert report.apply_id
    assert report.claim_outcome == "completed"


def test_a_second_rehearsal_on_the_same_capability_is_refused(crashed, tmp_path):
    capability, _host, _clock = _quiesce(crashed, tmp_path)
    _rehearse(crashed, tmp_path, fence=capability, run_id="run-1")

    with pytest.raises(wal_recovery.WalRecoveryError) as caught:
        _rehearse(crashed, tmp_path, fence=capability, run_id="run-2")

    assert caught.value.report.checkpoint_started is False


def test_a_release_slipped_in_after_the_grant_starts_nothing(crashed, tmp_path):
    """RED B. The window the previous round left open."""
    before = _source_digests(crashed)
    capability, host, clock = _quiesce(crashed, tmp_path)

    class ReleasesAfterGranting(wal_recovery.SourceApplyAuthority):
        def check_before_source_open(self, *, db_path):
            grant = capability.check_before_source_open(db_path=db_path)
            # Somebody runs `release` the instant the grant is issued.
            with contextlib.suppress(wal_fence.WalFenceError):
                wal_fence.release(
                    manifest_path=capability.manifest.path,
                    runner=host,
                    clock=clock,
                    start_containers=True,
                )
            return grant

        def begin_apply(self):
            return capability.begin_apply()

        def complete(self):
            capability.complete()

        def fail(self, reason):
            capability.fail(reason)

    report_or_error = None
    try:
        report_or_error = _rehearse(crashed, tmp_path, fence=ReleasesAfterGranting())
    except wal_recovery.WalRecoveryError as exc:
        report_or_error = exc

    assert not host.ran("start"), "a container was started mid-apply"
    for container in host.containers.values():
        assert container.running is False
        assert container.restart_policy == "no"
    if isinstance(report_or_error, wal_recovery.WalRecoveryError):
        assert _source_digests(crashed) == before


def test_an_interrupt_after_the_checkpoint_spends_the_claim_and_keeps_the_report(
    crashed, tmp_path, monkeypatch
):
    capability, _host, _clock = _quiesce(crashed, tmp_path)
    real = wal_recovery._read_back
    calls = []

    def interrupt_after_the_checkpoint(conn, sentinel, probes):
        calls.append(1)
        if len(calls) == 3:
            raise KeyboardInterrupt()
        return real(conn, sentinel, probes)

    monkeypatch.setattr(wal_recovery, "_read_back", interrupt_after_the_checkpoint)

    with pytest.raises(KeyboardInterrupt) as caught:
        _rehearse(crashed, tmp_path, fence=capability)

    report = getattr(caught.value, "recovery_report", None)
    assert report is not None, "the report was lost with the interrupt"
    assert report.checkpoint_started is True
    assert report.source_checkpoint is not None
    assert capability.is_active is False


def test_the_connection_is_verified_to_have_opened_the_checked_file(crashed, tmp_path):
    capability, _host, _clock = _quiesce(crashed, tmp_path)

    report = _rehearse(crashed, tmp_path, fence=capability)

    assert report.opened_identity is not None
    assert report.opened_identity["inode"] == report.fence_check.db_inode
    assert report.opened_identity["main_file"] == str(crashed.db)


# ── 6g. every failure past a valid authority ends the claim ───

class _Impostor:
    """The right method names, and no provenance whatsoever.

    Nothing here may ever be called. Spending a claim is what lets a fence be
    released, and calling it on an object that never issued one is the
    procedure inventing an authority so it has something to close out.
    """

    def __init__(self):
        self.calls = []

    def check_before_source_open(self, *, db_path):
        self.calls.append("check")

    def begin_apply(self):
        self.calls.append("begin_apply")

    def complete(self):
        self.calls.append("complete")

    def fail(self, reason):
        self.calls.append(("fail", reason))


class _InterruptingSpend(wal_recovery.SourceApplyAuthority):
    """A real authority whose closing-out is itself interrupted."""

    def __init__(self, inner):
        self.inner = inner
        self.attempts = []

    def check_before_source_open(self, *, db_path):
        return self.inner.check_before_source_open(db_path=db_path)

    def begin_apply(self):
        return self.inner.begin_apply()

    def complete(self):
        self.attempts.append("complete")
        raise KeyboardInterrupt("^C while the claim was being closed out")

    def fail(self, reason):
        self.attempts.append(("fail", reason))
        raise KeyboardInterrupt("^C while the claim was being closed out")


def test_an_invalid_run_id_still_ends_the_claim_it_was_given(crashed, tmp_path):
    """RED B2a. Input validation sits outside the claim-ending handler.

    A run refused for its run id has been handed a live, one-shot capability
    over a host whose containers are already stopped. Refusing without closing
    it out leaves a fence nobody can release: `release()` sees a claim in
    flight and the containers stay down until somebody works out by hand what
    happened.
    """
    before = _source_digests(crashed)
    capability, host, clock = _quiesce(crashed, tmp_path)

    with pytest.raises(wal_recovery.BackupRejected) as caught:
        _rehearse(crashed, tmp_path, run_id="../escape", fence=capability)

    assert caught.value.stage == "validate"
    assert caught.value.report is not None
    assert caught.value.report.run_id == "../escape"
    assert caught.value.report.claim_outcome == "failed"
    # Nothing opened the source to find out the run id was unusable.
    assert _source_digests(crashed) == before
    assert capability.is_active is False
    # And the whole point: the fence can now be put back by hand.
    released = wal_fence.release(
        manifest_path=capability.manifest.path, runner=host, clock=clock
    )
    assert released.restored


def test_a_rejected_probe_still_ends_the_claim_it_was_given(crashed, tmp_path):
    """RED B2b. The same boundary, a different input.

    A probe name that is not a plain identifier is refused before any SQL is
    built — and, exactly like the run id, after a capability has already been
    issued against a stopped host.
    """
    before = _source_digests(crashed)
    capability, host, clock = _quiesce(crashed, tmp_path)

    with pytest.raises(wal_recovery.ProbeRejected) as caught:
        _rehearse(
            crashed,
            tmp_path,
            fence=capability,
            probes=(wal_recovery.Probe(table="events; drop table events", id_column="id"),),
        )

    assert caught.value.report is not None
    assert caught.value.report.claim_outcome == "failed"
    assert _source_digests(crashed) == before
    assert capability.is_active is False
    assert wal_fence.release(
        manifest_path=capability.manifest.path, runner=host, clock=clock
    ).restored


def test_a_backup_root_that_is_not_a_directory_still_ends_the_claim(
    crashed, tmp_path
):
    """RED B2c. And again for the destination."""
    capability, host, clock = _quiesce(crashed, tmp_path)
    not_a_directory = tmp_path / "not-a-directory"
    not_a_directory.write_text("")

    with pytest.raises(wal_recovery.BackupRejected) as caught:
        wal_recovery.rehearse_wal_recovery(
            db_path=crashed.db,
            backup_root=not_a_directory,
            run_id="run-1",
            sentinel=SENTINEL,
            probes=PROBES,
            fence=capability,
        )

    assert caught.value.stage == "validate"
    assert caught.value.report is not None
    assert capability.is_active is False
    assert wal_fence.release(
        manifest_path=capability.manifest.path, runner=host, clock=clock
    ).restored


def test_a_missing_sentinel_table_keeps_the_report_that_says_how_far_it_got(
    crashed, tmp_path
):
    """RED B2d. A failure inside the run is not a reason to lose the account.

    The sentinel table name is a perfectly good identifier; it simply is not
    in this database. That is discovered after a verified backup already
    exists, and which stages got that far is precisely what an operator needs.
    """
    capability, host, clock = _quiesce(crashed, tmp_path)

    with pytest.raises(wal_recovery.VerificationFailed) as caught:
        _rehearse(
            crashed,
            tmp_path,
            fence=capability,
            sentinel=wal_recovery.Sentinel(
                table="no_such_table", column="name", value="x"
            ),
        )

    report = caught.value.report
    assert report is not None
    assert "snapshot-verify" in report.stages
    assert report.snapshot_db is not None
    assert report.claim_outcome == "failed"
    assert capability.is_active is False
    assert wal_fence.release(
        manifest_path=capability.manifest.path, runner=host, clock=clock
    ).restored


def test_an_interrupt_while_the_claim_closes_does_not_replace_the_real_failure(
    crashed, tmp_path
):
    """RED B2e. The bookkeeping is not allowed to become the finding.

    `_spend_claim` guards against a failure to close out — but only against
    `Exception`. A ^C landing in the same place unwinds straight past the
    original error, and the operator is told about the interrupt instead of
    about the recovery that failed.
    """
    capability, _, _ = _quiesce(crashed, tmp_path)
    interrupting = _InterruptingSpend(capability)

    with pytest.raises(wal_recovery.VerificationFailed) as caught:
        _rehearse(
            crashed,
            tmp_path,
            fence=interrupting,
            sentinel=wal_recovery.Sentinel(
                table="no_such_table", column="name", value="x"
            ),
        )

    assert interrupting.attempts, "the claim was never closed out at all"
    report = caught.value.report
    assert report is not None
    assert report.claim_outcome is None
    assert "KeyboardInterrupt" in (report.claim_spend_error or "")


def test_an_object_that_is_not_an_authority_is_never_asked_to_spend_a_claim(
    crashed, tmp_path
):
    """RED B2f. The fence is checked FIRST, and an impostor is never touched.

    Moving input validation inside the claim-ending handler must not drag the
    fence check in with it: `fail()` on an object that issued nothing is not
    bookkeeping, it is the procedure talking to something it already refused.
    """
    impostor = _Impostor()

    with pytest.raises(wal_recovery.FenceRequired):
        _rehearse(crashed, tmp_path, run_id="../escape", fence=impostor)

    assert impostor.calls == []

    with pytest.raises(wal_recovery.FenceRequired):
        _rehearse(crashed, tmp_path, fence=impostor)

    assert impostor.calls == []


# ── 6h. the report survives a failure this module never named ───

class Unbindable:
    """A sentinel value SQLite has no idea how to bind.

    Every identifier in the query spec is checked; the VALUE is not, because
    it goes in as a parameter and parameters are not an injection point. What
    it can still be is a type the driver refuses, and that refusal is a plain
    `sqlite3.ProgrammingError` from well inside the run.
    """


UNBINDABLE_SENTINEL = wal_recovery.Sentinel(
    table="events", column="name", value=Unbindable()
)


def test_a_plain_exception_still_carries_the_report_of_how_far_it_got(
    crashed, tmp_path
):
    """RED C3a. A failure this module did not name is still a failure it saw.

    The claim is spent, a verified backup is sitting in the run directory, and
    the source was never opened — and every one of those is something the
    operator has to know before deciding what to do next. A bare driver error
    carries none of it.
    """
    capability, host, clock = _quiesce(crashed, tmp_path)

    with pytest.raises(sqlite3.ProgrammingError) as caught:
        _rehearse(crashed, tmp_path, fence=capability, sentinel=UNBINDABLE_SENTINEL)

    assert "Error binding parameter" in str(caught.value)
    report = getattr(caught.value, "recovery_report", None)
    assert report is not None
    # What the report has to be able to answer.
    assert report.claim_outcome == "failed"
    assert report.run_dir is not None and Path(report.run_dir).is_dir()
    assert report.snapshot_db is not None
    assert report.source_checkpoint is None
    assert report.checkpoint_started is False
    assert capability.is_active is False
    assert wal_fence.release(
        manifest_path=capability.manifest.path, runner=host, clock=clock
    ).restored


def test_a_report_that_cannot_be_attached_does_not_replace_the_failure(
    crashed, tmp_path, monkeypatch
):
    """RED C3b. The bookkeeping is never allowed to become the finding.

    Not every exception object will take an attribute, and a run that failed
    for a real reason must not come back reporting the failure to write down
    how far it got instead.
    """
    capability, host, clock = _quiesce(crashed, tmp_path)

    class Unassignable(Exception):
        """An exception that refuses to carry anything extra."""

        def __setattr__(self, name, value):
            raise AttributeError(f"{name} cannot be set on this exception")

    def blow_up(*args, **kwargs):
        raise Unassignable("the reason the run actually stopped")

    monkeypatch.setattr(wal_recovery, "_count_on_connection", blow_up)

    with pytest.raises(Unassignable) as caught:
        _rehearse(crashed, tmp_path, fence=capability)

    assert str(caught.value) == "the reason the run actually stopped"
    # The claim was still closed out, so the fence is still releasable.
    assert capability.is_active is False
    assert wal_fence.release(
        manifest_path=capability.manifest.path, runner=host, clock=clock
    ).restored


def test_a_capability_for_another_claim_is_refused_and_recorded(
    crashed, tmp_path
):
    """RED C2d. A refusal to close out is recorded, not raised over the top.

    The run fails because the capability it was handed does not bind to the
    claim on disk; closing that same capability out then fails for the same
    reason. What comes back has to be the first refusal, with the second one
    written down beside it.
    """
    real, host, clock = _quiesce(crashed, tmp_path)
    forged = wal_fence.SourceApplyCapability(
        real.manifest,
        "f" * 32,
        runner=host,
        clock=clock,
        authority_verifier=ACK_KEY,
        visibility_witness=ScriptedWitness(host),
    )

    with pytest.raises(wal_recovery.FenceRefused) as caught:
        _rehearse(crashed, tmp_path, fence=forged)

    report = caught.value.report
    assert report is not None
    assert report.claim_outcome is None
    assert "ClaimUnavailable" in (report.claim_spend_error or "")
    # The real claim never moved, and nothing was written at the terminal name.
    assert not Path(f"{real.manifest.path}.claim.done.json").exists()
    assert real.is_active is True


# ── 6i. a second run may not rewrite the first one's outcome ───

def test_a_second_run_over_a_spent_claim_does_not_record_a_new_outcome(
    crashed, tmp_path
):
    """RED D2e. The durable answer is COMPLETED; the second run says FAILED.

    A capability is one-shot, so the second run is refused — correctly. What
    it must not then do is close the claim out again: the record on disk says
    the first run completed, and a `claim_outcome` of "failed" beside it is
    the report contradicting the fence it came from.
    """
    capability, host, clock = _quiesce(crashed, tmp_path)
    first = _rehearse(crashed, tmp_path, run_id="run-1", fence=capability)
    assert first.claim_outcome == "completed"
    done = Path(f"{capability.manifest.path}.claim.done.json")
    before = done.read_text()
    assert json.loads(before)["state"] == wal_recovery.COMPLETED

    with pytest.raises(wal_recovery.WalRecoveryError) as caught:
        _rehearse(crashed, tmp_path, run_id="run-2", fence=capability)

    report = caught.value.report
    assert report is not None
    assert report.claim_outcome is None
    assert "ClaimSpent" in (report.claim_spend_error or "")
    assert report.checkpoint_started is False
    # The durable record never moved.
    assert done.read_text() == before


# ── 6j. attaching the report is bookkeeping, never the finding ───

class _ReportSetterInterrupts(Exception):
    """A plain exception whose annotation slot is itself hostile."""

    def __setattr__(self, name, value):
        if name in ("report", "recovery_report"):
            raise KeyboardInterrupt("^C while the report was being attached")
        super().__setattr__(name, value)


class _RefusalSetterInterrupts(wal_recovery.WalRecoveryError):
    """A typed refusal whose `.report` cannot be written a second time."""

    armed = False

    def __setattr__(self, name, value):
        if name == "report" and self.armed:
            raise KeyboardInterrupt("^C while the report was being attached")
        super().__setattr__(name, value)


class _CancelledSetterInterrupts(BaseException):
    """A cancellation that will not carry the account of what it interrupted."""

    def __setattr__(self, name, value):
        if name == "recovery_report":
            raise KeyboardInterrupt("^C while the report was being attached")
        super().__setattr__(name, value)


def _deepest_frame(exc: BaseException) -> str:
    tb = exc.__traceback__
    while tb.tb_next is not None:
        tb = tb.tb_next
    return tb.tb_frame.f_code.co_name


def raiser_for_the_original_failure(kind):  # noqa: D401 - named to be found in a traceback
    raise kind


def test_an_interrupt_attaching_a_report_does_not_replace_a_plain_failure(
    crashed, tmp_path, monkeypatch
):
    """RED D3a. Annotating the failure is not allowed to become the failure."""
    capability, host, clock = _quiesce(crashed, tmp_path)
    original = _ReportSetterInterrupts("the reason the run actually stopped")

    monkeypatch.setattr(
        wal_recovery,
        "_count_on_connection",
        lambda *a, **k: raiser_for_the_original_failure(original),
    )

    with pytest.raises(_ReportSetterInterrupts) as caught:
        _rehearse(crashed, tmp_path, fence=capability)

    assert caught.value is original
    assert str(caught.value) == "the reason the run actually stopped"
    assert _deepest_frame(caught.value) == "raiser_for_the_original_failure"
    assert capability.is_active is False
    assert wal_fence.release(
        manifest_path=capability.manifest.path, runner=host, clock=clock
    ).restored


def test_an_interrupt_attaching_a_report_does_not_replace_a_typed_refusal(
    crashed, tmp_path, monkeypatch
):
    """RED D3b. The same, for the refusals this module raises itself."""
    capability, host, clock = _quiesce(crashed, tmp_path)
    original = _RefusalSetterInterrupts(
        "the reason the run actually stopped", stage="oracle"
    )
    original.armed = True

    monkeypatch.setattr(
        wal_recovery,
        "_count_on_connection",
        lambda *a, **k: raiser_for_the_original_failure(original),
    )

    with pytest.raises(_RefusalSetterInterrupts) as caught:
        _rehearse(crashed, tmp_path, fence=capability)

    assert caught.value is original
    assert caught.value.stage == "oracle"
    assert str(caught.value) == "the reason the run actually stopped"
    assert _deepest_frame(caught.value) == "raiser_for_the_original_failure"
    assert capability.is_active is False


def test_an_interrupt_attaching_a_report_after_the_apply_keeps_the_original(
    crashed, tmp_path, monkeypatch
):
    """RED D3c. The one place the source has already been written to.

    This is the branch that exists precisely so the account of what happened
    to the original database travels out with whatever is unwinding. Losing
    the cancellation itself to a failure to annotate it is the same mistake
    one layer up.
    """
    capability, host, clock = _quiesce(crashed, tmp_path)
    original = _CancelledSetterInterrupts("the operator cancelled the apply")

    monkeypatch.setattr(
        wal_recovery,
        "_apply_to_source",
        lambda *a, **k: raiser_for_the_original_failure(original),
    )

    with pytest.raises(_CancelledSetterInterrupts) as caught:
        _rehearse(crashed, tmp_path, fence=capability)

    assert caught.value is original
    assert str(caught.value) == "the operator cancelled the apply"
    assert _deepest_frame(caught.value) == "raiser_for_the_original_failure"
    assert capability.is_active is False


# ── 7. the module's own boundaries ───────────────────

def test_the_module_imports_only_the_standard_library(tmp_path):
    tree = ast.parse(MODULE_PY.read_text())
    roots = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.split(".")[0])

    assert "app" not in roots, (
        "recovery must not depend on app.database's global connection or its "
        "runtime lifecycle — it has to run against a database no process owns"
    )
    assert roots <= sys.stdlib_module_names, f"non-stdlib imports: {sorted(roots)}"


def test_the_module_never_deletes_renames_or_rewrites_the_source():
    source = MODULE_PY.read_text()
    tree = ast.parse(source)
    called = {
        ast.unparse(node.func)
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
    }

    forbidden = {
        "os.remove",
        "os.unlink",
        "os.rename",
        "os.replace",
        "shutil.move",
        "shutil.rmtree",
        "Path.unlink",
    }
    assert not (called & forbidden), (
        f"destructive call in the recovery path: {sorted(called & forbidden)}"
    )
    upper = source.upper()
    assert "VACUUM" not in upper
    assert "JOURNAL_MODE=" not in upper.replace(" ", "")
