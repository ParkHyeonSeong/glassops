"""One command that runs the CP-5B fence and the CP-5A recovery in order.

`app.wal_recovery_cli` is the operator's entry point: it quiesces the host,
re-reads the manifest, claims the lease, runs the rehearsal against the
database path the fence derived, checks the terminal outcome, and only then
releases the restart policies. Every stage has its own exit code, and every
failure stops the command where it is — nothing is deleted, nothing is put
back, nothing is retried.

Everything here runs against the scripted host in `conftest.py` and the
disposable crash fixture from `test_wal_recovery.py`. No Docker daemon, no
`/app/data`, no dev9. Passing this file is not an approval to run the command
against a real host.
"""

import ast
import hashlib
import io
import json
import os
import sqlite3
import subprocess
from pathlib import Path

import pytest

import app.wal_fence as wal_fence
import app.wal_recovery as wal_recovery
import app.wal_recovery_cli as cli
from tests.backend.conftest import (
    ACK_KEY,
    AGENT_ID,
    AGENT_SERVICE,
    BACKEND_ID,
    DATA_DESTINATION,
    HOST_ID,
    PROJECT,
    SERVICE,
    FakeContainer,
    FakeDocker,
    OpenFile,
    ScriptedWitness,
    bind_mount,
    external_ack,
)
from tests.backend.test_wal_recovery import PROBES, SENTINEL, Fixture, _build_fixture

MODULE_PY = Path(cli.__file__)
REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def children():
    procs = []
    yield procs
    leaked = [p for p in procs if p.poll() is None]
    for proc in leaked:
        proc.kill()
        proc.wait(timeout=10)
    assert not leaked, f"{len(leaked)} fixture child process(es) outlived the test"


@pytest.fixture
def crashed(tmp_path, children) -> Fixture:
    source = tmp_path / "source"
    source.mkdir()
    return _build_fixture(source, children)


@pytest.fixture
def clock():
    class Clock:
        now = 1_700_000_000.0

        def __call__(self):
            return self.now

        def advance(self, seconds):
            self.now += seconds

    return Clock()


def _host(fixture: Fixture) -> FakeDocker:
    mount = bind_mount(fixture.db.parent, destination=DATA_DESTINATION)
    return FakeDocker(
        [
            FakeContainer(container_id=BACKEND_ID, service=SERVICE, mounts=[mount]),
            FakeContainer(
                container_id=AGENT_ID,
                service=AGENT_SERVICE,
                restart_policy="on-failure",
                restart_max_retries=3,
                mounts=[mount],
            ),
        ]
    )


class Run:
    """Everything one CLI invocation needs, written to disk the way an operator would."""

    def __init__(self, fixture: Fixture, tmp_path: Path, host: FakeDocker, clock):
        self.fixture = fixture
        self.host = host
        self.clock = clock
        self.root = tmp_path
        self.manifest = tmp_path / "fence" / "quiesce-1.json"
        self.backups = tmp_path / "backups"
        self.backups.mkdir()
        self.ack_file = tmp_path / "ack.json"
        self.ack_file.write_text(
            json.dumps(
                external_ack(
                    container_ids=[AGENT_ID, BACKEND_ID], data_dir=fixture.db.parent
                ).as_json()
            )
        )
        self.key_file = tmp_path / "ack.key"
        self.key_file.write_bytes(ACK_KEY)
        self.stdout = io.StringIO()
        self.stderr = io.StringIO()

    def argv(self, **overrides):
        args = {
            "--project": PROJECT,
            "--service": SERVICE,
            "--container-id": BACKEND_ID,
            "--data-destination": DATA_DESTINATION,
            "--db-relpath": self.fixture.db.name,
            "--manifest": str(self.manifest),
            "--host-id": HOST_ID,
            "--ack-file": str(self.ack_file),
            "--ack-key-file": str(self.key_file),
            "--backup-root": str(self.backups),
            "--run-id": "run-1",
            "--sentinel": f"{SENTINEL.table}:{SENTINEL.column}:{SENTINEL.value}",
        }
        args.update(overrides)
        argv = []
        for flag, value in args.items():
            if value is not None:
                argv += [flag, value]
        argv += ["--scope", f"{SERVICE}={BACKEND_ID}", "--scope", f"{AGENT_SERVICE}={AGENT_ID}"]
        argv += ["--probe", f"{PROBES[0].table}:{PROBES[0].id_column}:{PROBES[0].timestamp_column}"]
        return argv

    def main(self, argv=None, *, stdout=None):
        return cli.main(
            self.argv() if argv is None else argv,
            runner=self.host,
            clock=self.clock,
            visibility_witness=ScriptedWitness(self.host),
            stdout=self.stdout if stdout is None else stdout,
            stderr=self.stderr,
        )

    def returned(self, argv=None, *, stdout=None):
        """The exit code — and a failure, not an abort, if anything escaped.

        A `KeyboardInterrupt` that gets past the command would otherwise take
        the whole pytest session down with it, which is not a test result.
        """
        try:
            return self.main(argv, stdout=stdout)
        except BaseException as exc:  # noqa: BLE001 - escaping at all is the failure
            pytest.fail(f"{type(exc).__name__} escaped the command: {exc!r}")

    # -- what the run leaves behind ----------------------------------
    @property
    def prepared(self) -> Path:
        return Path(f"{self.manifest}.prepared.json")

    @property
    def claim(self) -> Path:
        return Path(f"{self.manifest}.claim.json")

    @property
    def done(self) -> Path:
        return Path(f"{self.manifest}.claim.done.json")

    @property
    def release_record(self) -> Path:
        return Path(f"{self.manifest}.release.json")

    @property
    def run_dir(self) -> Path:
        return self.backups / "run-1"


@pytest.fixture
def run(crashed, tmp_path, clock):
    return Run(crashed, tmp_path, _host(crashed), clock)


@pytest.fixture
def stages(monkeypatch):
    """The order the CLI called the four entry points in, and how often."""
    calls = []

    def spy(module, name, label):
        real = getattr(module, name)

        def wrapped(*args, **kwargs):
            calls.append(label)
            return real(*args, **kwargs)

        monkeypatch.setattr(module, name, wrapped)

    spy(wal_fence, "fence", "fence")
    spy(wal_fence.ManifestFence, "claim", "claim")
    spy(wal_recovery, "rehearse_wal_recovery", "recovery")
    spy(wal_fence, "release", "release")
    return calls


def _sha256(path: Path) -> str | None:
    if not path.exists():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _record(path: Path) -> dict:
    return json.loads(path.read_text())


def _sentinel_rows_in_main_file(db: Path) -> int:
    """The sentinel as seen through the database file alone, WAL ignored."""
    conn = sqlite3.connect(f"{db.as_uri()}?mode=ro&immutable=1", uri=True)
    try:
        return conn.execute(
            "SELECT count(*) FROM events WHERE name = ?", (SENTINEL.value,)
        ).fetchone()[0]
    finally:
        conn.close()


def _assert_host_left_fenced(host: FakeDocker):
    """Stopped, pinned to `no`, never started: the fence's failure contract."""
    for container in host.containers.values():
        assert container.running is False
        assert container.status == "exited"
        assert container.restart_policy == "no"
        assert container.sigkilled is False
    assert not host.ran("start")


# ── 1. the whole thing, in order ───


def test_the_command_runs_fence_claim_recovery_terminal_release_in_order(run, stages):
    fixture = run.fixture
    assert _sentinel_rows_in_main_file(fixture.db) == 0

    code = run.main()

    assert code == cli.EXIT_OK, run.stderr.getvalue()
    assert stages == ["fence", "claim", "recovery", "release"]

    # The WAL-only commit is in the database file now, and the backup is there.
    assert _sentinel_rows_in_main_file(fixture.db) == 1
    assert (run.run_dir / "snapshot" / fixture.db.name).exists()

    # Every durable record is where the fence and the recovery leave it.
    assert _record(run.manifest)["state"] == wal_fence.QUIESCED
    assert _record(run.prepared)["state"] == wal_fence.PREPARED
    assert _record(run.claim)["state"] == wal_recovery.COMPLETED
    assert _record(run.done)["state"] == wal_recovery.COMPLETED
    release = _record(run.release_record)
    assert release["state"] == wal_fence.RELEASED
    assert set(release["restored"]) == {BACKEND_ID, AGENT_ID}

    # Restart policies are back; the containers were deliberately left down.
    assert run.host.container(BACKEND_ID).restart_policy == "unless-stopped"
    assert run.host.container(AGENT_ID).restart_policy == "on-failure"
    assert run.host.container(AGENT_ID).restart_max_retries == 3
    assert not run.host.ran("start")
    for container in run.host.containers.values():
        assert container.running is False
        assert container.sigkilled is False

    out = run.stdout.getvalue()
    order = [out.index(f"[{stage}]") for stage in ("fence", "claim", "recovery", "terminal", "release")]
    assert order == sorted(order), out
    assert run.stderr.getvalue() == ""


def test_relative_paths_are_bound_into_the_records_as_absolute_ones(run, stages, monkeypatch):
    # The manifest path is written into every claim record and compared by
    # string; a cwd-relative spelling would bind the fence to whatever
    # directory the operator happened to be in.
    monkeypatch.chdir(run.root)

    code = run.returned(
        run.argv(**{"--manifest": "fence/quiesce-1.json", "--backup-root": "backups"})
    )

    assert code == cli.EXIT_OK, run.stderr.getvalue()
    assert _record(run.claim)["manifest_path"] == str(run.manifest)
    assert (run.run_dir / "snapshot" / run.fixture.db.name).exists()


# ── 2. each stage fails with its own code, and stops there ───


def test_a_fence_refusal_exits_with_the_fence_code_and_leaves_the_host_fenced(
    run, stages
):
    # Something on the host still holds the database open after the stop.
    run.host.open_files.append(OpenFile(pid=31337, fd=7, path=str(run.fixture.db)))
    before = (_sha256(run.fixture.db), _sha256(run.fixture.wal))

    code = run.main()

    assert code == cli.EXIT_FENCE
    err = run.stderr.getvalue()
    assert "stage fence" in err and "DESCRIPTORS_OPEN" in err, err
    assert stages == ["fence"]

    # The refusal came after the stop, so the host stays exactly as it is:
    # stopped and pinned, with the PREPARED record that a hand release needs.
    _assert_host_left_fenced(run.host)
    assert _record(run.prepared)["state"] == wal_fence.PREPARED
    assert not run.manifest.exists()
    assert not run.claim.exists()
    assert not run.run_dir.exists()
    assert not run.release_record.exists()
    assert (_sha256(run.fixture.db), _sha256(run.fixture.wal)) == before
    assert str(run.prepared) in err


def test_a_manifest_that_does_not_read_back_from_disk_is_a_fence_failure(
    run, stages, monkeypatch
):
    # The quiesce returned, but the record it wrote cannot be read back. The
    # claim is taken against the file, not the object, so the command stops.
    def torn(path):
        raise wal_fence.ManifestRejected(f"{path!r} is torn", stage="record")

    monkeypatch.setattr(wal_fence, "read_manifest", torn)

    code = run.main()

    assert code == cli.EXIT_FENCE
    err = run.stderr.getvalue()
    assert "stage fence" in err and "MANIFEST_REJECTED" in err and "torn" in err, err
    assert stages == ["fence"]
    _assert_host_left_fenced(run.host)
    assert run.manifest.exists()
    assert not run.claim.exists()
    assert not run.run_dir.exists()


def test_a_claim_refusal_exits_with_the_claim_code_and_keeps_the_stale_record(
    run, stages
):
    # A claim record from some earlier run is already sitting at this name.
    run.claim.parent.mkdir(parents=True)
    run.claim.write_bytes(b"{not a record this fence wrote}\n")
    stale = _sha256(run.claim)

    code = run.main()

    assert code == cli.EXIT_CLAIM
    err = run.stderr.getvalue()
    assert "stage claim" in err and "CLAIM_UNAVAILABLE" in err, err
    assert stages == ["fence", "claim"]

    _assert_host_left_fenced(run.host)
    assert _record(run.manifest)["state"] == wal_fence.QUIESCED
    assert _sha256(run.claim) == stale, "the stale claim record was rewritten or removed"
    assert not run.done.exists()
    assert not run.run_dir.exists()
    assert not run.release_record.exists()


def test_a_recovery_failure_exits_with_the_recovery_code_and_keeps_the_failed_claim(
    tmp_path, children, clock, stages
):
    # The commit the operator names was already checkpointed into the database
    # file; the oracle refuses it — after the backup has been taken.
    source = tmp_path / "source"
    source.mkdir()
    fixture = _build_fixture(source, children, mode="checkpointed")
    run = Run(fixture, tmp_path, _host(fixture), clock)
    before = (_sha256(fixture.db), _sha256(fixture.wal))

    code = run.main()

    assert code == cli.EXIT_RECOVERY
    err = run.stderr.getvalue()
    assert "stage recovery" in err and "ORACLE_INVALID" in err, err
    assert "source_touched=False" in err, err
    assert stages == ["fence", "claim", "recovery"]

    # The claim was spent as FAILED by the recovery; release was NOT run.
    _assert_host_left_fenced(run.host)
    assert (_sha256(fixture.db), _sha256(fixture.wal)) == before
    assert _record(run.manifest)["state"] == wal_fence.QUIESCED
    assert _record(run.claim)["state"] == wal_recovery.FAILED
    assert _record(run.done)["state"] == wal_recovery.FAILED
    assert (run.run_dir / "snapshot" / fixture.db.name).exists()
    assert not run.release_record.exists()
    assert len(run.host.ran("update")) == 2, "release restored a restart policy"


def test_a_terminal_record_that_cannot_be_written_exits_with_the_terminal_code(
    run, stages, monkeypatch
):
    real_write = wal_fence._write_record

    def disk_full_for_terminal(path, data, **kwargs):
        if str(path).endswith(".claim.done.json"):
            raise OSError(28, "No space left on device")
        return real_write(path, data, **kwargs)

    monkeypatch.setattr(wal_fence, "_write_record", disk_full_for_terminal)

    code = run.main()

    assert code == cli.EXIT_TERMINAL
    err = run.stderr.getvalue()
    assert "stage terminal" in err and "No space left on device" in err, err
    # The recovery itself finished; the operator has to be told that plainly,
    # and told that the way out is to record the outcome, not to release.
    assert "source_touched=True" in err, err
    assert "reattach" in err and "complete()" in err, err
    assert stages == ["fence", "claim", "recovery"]
    assert _sentinel_rows_in_main_file(run.fixture.db) == 1

    # The claim is still live, no terminal record exists, and release was not
    # attempted — and would refuse if it were.
    _assert_host_left_fenced(run.host)
    assert _record(run.claim)["state"] == wal_recovery.APPLYING
    assert not run.done.exists()
    assert not run.release_record.exists()
    assert len(run.host.ran("update")) == 2
    monkeypatch.undo()
    with pytest.raises(wal_fence.ClaimInFlight):
        wal_fence.release(manifest_path=str(run.manifest), runner=run.host, clock=run.clock)


def test_a_report_the_disk_disagrees_with_is_not_released(run, stages, monkeypatch):
    # The recovery says the claim was completed, but the capability still
    # reads as live: whatever happened to the terminal record, the restart
    # policies do not move on the strength of a report alone.
    monkeypatch.setattr(
        wal_fence.SourceApplyCapability, "is_active", property(lambda self: True)
    )

    code = run.main()

    assert code == cli.EXIT_TERMINAL
    err = run.stderr.getvalue()
    assert "stage terminal" in err and "still active" in err, err
    assert stages == ["fence", "claim", "recovery"]
    assert not run.release_record.exists()
    assert len(run.host.ran("update")) == 2
    _assert_host_left_fenced(run.host)


def test_a_release_failure_exits_with_the_release_code_and_keeps_the_completed_run(
    run, stages
):
    # The host acknowledges every restore but never applies one: the fence's
    # own `--restart=no` updates go through, release's read back wrong.
    real_update = run.host._update

    def acknowledge_without_applying(argv):
        if "--restart=no" in argv:
            return real_update(argv)
        return 0, argv[-1] + "\n", ""

    run.host.overrides["update"] = acknowledge_without_applying

    code = run.main()

    assert code == cli.EXIT_RELEASE
    err = run.stderr.getvalue()
    assert "stage release" in err and "RELEASE_FAILED" in err, err
    assert "already restored: none" in err, err
    assert stages == ["fence", "claim", "recovery", "release"]

    # The recovery stands: WAL folded in, COMPLETED on disk, nothing undone.
    assert _sentinel_rows_in_main_file(run.fixture.db) == 1
    assert _record(run.done)["state"] == wal_recovery.COMPLETED
    assert _record(run.claim)["state"] == wal_recovery.COMPLETED
    assert _record(run.manifest)["state"] == wal_fence.QUIESCED
    assert (run.run_dir / "snapshot" / run.fixture.db.name).exists()
    assert not run.release_record.exists()
    _assert_host_left_fenced(run.host)
    assert "[recovery]" in run.stdout.getvalue()


def test_a_release_that_fails_partway_says_which_policies_are_already_back(run, stages):
    # Release restores one container at a time, in container-id order: the
    # agent ("aaa…") goes back first, then the backend's update is silently
    # ignored. "Nothing was put back" would be a lie here; the operator has
    # to be told exactly which policy already moved.
    real_update = run.host._update

    def backend_never_applies(argv):
        if "--restart=no" in argv or argv[-1] == AGENT_ID:
            return real_update(argv)
        return 0, argv[-1] + "\n", ""

    run.host.overrides["update"] = backend_never_applies

    code = run.main()

    assert code == cli.EXIT_RELEASE
    err = run.stderr.getvalue()
    assert "stage release" in err and "RELEASE_FAILED" in err, err
    assert f"already restored: {AGENT_ID}" in err, err
    assert "put back" not in err, err
    assert stages == ["fence", "claim", "recovery", "release"]

    # The host is exactly as release left it: one policy back, one pinned.
    assert run.host.container(AGENT_ID).restart_policy == "on-failure"
    assert run.host.container(AGENT_ID).restart_max_retries == 3
    assert run.host.container(BACKEND_ID).restart_policy == "no"
    assert not run.host.ran("start")
    assert not run.release_record.exists()
    assert _record(run.done)["state"] == wal_recovery.COMPLETED


def test_a_host_error_mid_release_reports_the_restored_set_as_unknown(run, stages):
    # The daemon rejects the restore outright. `release()` cannot say how far
    # it got on a refusal it did not raise itself, and neither may the CLI.
    real_update = run.host._update

    def daemon_refuses(argv):
        if "--restart=no" in argv:
            return real_update(argv)
        return 1, "", "Error response from daemon: something went wrong"

    run.host.overrides["update"] = daemon_refuses

    code = run.main()

    assert code == cli.EXIT_RELEASE
    err = run.stderr.getvalue()
    assert "stage release" in err and "DOCKER_COMMAND_FAILED" in err, err
    assert "already restored: unknown" in err, err
    assert "put back" not in err, err
    assert stages == ["fence", "claim", "recovery", "release"]
    assert not run.host.ran("start")
    assert not run.release_record.exists()


# ── 3. interrupts and output failures after the host has been changed ───
#
# From the first `docker update` on, the command's exit code and its stderr
# are the only account the operator gets. A traceback is not an account.


class _FailingStdout:
    """A stdout that fails on the first line matching `marker`, with `error`.

    Everything before the marker is kept, so a test can also check that the
    stages before the failing one were reported normally.
    """

    def __init__(self, marker: str, error: BaseException):
        self.marker = marker
        self.error = error
        self.lines = []

    def write(self, text):
        if self.marker in text:
            raise self.error
        self.lines.append(text)

    def flush(self):
        pass


def test_an_interrupt_during_the_recovery_exits_with_the_recovery_code(
    run, stages, monkeypatch
):
    real_copy = wal_recovery._copy_file

    def interrupt(src, dst):
        real_copy(src, dst)
        raise KeyboardInterrupt()

    monkeypatch.setattr(wal_recovery, "_copy_file", interrupt)

    code = run.returned()

    assert code == cli.EXIT_RECOVERY
    err = run.stderr.getvalue()
    assert "stage recovery" in err and "KeyboardInterrupt" in err, err
    # The recovery's own account travels with the interrupt, and so it is here.
    assert "source_touched=False" in err and "claim_outcome=failed" in err, err
    assert stages == ["fence", "claim", "recovery"]
    # The recovery spent the claim on its way out; release was not attempted.
    assert _record(run.done)["state"] == wal_recovery.FAILED
    assert len(run.host.ran("update")) == 2
    assert not run.release_record.exists()
    _assert_host_left_fenced(run.host)


def test_an_interrupt_during_the_release_exits_with_the_release_code(run, stages):
    real_update = run.host._update

    def interrupted_on_the_second_restore(argv):
        if "--restart=no" in argv or argv[-1] == AGENT_ID:
            return real_update(argv)
        raise KeyboardInterrupt()

    run.host.overrides["update"] = interrupted_on_the_second_restore

    code = run.returned()

    assert code == cli.EXIT_RELEASE
    err = run.stderr.getvalue()
    assert "stage release" in err and "KeyboardInterrupt" in err, err
    # An interrupt carries no account of how far release got, and the command
    # may not guess: unknown is the honest word.
    assert "already restored: unknown" in err, err
    assert stages == ["fence", "claim", "recovery", "release"]
    assert run.host.container(AGENT_ID).restart_policy == "on-failure"
    assert run.host.container(BACKEND_ID).restart_policy == "no"
    assert not run.host.ran("start")
    assert not run.release_record.exists()
    assert _record(run.done)["state"] == wal_recovery.COMPLETED


def test_a_broken_stdout_right_after_the_fence_exits_with_the_fence_code(run, stages):
    stdout = _FailingStdout("[fence]", BrokenPipeError(32, "Broken pipe"))

    code = run.returned(stdout=stdout)

    assert code == cli.EXIT_FENCE
    err = run.stderr.getvalue()
    assert "stage fence" in err and "BrokenPipeError" in err, err
    # The fence itself finished: its records are on disk and listed.
    assert str(run.manifest) in err and str(run.prepared) in err, err
    assert stages == ["fence"]
    assert not run.claim.exists()
    assert not run.run_dir.exists()
    assert not run.release_record.exists()
    _assert_host_left_fenced(run.host)


def test_a_broken_stdout_right_after_the_claim_says_the_claim_is_live(run, stages):
    stdout = _FailingStdout("[claim]", BrokenPipeError(32, "Broken pipe"))

    code = run.returned(stdout=stdout)

    assert code == cli.EXIT_CLAIM
    err = run.stderr.getvalue()
    assert "stage claim" in err and "BrokenPipeError" in err, err
    # The claim WAS taken. Telling the operator to release would send them
    # into ClaimInFlight; they have to reattach it and record its outcome.
    assert "reattach" in err, err
    assert stages == ["fence", "claim"]
    assert _record(run.claim)["state"] == wal_recovery.CLAIMED
    assert not run.run_dir.exists()
    assert not run.release_record.exists()
    _assert_host_left_fenced(run.host)


@pytest.mark.parametrize("where", ["verification", "output"])
def test_an_interrupt_in_the_terminal_stage_exits_with_the_terminal_code(
    run, stages, monkeypatch, where
):
    stdout = run.stdout
    if where == "verification":

        def interrupted(self):
            raise KeyboardInterrupt()

        monkeypatch.setattr(
            wal_fence.SourceApplyCapability, "is_active", property(interrupted)
        )
    else:
        stdout = _FailingStdout("[terminal]", KeyboardInterrupt())

    code = run.returned(stdout=stdout)

    assert code == cli.EXIT_TERMINAL
    err = run.stderr.getvalue()
    assert "stage terminal" in err and "KeyboardInterrupt" in err, err
    assert stages == ["fence", "claim", "recovery"]
    # Durable records exactly as the recovery left them; nothing released.
    assert _record(run.done)["state"] == wal_recovery.COMPLETED
    assert _record(run.claim)["state"] == wal_recovery.COMPLETED
    assert len(run.host.ran("update")) == 2
    assert not run.release_record.exists()
    _assert_host_left_fenced(run.host)


def test_a_broken_stdout_after_the_release_reports_the_release_as_done(run, stages):
    stdout = _FailingStdout("[release]", BrokenPipeError(32, "Broken pipe"))

    code = run.returned(stdout=stdout)

    assert code == cli.EXIT_RELEASE
    err = run.stderr.getvalue()
    assert "stage release" in err and "BrokenPipeError" in err, err
    # Release completed and its record is on disk: say so, and do not send
    # the operator off to inspect a host that is already back.
    assert "release completed" in err and str(run.release_record) in err, err
    assert "unknown" not in err, err
    assert _record(run.release_record)["state"] == wal_fence.RELEASED
    assert run.host.container(AGENT_ID).restart_policy == "on-failure"
    assert run.host.container(BACKEND_ID).restart_policy == "unless-stopped"
    assert not run.host.ran("start")


# ── 4. what the command must never do ───


def _bad_inputs(run):
    """Each entry has exactly one thing wrong with it, so each guard is the
    one that has to refuse."""
    empty_key = run.key_file.with_name("empty.key")
    empty_key.write_bytes(b"")
    half_ack = run.root / "half.json"
    half_ack.write_text(json.dumps({"issuer": "x"}))
    (run.backups / "taken").mkdir()
    return [
        ([], "required"),
        (run.argv() + ["--scope", "agent"], "--scope needs SERVICE=CONTAINER_ID"),
        (run.argv(**{"--sentinel": "events:name"}), "--sentinel needs TABLE:COLUMN:VALUE"),
        (run.argv() + ["--probe", "a:b:c:d"], "--probe needs TABLE"),
        (run.argv(**{"--ack-file": str(run.fixture.db)}), "--ack-file"),
        (run.argv(**{"--ack-file": str(half_ack)}), "--ack-file"),
        (run.argv(**{"--ack-key-file": str(empty_key)}), "is empty"),
        (run.argv(**{"--backup-root": str(run.backups / "nope")}), "not an existing directory"),
        (run.argv(**{"--run-id": "taken"}), "already has a directory"),
    ]


def test_a_usage_error_touches_nothing_on_the_host(run, stages):
    for argv, expected in _bad_inputs(run):
        already = len(run.stderr.getvalue())
        assert run.main(argv) == cli.EXIT_USAGE, expected
        assert expected in run.stderr.getvalue()[already:], expected
    assert run.host.calls == []
    assert stages == []
    assert not run.manifest.parent.exists()


def _documented_command(cwd: Path):
    """Exactly what the docstring tells the operator to type, from `cwd`.

    No PYTHONPATH help: the environment is the operator's shell, minus the
    one variable a test could use to make a broken command look fine.
    """
    env = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
    return subprocess.run(
        ["python3", "-m", "app.wal_recovery_cli", "--help"],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )


def test_the_documented_command_runs_from_backend_and_nowhere_else():
    assert "cd backend && python3 -m app.wal_recovery_cli" in cli.__doc__

    from_backend = _documented_command(REPO_ROOT / "backend")
    assert from_backend.returncode == 0, from_backend.stderr
    assert "--manifest" in from_backend.stdout
    assert "cd backend && python3 -m app.wal_recovery_cli" in from_backend.stdout

    # The same words from the repository root are the ModuleNotFoundError the
    # operator would otherwise meet; the docstring has to be that specific.
    from_root = _documented_command(REPO_ROOT)
    assert from_root.returncode != 0
    assert "No module named" in from_root.stderr


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"--run-id": "../escape"}, "run id must be a plain name"),
        ({"--run-id": "run 1"}, "run id must be a plain name"),
        ({"--sentinel": "ev ents:name:v"}, "sentinel table is not a plain identifier"),
        ({"--sentinel": "events:na-me:v"}, "sentinel column is not a plain identifier"),
        ({"--probe": "events:id:ts-x"}, "probe timestamp column is not a plain identifier"),
        ({"--probe": "1events"}, "probe table is not a plain identifier"),
        ({"--backup-root": "SYMLINK"}, "backup root is a symlink"),
        ({"--lease-ttl-seconds": "0"}, "--lease-ttl-seconds"),
        ({"--lease-ttl-seconds": "-1"}, "--lease-ttl-seconds"),
        ({"--stop-deadline-seconds": "0"}, "--stop-deadline-seconds"),
        ({"--stop-deadline-seconds": "-5"}, "--stop-deadline-seconds"),
        ({"--stop-deadline-seconds": "nan"}, "--stop-deadline-seconds"),
        ({"--stop-deadline-seconds": "inf"}, "--stop-deadline-seconds"),
        ({"--stop-deadline-seconds": "-inf"}, "--stop-deadline-seconds"),
        ({"--stop-deadline-seconds": "1e999"}, "--stop-deadline-seconds"),
    ],
)
def test_an_input_the_recovery_would_refuse_is_refused_before_the_host_is_touched(
    run, stages, overrides, expected
):
    """Every rule here is one `rehearse_wal_recovery` (or the fence) already
    enforces. Applied only there, it fires after the containers are stopped,
    so a typo costs a quiesce, a FAILED claim, and a hand release."""
    if overrides.get("--backup-root") == "SYMLINK":
        link = run.root / "backups-link"
        link.symlink_to(run.backups)
        overrides = {"--backup-root": str(link)}
    argv = run.argv()
    for flag, value in overrides.items():
        if flag in argv:
            argv[argv.index(flag) + 1] = value
        elif value.startswith("-"):
            # `--flag=-inf`: the joined form is the only way argparse takes a
            # value that looks like an option, so the validator actually sees it.
            argv.append(f"{flag}={value}")
        else:
            argv += [flag, value]

    code = run.returned(argv)

    assert code == cli.EXIT_USAGE, run.stderr.getvalue()
    assert expected in run.stderr.getvalue(), run.stderr.getvalue()
    assert run.host.calls == [], "a host command was issued for a bad input"
    assert stages == []
    assert not run.manifest.parent.exists()
    assert not run.run_dir.exists()


def test_the_command_never_deletes_renames_or_copies_anything():
    """Retries are ruled out behaviourally above (`stages`); this rules out the
    other two prohibitions statically: no removal, no restore-by-copy."""
    tree = ast.parse(MODULE_PY.read_text())
    called = {ast.unparse(node.func) for node in ast.walk(tree) if isinstance(node, ast.Call)}
    forbidden = {
        "os.remove",
        "os.unlink",
        "os.rename",
        "os.replace",
        "os.rmdir",
        "shutil.move",
        "shutil.rmtree",
        "shutil.copy",
        "shutil.copyfile",
        "shutil.copy2",
        "Path.unlink",
    }
    assert not (called & forbidden), sorted(called & forbidden)
