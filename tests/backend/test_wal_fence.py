"""The host-side fence that must hold before CP-5A touches a real database.

CP-5A proves it can fold a WAL back into a database without losing a commit.
It cannot prove that nothing else is writing to that database at the same
time — and a checkpoint run against a file a live backend still has open is
how a "successful recovery" ends up losing data anyway.

This is the fence: a wrapper over the host's own tools that refuses to hand
CP-5A a green light unless it has, in this order,

  * pinned the target down to one exact Compose project, service and full
    container id, with no ambiguity about which container is meant,
  * bound `/app/data` to exactly one read-write mount and derived the database
    path from THAT, rather than trusting a path someone typed,
  * written the existing restart policies down before changing them,
  * set every scoped container's restart policy to `no` and read it back,
  * stopped them gracefully and confirmed each one reached `exited` — never
    killing, never tearing the project down, never touching a volume,
  * confirmed from the host, independently of Docker, that no process holds a
    descriptor on the database or its WAL,
  * and recorded all of it, with the database's identity, in a QUIESCED
    manifest carrying a lease with an expiry.

Every test here runs against the scripted host in `conftest.py`. No Docker
daemon, no Compose project, no `/app/data`, no dev9. Passing this file is not
an approval to run the fence against a real host.
"""

import ast
import contextlib
import errno
import hashlib
import json
import os
import shutil
import stat
import sys
from pathlib import Path

import pytest

import app.wal_fence as wal_fence
import app.wal_recovery as wal_recovery
from tests.backend.conftest import (
    AGENT_ID,
    AGENT_SERVICE,
    BACKEND_ID,
    DATA_DESTINATION,
    DB_RELPATH,
    DOCKER_SOCKET,
    IMAGE_ID,
    OTHER_PROJECT_ID,
    PROJECT,
    SERVICE,
    SOCKET_PROXY_ID,
    UNLABELED_ID,
    FakeContainer,
    FakeDocker,
    OpenFile,
    ACK_KEY,
    HOST_ID,
    ScriptedWitness,
    bind_mount,
    external_ack,
    fence_kwargs,
    mount_identity_of,
    scope_digest,
    scope_of,
    signed_ack,
    socket_mount,
)

MODULE_PY = Path(wal_fence.__file__)

DB_BYTES = b"SQLite format 3\x00" + b"\x11" * 4080
WAL_BYTES = b"\x37\x7f\x06\x82" + b"\x22" * 4148


@pytest.fixture
def database(data_dir):
    """The bytes the fence is being asked to protect."""
    db = data_dir / DB_RELPATH
    db.write_bytes(DB_BYTES)
    wal = data_dir / f"{DB_RELPATH}-wal"
    wal.write_bytes(WAL_BYTES)
    return db


@pytest.fixture
def clock():
    class Clock:
        now = 1_700_000_000.0

        def __call__(self):
            return self.now

        def advance(self, seconds):
            self.now += seconds

    return Clock()


def _fence(fake, tmp_path, clock, **overrides):
    return wal_fence.fence(**fence_kwargs(fake, tmp_path, clock=clock, **overrides))


def _guard(manifest, fake, clock):
    """A fence bound to this scripted host, with a verifier and a witness."""
    return wal_fence.ManifestFence(
        manifest,
        runner=fake,
        clock=clock,
        authority_verifier=ACK_KEY,
        visibility_witness=ScriptedWitness(fake),
    )


# ── 1. commands this wrapper is not allowed to issue ───

def _refusing_runner(calls):
    def runner(command):
        calls.append(command)
        raise AssertionError("the runner must never see a refused command")

    return runner


@pytest.mark.parametrize(
    "argv",
    [
        ["docker", "kill", BACKEND_ID],
        ["docker", "compose", "down"],
        ["docker", "compose", "-p", PROJECT, "down", "--volumes"],
        ["docker", "volume", "rm", "glassops_data"],
        ["docker", "volume", "prune", "--force"],
        ["docker", "rm", "--force", BACKEND_ID],
        ["docker", "system", "prune", "--all"],
    ],
)
def test_a_destructive_command_is_refused_before_it_reaches_the_host(argv):
    calls = []

    with pytest.raises(wal_fence.ForbiddenCommand) as caught:
        wal_fence.run_host_command(_refusing_runner(calls), argv)

    assert caught.value.code == "FORBIDDEN_COMMAND"
    # Named as destructive, not merely unrecognised: whoever reads this needs
    # to know the wrapper refused on purpose, not for want of a feature.
    assert "destructive" in str(caught.value)
    assert calls == []


@pytest.mark.parametrize(
    "argv",
    [
        # Not destructive — just not this wrapper's business. `docker top`
        # in particular is the daemon's answer about its own containers, and
        # the fence deliberately asks the host instead.
        ["docker", "top", BACKEND_ID],
        ["docker", "exec", BACKEND_ID, "sh"],
        ["docker", "logs", BACKEND_ID],
        ["docker", "cp", BACKEND_ID + ":/app/data", "/tmp/x"],
        ["systemctl", "stop", "docker"],
        ["fuser", "-k", "/app/data"],
    ],
)
def test_a_command_outside_the_vocabulary_is_refused(argv):
    calls = []

    with pytest.raises(wal_fence.ForbiddenCommand) as caught:
        wal_fence.run_host_command(_refusing_runner(calls), argv)

    assert "destructive" not in str(caught.value)
    assert calls == []


def test_a_successful_fence_issues_no_destructive_command(
    fake_docker, tmp_path, clock, database
):
    _fence(fake_docker, tmp_path, clock)

    flat = {token for argv in fake_docker.calls for token in argv}
    assert "kill" not in flat
    assert "down" not in flat
    assert "prune" not in flat
    assert "rm" not in flat
    assert not fake_docker.ran("volume")


# ── 2. pinning the target ────────────────────────────

def test_a_short_container_id_is_refused(fake_docker, tmp_path, clock, database):
    with pytest.raises(wal_fence.TargetRejected) as caught:
        _fence(fake_docker, tmp_path, clock, container_id=BACKEND_ID[:12])

    assert caught.value.code == "TARGET_REJECTED"
    # A short id is a prefix, and a prefix can start matching a second
    # container tomorrow. Nothing may run against a target that loose.
    assert fake_docker.calls == []


def test_a_project_and_service_matching_two_containers_is_ambiguous(
    data_dir, tmp_path, clock, database
):
    twin = FakeContainer(
        container_id="e" * 64, service=SERVICE, mounts=[bind_mount(data_dir)]
    )
    fake = FakeDocker(
        [
            FakeContainer(
                container_id=BACKEND_ID, service=SERVICE, mounts=[bind_mount(data_dir)]
            ),
            twin,
        ]
    )

    with pytest.raises(wal_fence.AmbiguousTarget) as caught:
        wal_fence.fence(**fence_kwargs(fake, tmp_path, clock=clock, scope=scope_of(fake)))

    assert caught.value.code == "AMBIGUOUS_TARGET"
    assert not fake.ran("update")
    assert not fake.ran("stop")


def test_a_project_and_service_matching_nothing_is_refused(
    fake_docker, tmp_path, clock, database
):
    with pytest.raises(wal_fence.TargetNotFound):
        _fence(fake_docker, tmp_path, clock, service="does-not-exist")


def test_a_container_id_compose_does_not_report_is_refused(
    fake_docker, tmp_path, clock, database
):
    """Compose names one container; the operator named another.

    The one the operator named has perfectly good labels, so nothing later in
    the chain would object. Only comparing the two answers catches it — and a
    fence built around the wrong container is no fence at all.
    """
    fake_docker._ps = lambda argv: (0, AGENT_ID + "\n", "")

    with pytest.raises(wal_fence.AmbiguousTarget) as caught:
        _fence(fake_docker, tmp_path, clock, container_id=BACKEND_ID)

    assert AGENT_ID in str(caught.value)
    assert BACKEND_ID in str(caught.value)


def test_a_container_whose_labels_disagree_with_the_target_is_refused(
    fake_docker, tmp_path, clock, database
):
    # Compose reports it, but the container itself claims another project.
    fake_docker.container(BACKEND_ID).project = PROJECT
    original = fake_docker._ps

    def lying_ps(argv):
        return 0, BACKEND_ID + "\n", ""

    fake_docker._ps = lying_ps
    fake_docker.container(BACKEND_ID).service = "something-else"

    with pytest.raises(wal_fence.AmbiguousTarget):
        _fence(fake_docker, tmp_path, clock)

    fake_docker._ps = original


# ── 3. binding /app/data to real bytes ───────────────

def test_the_database_path_is_derived_from_the_mount_not_from_the_caller(
    fake_docker, tmp_path, clock, database, data_dir
):
    pre = wal_fence.preflight(
        **{
            k: v
            for k, v in fence_kwargs(fake_docker, tmp_path).items()
            if k not in {"manifest_path", "visibility_witness"}
        },
        now=clock.now,
    )

    assert pre.mount.source == str(data_dir)
    assert pre.mount.rw is True
    assert pre.db_path == str(data_dir / DB_RELPATH)
    assert pre.wal_path == str(data_dir / f"{DB_RELPATH}-wal")


def test_a_missing_data_mount_is_refused(fake_docker, tmp_path, clock, database):
    fake_docker.container(BACKEND_ID).mounts = []

    with pytest.raises(wal_fence.MountNotBound) as caught:
        _fence(fake_docker, tmp_path, clock)

    assert caught.value.code == "MOUNT_NOT_BOUND"
    assert not fake_docker.ran("update")


def test_two_mounts_on_the_data_destination_are_refused(
    fake_docker, tmp_path, clock, database, data_dir
):
    other = tmp_path / "other-data"
    other.mkdir()
    fake_docker.container(BACKEND_ID).mounts = [
        bind_mount(data_dir),
        bind_mount(other),
    ]

    with pytest.raises(wal_fence.MountNotBound) as caught:
        _fence(fake_docker, tmp_path, clock)

    assert "exactly one" in str(caught.value)
    assert not fake_docker.ran("update")


def test_a_mount_overlapping_the_data_destination_is_refused(
    fake_docker, tmp_path, clock, database, data_dir
):
    # /app is mounted too, so which host bytes back /app/data is a question
    # with two answers.
    outer = tmp_path / "outer"
    outer.mkdir()
    fake_docker.container(BACKEND_ID).mounts = [
        bind_mount(data_dir),
        bind_mount(outer, destination="/app"),
    ]

    with pytest.raises(wal_fence.MountNotBound) as caught:
        _fence(fake_docker, tmp_path, clock)

    assert "overlap" in str(caught.value)


def test_a_read_only_data_mount_is_refused(fake_docker, tmp_path, clock, database, data_dir):
    fake_docker.container(BACKEND_ID).mounts = [bind_mount(data_dir, rw=False)]

    with pytest.raises(wal_fence.MountNotBound) as caught:
        _fence(fake_docker, tmp_path, clock)

    assert "read-write" in str(caught.value)


def test_a_database_that_is_not_on_the_mount_is_refused(
    fake_docker, tmp_path, clock, data_dir
):
    # The mount is bound, but nothing is there to recover.
    with pytest.raises(wal_fence.MountNotBound):
        _fence(fake_docker, tmp_path, clock)


def test_a_database_relative_path_that_escapes_the_mount_is_refused(
    fake_docker, tmp_path, clock, database
):
    with pytest.raises(wal_fence.TargetRejected):
        _fence(fake_docker, tmp_path, clock, db_relpath="../escape.db")


# ── 4. scope ─────────────────────────────────────────

def test_a_container_sharing_the_mount_but_left_out_of_scope_is_refused(
    fake_docker, tmp_path, clock, database
):
    only_backend = (
        wal_fence.ScopedContainer(service=SERVICE, container_id=BACKEND_ID),
    )

    with pytest.raises(wal_fence.ScopeMismatch) as caught:
        _fence(fake_docker, tmp_path, clock, scope=only_backend)

    assert caught.value.code == "SCOPE_MISMATCH"
    # The agent also mounts this directory. Stopping the backend alone would
    # leave a writer running behind the fence.
    assert AGENT_ID in str(caught.value)
    assert not fake_docker.ran("update")


def test_a_scope_naming_a_container_that_does_not_share_the_mount_is_refused(
    fake_docker, tmp_path, clock, database
):
    stranger = FakeContainer(container_id="9" * 64, service="frontend")
    fake_docker.containers[stranger.container_id] = stranger
    scope = scope_of(fake_docker)

    with pytest.raises(wal_fence.ScopeMismatch):
        _fence(fake_docker, tmp_path, clock, scope=scope)


def test_the_primary_container_must_be_in_scope(
    fake_docker, tmp_path, clock, database
):
    without_primary = (
        wal_fence.ScopedContainer(service="agent", container_id=AGENT_ID),
    )

    with pytest.raises(wal_fence.ScopeMismatch) as caught:
        _fence(fake_docker, tmp_path, clock, scope=without_primary)

    assert "not in the declared scope" in str(caught.value)


# ── 5. restart policy ────────────────────────────────

def test_the_existing_restart_policies_are_recorded_before_anything_changes(
    fake_docker, tmp_path, clock, database
):
    manifest = _fence(fake_docker, tmp_path, clock)

    recorded = {
        entry["container_id"]: entry["restart_policy_before"]
        for entry in manifest.data["scope"]
    }
    assert recorded[BACKEND_ID] == {"name": "unless-stopped", "maximum_retry_count": 0}
    assert recorded[AGENT_ID] == {"name": "on-failure", "maximum_retry_count": 3}
    # Written down first: the first `docker update` may not run until every
    # policy that will need restoring has been captured.
    first_update = next(i for i, argv in enumerate(fake_docker.calls) if argv[1] == "update")
    inspects_before = [
        argv for argv in fake_docker.calls[:first_update] if argv[1] == "inspect"
    ]
    assert {argv[-1] for argv in inspects_before} >= {BACKEND_ID, AGENT_ID}


def test_every_scoped_container_is_set_to_no_and_read_back(
    fake_docker, tmp_path, clock, database
):
    manifest = _fence(fake_docker, tmp_path, clock)

    updated = {argv[-1] for argv in fake_docker.ran("update")}
    assert updated == {BACKEND_ID, AGENT_ID}
    assert all("--restart=no" in argv for argv in fake_docker.ran("update"))
    after = {
        entry["container_id"]: entry["restart_policy_after"]
        for entry in manifest.data["scope"]
    }
    assert after[BACKEND_ID]["name"] == "no"
    assert after[AGENT_ID]["name"] == "no"


def test_a_restart_policy_that_does_not_read_back_as_no_is_refused(
    fake_docker, tmp_path, clock, database
):
    fake_docker.container(AGENT_ID).ignore_restart_update = True

    with pytest.raises(wal_fence.RestartPolicyNotApplied) as caught:
        _fence(fake_docker, tmp_path, clock)

    assert caught.value.code == "RESTART_POLICY_NOT_APPLIED"
    # Asking is not the same as it having happened, and a container that can
    # still be restarted by the daemon is not fenced.
    assert not fake_docker.ran("stop")


def test_a_failed_restart_policy_update_is_refused(
    fake_docker, tmp_path, clock, database
):
    fake_docker.overrides["update"] = lambda argv: (1, "", "permission denied")

    with pytest.raises(wal_fence.DockerCommandFailed) as caught:
        _fence(fake_docker, tmp_path, clock)

    assert "permission denied" in str(caught.value)
    assert not fake_docker.ran("stop")


# ── 6. stopping, without killing ─────────────────────

def test_a_container_still_running_after_stop_is_refused(
    fake_docker, tmp_path, clock, database
):
    fake_docker.container(AGENT_ID).stop_grace_seconds = float("inf")

    with pytest.raises(wal_fence.StopIncomplete) as caught:
        _fence(fake_docker, tmp_path, clock, stop_deadline_seconds=5)

    assert caught.value.code == "STOP_INCOMPLETE"
    # OUR patience ran out, not the workload's. The answer is a human, not a
    # kill, and the container is still there to be looked at.
    assert fake_docker.sigkilled() == []
    assert fake_docker.container(AGENT_ID).running is True


def test_a_stop_that_reports_failure_is_refused_without_escalating(
    fake_docker, tmp_path, clock, database
):
    fake_docker.overrides["stop"] = lambda argv: (1, "", "container stop timed out")

    with pytest.raises(wal_fence.DockerCommandFailed):
        _fence(fake_docker, tmp_path, clock)

    assert not fake_docker.ran("kill")
    assert not fake_docker.ran("start")


def test_the_stop_deadline_is_ours_and_never_dockers(
    fake_docker, tmp_path, clock, database
):
    """Our patience is configurable. The workload's death sentence is not."""
    _fence(fake_docker, tmp_path, clock, stop_deadline_seconds=45)

    for argv in fake_docker.ran("stop"):
        assert "--timeout=-1" in argv
        assert not any(a.startswith("--timeout=4") for a in argv)


# ── 7. the descriptor probe ──────────────────────────

def test_an_open_descriptor_on_the_database_refuses_the_fence(
    data_dir, tmp_path, clock, database
):
    fake = FakeDocker(
        [
            FakeContainer(BACKEND_ID, SERVICE, mounts=[bind_mount(data_dir)]),
            FakeContainer(AGENT_ID, "agent", mounts=[bind_mount(data_dir)]),
        ],
        open_files=[OpenFile(pid=9001, fd=11, path=str(database))],
    )

    with pytest.raises(wal_fence.DescriptorsOpen) as caught:
        wal_fence.fence(**fence_kwargs(fake, tmp_path, clock=clock, scope=scope_of(fake)))

    assert caught.value.code == "DESCRIPTORS_OPEN"
    assert 9001 in caught.value.pids


def test_an_open_descriptor_on_the_wal_refuses_the_fence(
    data_dir, tmp_path, clock, database
):
    wal = str(data_dir / f"{DB_RELPATH}-wal")
    fake = FakeDocker(
        [
            FakeContainer(BACKEND_ID, SERVICE, mounts=[bind_mount(data_dir)]),
            FakeContainer(AGENT_ID, "agent", mounts=[bind_mount(data_dir)]),
        ],
        open_files=[OpenFile(pid=9002, fd=12, path=wal)],
    )

    with pytest.raises(wal_fence.DescriptorsOpen) as caught:
        wal_fence.fence(**fence_kwargs(fake, tmp_path, clock=clock, scope=scope_of(fake)))

    assert 9002 in caught.value.pids


def test_a_probe_that_cannot_answer_refuses_the_fence(
    data_dir, tmp_path, clock, database
):
    fake = FakeDocker(
        [
            FakeContainer(BACKEND_ID, SERVICE, mounts=[bind_mount(data_dir)]),
            FakeContainer(AGENT_ID, "agent", mounts=[bind_mount(data_dir)]),
        ],
        lsof_returncode=2,
    )

    with pytest.raises(wal_fence.ProbeFailed) as caught:
        wal_fence.fence(**fence_kwargs(fake, tmp_path, clock=clock, scope=scope_of(fake)))

    assert caught.value.code == "PROBE_FAILED"
    # "I could not tell" is not "nothing is open".


def test_the_probe_asks_the_host_not_docker(fake_docker, tmp_path, clock, database):
    _fence(fake_docker, tmp_path, clock)

    probes = [argv for argv in fake_docker.calls if argv[0] == "lsof"]
    # Two: one to prove the probe can see a descriptor we are holding, and one
    # to ask about everybody else.
    assert len(probes) == 2
    assert all(str(database) in argv for argv in probes)
    assert all(f"{database}-wal" in argv for argv in probes)
    # Independent of the daemon on purpose: asking Docker whether its own
    # container let go would be asking the suspect.
    assert not fake_docker.ran("top")


# ── 8. a failure never puts the host back by itself ───

def test_a_failure_after_the_stop_leaves_everything_stopped(
    data_dir, tmp_path, clock, database
):
    fake = FakeDocker(
        [
            FakeContainer(BACKEND_ID, SERVICE, mounts=[bind_mount(data_dir)]),
            FakeContainer(AGENT_ID, "agent", mounts=[bind_mount(data_dir)]),
        ],
        open_files=[OpenFile(pid=9003, fd=5, path=str(database))],
    )

    with pytest.raises(wal_fence.DescriptorsOpen):
        wal_fence.fence(**fence_kwargs(fake, tmp_path, clock=clock, scope=scope_of(fake)))

    # Uncertain state is left exactly as it is. Restarting the service here
    # would hand the database back to a writer while a human is still
    # deciding what happened.
    assert not fake.ran("start")
    for container in fake.containers.values():
        assert container.running is False
        assert container.restart_policy == "no"


# ── 9. the manifest ──────────────────────────────────

def test_a_successful_fence_writes_a_quiesced_manifest(
    fake_docker, tmp_path, clock, database, data_dir
):
    manifest = _fence(fake_docker, tmp_path, clock, lease_ttl_seconds=600)

    on_disk = json.loads(Path(manifest.path).read_text())
    assert on_disk["state"] == "QUIESCED"
    assert on_disk["target"] == {
        "project": PROJECT,
        "service": SERVICE,
        "container_id": BACKEND_ID,
        "image_id": IMAGE_ID,
        "image_ref": fake_docker.container(BACKEND_ID).image_ref,
    }
    assert on_disk["mount"] == {
        "destination": DATA_DESTINATION,
        "source": str(data_dir),
        "type": "bind",
        "rw": True,
    }
    assert on_disk["database"]["db_path"] == str(database)
    assert on_disk["database"]["db"]["size_bytes"] == len(DB_BYTES)
    assert on_disk["database"]["wal"]["size_bytes"] == len(WAL_BYTES)
    assert on_disk["database"]["db"]["inode"] == os.stat(database).st_ino
    assert on_disk["fd_probe"]["open_pids"] == []
    assert on_disk["lease"]["expires_at"] == clock.now + 600
    for entry in on_disk["scope"]:
        assert entry["state_after_stop"]["status"] == "exited"
        assert entry["restart_policy_after"]["name"] == "no"


def test_a_manifest_is_never_written_over(fake_docker, tmp_path, clock, database):
    manifest = _fence(fake_docker, tmp_path, clock)
    before = Path(manifest.path).read_bytes()
    mutations_so_far = len(fake_docker.ran("update")) + len(fake_docker.ran("stop"))

    with pytest.raises(wal_fence.ManifestRejected) as caught:
        _fence(fake_docker, tmp_path, clock)

    assert caught.value.code == "MANIFEST_REJECTED"
    assert Path(manifest.path).read_bytes() == before
    # Refused before it touched anything. Discovering the clash only at the
    # write would mean a service stopped for a run that was never going to
    # produce a manifest.
    assert len(fake_docker.ran("update")) + len(fake_docker.ran("stop")) == (
        mutations_so_far
    )


# ── 10. what the fence promises right before CP-5A acts ───

def test_a_fresh_fence_passes_the_check_before_the_source_is_opened(
    fake_docker, tmp_path, clock, database
):
    manifest = _fence(fake_docker, tmp_path, clock)
    guard = _guard(manifest, fake_docker, clock).claim()

    grant = guard.check_before_source_open(db_path=str(database))

    assert isinstance(grant, wal_fence.SourceApplyGrant)
    assert grant.ok is True
    assert grant.lease_id == manifest.data["lease"]["id"]
    assert grant.claim_id == guard.claim_id
    assert grant.db_path == str(database)


def test_an_expired_lease_is_refused(fake_docker, tmp_path, clock, database):
    manifest = _fence(fake_docker, tmp_path, clock, lease_ttl_seconds=300)
    guard = _guard(manifest, fake_docker, clock).claim()
    clock.advance(301)

    with pytest.raises(wal_fence.FenceStale) as caught:
        guard.check_before_source_open(db_path=str(database))

    assert caught.value.code == "FENCE_STALE"


def test_a_container_that_came_back_up_is_refused(
    fake_docker, tmp_path, clock, database
):
    manifest = _fence(fake_docker, tmp_path, clock)
    guard = _guard(manifest, fake_docker, clock).claim()
    # Somebody, or something, started it again.
    fake_docker.container(AGENT_ID).status = "running"
    fake_docker.container(AGENT_ID).running = True

    with pytest.raises(wal_fence.FenceBroken) as caught:
        guard.check_before_source_open(db_path=str(database))

    assert caught.value.code == "FENCE_BROKEN"
    assert AGENT_ID in str(caught.value)


def test_a_restart_policy_that_drifted_back_is_refused(
    fake_docker, tmp_path, clock, database
):
    manifest = _fence(fake_docker, tmp_path, clock)
    guard = _guard(manifest, fake_docker, clock).claim()
    fake_docker.container(BACKEND_ID).restart_policy = "unless-stopped"

    with pytest.raises(wal_fence.FenceBroken):
        guard.check_before_source_open(db_path=str(database))


def test_a_descriptor_opened_after_the_fence_is_refused(
    fake_docker, tmp_path, clock, database
):
    manifest = _fence(fake_docker, tmp_path, clock)
    guard = _guard(manifest, fake_docker, clock).claim()
    fake_docker.open_files.append(OpenFile(pid=7777, fd=3, path=str(database)))

    with pytest.raises(wal_fence.DescriptorsOpen) as caught:
        guard.check_before_source_open(db_path=str(database))

    assert 7777 in caught.value.pids


def test_a_database_that_changed_since_the_fence_is_refused(
    fake_docker, tmp_path, clock, database
):
    manifest = _fence(fake_docker, tmp_path, clock)
    guard = _guard(manifest, fake_docker, clock).claim()
    database.write_bytes(DB_BYTES[:-1] + b"\x99")

    with pytest.raises(wal_fence.FenceDrift) as caught:
        guard.check_before_source_open(db_path=str(database))

    assert caught.value.code == "FENCE_DRIFT"


def test_a_wal_that_changed_since_the_fence_is_refused(
    fake_docker, tmp_path, clock, database, data_dir
):
    manifest = _fence(fake_docker, tmp_path, clock)
    guard = _guard(manifest, fake_docker, clock).claim()
    (data_dir / f"{DB_RELPATH}-wal").write_bytes(WAL_BYTES + b"\x00" * 8)

    with pytest.raises(wal_fence.FenceDrift):
        guard.check_before_source_open(db_path=str(database))


def test_a_manifest_reused_after_the_wal_was_already_folded_in_is_refused(
    fake_docker, tmp_path, clock, database, data_dir
):
    """The same lease may not authorise a second pass over the same database.

    A checkpoint rewrites the database and empties the WAL, so the identity
    the manifest recorded can never match again — replaying a spent manifest
    is exactly what that binding is there to stop.
    """
    manifest = _fence(fake_docker, tmp_path, clock)
    guard = _guard(manifest, fake_docker, clock).claim()
    guard.check_before_source_open(db_path=str(database))

    # ... CP-5A runs and folds the WAL in ...
    database.write_bytes(DB_BYTES[:16] + b"\x55" * (len(DB_BYTES) - 16))
    (data_dir / f"{DB_RELPATH}-wal").write_bytes(b"")

    with pytest.raises(wal_fence.FenceDrift):
        guard.check_before_source_open(db_path=str(database))


def test_a_manifest_for_a_different_database_is_refused(
    fake_docker, tmp_path, clock, database
):
    manifest = _fence(fake_docker, tmp_path, clock)
    guard = _guard(manifest, fake_docker, clock).claim()
    elsewhere = tmp_path / "somewhere-else.db"
    elsewhere.write_bytes(DB_BYTES)

    with pytest.raises(wal_fence.FenceScopeMismatch) as caught:
        guard.check_before_source_open(db_path=str(elsewhere))

    assert caught.value.code == "FENCE_SCOPE_MISMATCH"


def test_a_manifest_is_reread_from_disk_with_its_lease_intact(
    fake_docker, tmp_path, clock, database
):
    written = _fence(fake_docker, tmp_path, clock)
    guard = wal_fence.ManifestFence.from_file(
        written.path,
        runner=fake_docker,
        clock=clock,
        authority_verifier=ACK_KEY,
        visibility_witness=ScriptedWitness(fake_docker),
    ).claim()

    check = guard.check_before_source_open(db_path=str(database))

    assert check.lease_id == written.data["lease"]["id"]


def test_a_manifest_that_is_not_quiesced_is_refused(
    fake_docker, tmp_path, clock, database
):
    written = _fence(fake_docker, tmp_path, clock)
    tampered = tmp_path / "tampered.json"
    data = json.loads(Path(written.path).read_text())
    data["state"] = "DRAFT"
    tampered.write_text(json.dumps(data))

    # Refused at read time now, which is earlier and leaves no half-trusted
    # object in anyone's hands.
    with pytest.raises(wal_fence.ManifestRejected):
        wal_fence.ManifestFence.from_file(tampered, runner=fake_docker, clock=clock)


# ── 11. release is a separate, explicit act ──────────

def test_release_restores_the_recorded_policies_and_leaves_containers_stopped(
    fake_docker, tmp_path, clock, database
):
    manifest = _fence(fake_docker, tmp_path, clock)

    record = wal_fence.release(
        manifest_path=manifest.path, runner=fake_docker, clock=clock
    )

    assert fake_docker.container(BACKEND_ID).restart_policy == "unless-stopped"
    assert fake_docker.container(AGENT_ID).restart_policy == "on-failure"
    assert fake_docker.container(AGENT_ID).restart_max_retries == 3
    # The default is deliberately NOT to bring the service back: whoever ran
    # the recovery decides when the database is fit to serve again.
    assert not fake_docker.ran("start")
    assert all(c.running is False for c in fake_docker.containers.values())
    assert record.started_containers == ()


def test_release_starts_containers_only_when_asked(
    fake_docker, tmp_path, clock, database
):
    manifest = _fence(fake_docker, tmp_path, clock)

    record = wal_fence.release(
        manifest_path=manifest.path,
        runner=fake_docker,
        clock=clock,
        start_containers=True,
    )

    assert {argv[-1] for argv in fake_docker.ran("start")} == {BACKEND_ID, AGENT_ID}
    assert set(record.started_containers) == {BACKEND_ID, AGENT_ID}
    assert all(c.running is True for c in fake_docker.containers.values())


def test_a_release_whose_readback_disagrees_is_refused(
    fake_docker, tmp_path, clock, database
):
    manifest = _fence(fake_docker, tmp_path, clock)
    fake_docker.container(AGENT_ID).ignore_restart_update = True

    with pytest.raises(wal_fence.ReleaseFailed) as caught:
        wal_fence.release(manifest_path=manifest.path, runner=fake_docker, clock=clock)

    assert caught.value.code == "RELEASE_FAILED"
    assert not fake_docker.ran("start")


def test_release_refuses_to_run_twice(fake_docker, tmp_path, clock, database):
    manifest = _fence(fake_docker, tmp_path, clock)
    wal_fence.release(manifest_path=manifest.path, runner=fake_docker, clock=clock)

    with pytest.raises(wal_fence.AlreadyReleased) as caught:
        wal_fence.release(manifest_path=manifest.path, runner=fake_docker, clock=clock)

    assert caught.value.code == "ALREADY_RELEASED"


def test_a_release_whose_container_is_gone_is_refused(
    fake_docker, tmp_path, clock, database
):
    manifest = _fence(fake_docker, tmp_path, clock)
    del fake_docker.containers[AGENT_ID]

    with pytest.raises(wal_fence.ReleaseFailed):
        wal_fence.release(manifest_path=manifest.path, runner=fake_docker, clock=clock)


def test_release_writes_a_record_beside_the_manifest_and_leaves_it_intact(
    fake_docker, tmp_path, clock, database
):
    manifest = _fence(fake_docker, tmp_path, clock)
    before = Path(manifest.path).read_bytes()

    record = wal_fence.release(
        manifest_path=manifest.path, runner=fake_docker, clock=clock
    )

    assert Path(manifest.path).read_bytes() == before
    written = json.loads(Path(record.path).read_text())
    assert written["state"] == "RELEASED"
    assert written["lease_id"] == manifest.data["lease"]["id"]


def test_every_entry_point_demands_its_runner(fake_docker, tmp_path, clock, database):
    """There is no default host. A caller has to say whose host this is.

    That is what makes the whole module testable, and it is also what stops a
    stray call from reaching a real daemon because someone forgot an argument.
    """
    import inspect

    for name in ("preflight", "fence", "release"):
        parameter = inspect.signature(getattr(wal_fence, name)).parameters["runner"]
        assert parameter.default is inspect.Parameter.empty, name
        assert parameter.kind is inspect.Parameter.KEYWORD_ONLY, name
    guard = inspect.signature(wal_fence.ManifestFence.__init__).parameters["runner"]
    assert guard.default is inspect.Parameter.empty


def test_a_full_fence_and_release_start_no_real_process(
    fake_docker, tmp_path, clock, database, monkeypatch
):
    def explode(*args, **kwargs):  # pragma: no cover - the point is it is unused
        raise AssertionError(
            "a real process was about to be started; this suite must never "
            "reach a Docker daemon or a host lsof"
        )

    monkeypatch.setattr(wal_fence.subprocess, "run", explode)

    manifest = _fence(fake_docker, tmp_path, clock)
    capability = _guard(manifest, fake_docker, clock).claim()
    capability.check_before_source_open(db_path=str(database))
    capability.begin_apply()
    capability.complete()
    wal_fence.release(manifest_path=manifest.path, runner=fake_docker, clock=clock)


# ── 13. the restart policy has to survive a crash mid-change ───

def test_a_prepared_record_lands_on_disk_before_the_first_update(
    fake_docker, tmp_path, clock, database
):
    """The record of what to put back is written before anything is changed.

    If the process dies between the first `docker update` and the second, the
    only thing that can tell anyone the agent used to be `on-failure:3` is a
    file that was already on disk when the change began.
    """
    seen_at_first_update = {}
    real_update = fake_docker._update

    def capture(argv):
        seen_at_first_update.setdefault(
            "prepared", Path(_prepared_path(tmp_path)).exists()
        )
        seen_at_first_update.setdefault(
            "body", Path(_prepared_path(tmp_path)).read_text()
            if Path(_prepared_path(tmp_path)).exists() else ""
        )
        return real_update(argv)

    fake_docker._update = capture
    wal_fence.fence(**fence_kwargs(fake_docker, tmp_path, clock=clock))

    assert seen_at_first_update["prepared"] is True
    recorded = json.loads(seen_at_first_update["body"])
    assert recorded["state"] == "PREPARED"
    policies = {
        e["container_id"]: e["restart_policy_before"] for e in recorded["scope"]
    }
    assert policies[AGENT_ID] == {"name": "on-failure", "maximum_retry_count": 3}
    assert policies[BACKEND_ID] == {"name": "unless-stopped", "maximum_retry_count": 0}
    assert recorded["target"]["container_id"] == BACKEND_ID
    assert recorded["target"]["image_id"] == IMAGE_ID
    assert recorded["target"]["project"] == PROJECT
    assert recorded["target"]["service"] == SERVICE
    assert recorded["mount"]["source"] == str(database.parent)


def _prepared_path(tmp_path, name="quiesce-1.json"):
    return str(tmp_path / "manifests" / f"{name}.prepared.json")


def test_a_second_update_failure_leaves_prepared_and_release_restores_exactly(
    fake_docker, tmp_path, clock, database
):
    """RED A. The first policy moved, the second did not, and the process died.

    A rerun must not read the already-changed `no` as the original, and an
    explicit release must put `on-failure:3` back exactly.
    """
    calls = {"n": 0}
    real_update = fake_docker._update

    def fail_the_second(argv):
        calls["n"] += 1
        if calls["n"] == 2:
            return 1, "", "daemon refused"
        return real_update(argv)

    fake_docker._update = fail_the_second

    with pytest.raises(wal_fence.DockerCommandFailed):
        wal_fence.fence(**fence_kwargs(fake_docker, tmp_path, clock=clock))

    # One policy is now `no`, the other is untouched, and the host is left that
    # way on purpose.
    changed = [c for c in fake_docker.containers.values() if c.restart_policy == "no"]
    assert len(changed) == 1
    prepared = json.loads(Path(_prepared_path(tmp_path)).read_text())
    assert prepared["state"] == "PREPARED"

    fake_docker._update = real_update
    record = wal_fence.release(
        prepared_path=_prepared_path(tmp_path), runner=fake_docker, clock=clock
    )

    assert fake_docker.container(AGENT_ID).restart_policy == "on-failure"
    assert fake_docker.container(AGENT_ID).restart_max_retries == 3
    assert fake_docker.container(BACKEND_ID).restart_policy == "unless-stopped"
    assert set(record.restored) == {AGENT_ID, BACKEND_ID}


def test_a_rerun_after_a_partial_change_never_rebaselines_from_no(
    fake_docker, tmp_path, clock, database
):
    """RED A, the dangerous half: a retry must not record `no` as the original."""
    calls = {"n": 0}
    real_update = fake_docker._update

    def fail_the_second(argv):
        calls["n"] += 1
        return (1, "", "daemon refused") if calls["n"] == 2 else real_update(argv)

    fake_docker._update = fail_the_second
    with pytest.raises(wal_fence.DockerCommandFailed):
        wal_fence.fence(**fence_kwargs(fake_docker, tmp_path, clock=clock))

    fake_docker._update = real_update
    manifest = wal_fence.fence(**fence_kwargs(fake_docker, tmp_path, clock=clock))

    before = {e["container_id"]: e["restart_policy_before"] for e in manifest.data["scope"]}
    # Not `no`: the retry reused the record written before the first change.
    assert before[AGENT_ID] == {"name": "on-failure", "maximum_retry_count": 3}
    assert before[BACKEND_ID] == {"name": "unless-stopped", "maximum_retry_count": 0}


def test_a_torn_prepared_record_is_not_read_as_a_complete_one(
    fake_docker, tmp_path, clock, database
):
    wal_fence.fence(**fence_kwargs(fake_docker, tmp_path, clock=clock))
    prepared = Path(_prepared_path(tmp_path))
    body = prepared.read_text()
    prepared.write_text(body[: len(body) // 2])

    with pytest.raises(wal_fence.PreparedRecordRejected) as caught:
        wal_fence.read_prepared(prepared)

    assert caught.value.code == "PREPARED_RECORD_REJECTED"


def test_a_prepared_record_with_a_broken_digest_is_refused(
    fake_docker, tmp_path, clock, database
):
    wal_fence.fence(**fence_kwargs(fake_docker, tmp_path, clock=clock))
    prepared = Path(_prepared_path(tmp_path))
    data = json.loads(prepared.read_text())
    data["scope"][0]["restart_policy_before"]["name"] = "always"
    prepared.write_text(json.dumps(data))

    with pytest.raises(wal_fence.PreparedRecordRejected):
        wal_fence.read_prepared(prepared)


def test_a_write_that_dies_halfway_leaves_nothing_at_the_final_name(
    fake_docker, tmp_path, clock, database, monkeypatch
):
    """A record only ever appears complete.

    The bytes go to a scratch file and are renamed into place, so a process
    that dies mid-write leaves the final name absent rather than present and
    half-true. A reader has no way to tell a truncated record from a short one
    except by it not being there.
    """
    real_dumps = wal_fence.json.dumps

    def die_on_the_manifest(obj, **kwargs):
        # `indent=2` is the call that serialises FOR THE FILE; the digest uses
        # compact separators. Failing here means the file has already been
        # opened, which is the only version of this that tests anything.
        if kwargs.get("indent") == 2 and obj.get("state") == "QUIESCED":
            raise OSError(28, "No space left on device")
        return real_dumps(obj, **kwargs)

    monkeypatch.setattr(wal_fence.json, "dumps", die_on_the_manifest)

    with pytest.raises(OSError):
        _fence(fake_docker, tmp_path, clock)

    manifest = tmp_path / "manifests" / "quiesce-1.json"
    assert not manifest.exists()
    # The prepared record, written before the failure, is complete and readable.
    assert wal_fence.read_prepared(_prepared_path(tmp_path)).scope


# ── 14. every writer on the host, not just this project ───

def _host_with(extra, data_dir):
    return FakeDocker(
        [
            FakeContainer(BACKEND_ID, SERVICE, mounts=[bind_mount(data_dir)]),
            FakeContainer(AGENT_ID, AGENT_SERVICE, mounts=[bind_mount(data_dir)]),
            *extra,
        ]
    )


def test_a_container_from_another_project_sharing_the_mount_is_refused(
    data_dir, tmp_path, clock, database
):
    """RED B. A different Compose project writes to the same directory."""
    intruder = FakeContainer(
        OTHER_PROJECT_ID, "sidecar", project="something-else",
        mounts=[bind_mount(data_dir)],
    )
    fake = _host_with([intruder], data_dir)

    with pytest.raises(wal_fence.ScopeMismatch) as caught:
        wal_fence.fence(
            **fence_kwargs(
                fake, tmp_path, clock=clock,
                scope=(
                    wal_fence.ScopedContainer(AGENT_SERVICE, AGENT_ID),
                    wal_fence.ScopedContainer(SERVICE, BACKEND_ID),
                ),
            )
        )

    assert OTHER_PROJECT_ID in str(caught.value)
    assert not fake.ran("update")
    assert not Path(_prepared_path(tmp_path)).exists()


def test_an_unlabeled_container_sharing_the_mount_is_refused(
    data_dir, tmp_path, clock, database
):
    stray = FakeContainer(
        UNLABELED_ID, "", labelled=False, mounts=[bind_mount(data_dir)]
    )
    fake = _host_with([stray], data_dir)

    with pytest.raises(wal_fence.ScopeMismatch) as caught:
        wal_fence.fence(
            **fence_kwargs(
                fake, tmp_path, clock=clock,
                scope=(
                    wal_fence.ScopedContainer(AGENT_SERVICE, AGENT_ID),
                    wal_fence.ScopedContainer(SERVICE, BACKEND_ID),
                ),
            )
        )

    assert UNLABELED_ID in str(caught.value)


def test_the_sharer_inventory_asks_the_host_for_every_container(
    fake_docker, tmp_path, clock, database
):
    wal_fence.fence(**fence_kwargs(fake_docker, tmp_path, clock=clock))

    unfiltered = [
        argv for argv in fake_docker.ran("ps") if "--filter" not in argv
    ]
    assert unfiltered, (
        "the inventory must list every container on the host; a project-scoped "
        "`docker ps` cannot see a writer that belongs to someone else"
    )


# ── 15. who can start these containers again ───

def test_a_docker_socket_holder_outside_the_scope_is_refused(
    data_dir, tmp_path, clock, database
):
    """RED C. Anything holding the Docker socket can undo the whole fence."""
    proxy = FakeContainer(
        SOCKET_PROXY_ID, "socket-proxy", mounts=[socket_mount()]
    )
    fake = _host_with([proxy], data_dir)

    with pytest.raises(wal_fence.StartAuthorityUnbound) as caught:
        wal_fence.fence(
            **fence_kwargs(
                fake, tmp_path, clock=clock,
                scope=(
                    wal_fence.ScopedContainer(AGENT_SERVICE, AGENT_ID),
                    wal_fence.ScopedContainer(SERVICE, BACKEND_ID),
                ),
            )
        )

    assert caught.value.code == "START_AUTHORITY_UNBOUND"
    assert SOCKET_PROXY_ID in str(caught.value)
    assert not fake.ran("update")


def test_a_declared_socket_holder_is_stopped_with_everything_else(
    data_dir, tmp_path, clock, database
):
    proxy = FakeContainer(SOCKET_PROXY_ID, "socket-proxy", mounts=[socket_mount()])
    fake = _host_with([proxy], data_dir)

    manifest = wal_fence.fence(
        **fence_kwargs(
            fake, tmp_path, clock=clock,
            scope=(
                wal_fence.ScopedContainer(AGENT_SERVICE, AGENT_ID),
                wal_fence.ScopedContainer("socket-proxy", SOCKET_PROXY_ID),
                wal_fence.ScopedContainer(SERVICE, BACKEND_ID),
            ),
        )
    )

    assert fake.container(SOCKET_PROXY_ID).running is False
    authorities = {a["container_id"]: a for a in manifest.data["start_authorities"]}
    assert authorities[SOCKET_PROXY_ID]["reason"] == "docker-socket"


def test_the_undetectable_authorities_are_written_down_not_assumed_away(
    fake_docker, tmp_path, clock, database
):
    manifest = wal_fence.fence(**fence_kwargs(fake_docker, tmp_path, clock=clock))

    ack = manifest.data["external_authority_ack"]
    # A host administrator, a systemd unit or a cron job can start any of this
    # and no probe can see them. The fence records who accepted that, when it
    # stops being true, and a signature that ties it to this exact scope.
    assert ack["issuer"] == "ops-oncall"
    assert ack["host_id"] == HOST_ID
    assert ack["expires_at"] > ack["issued_at"]
    assert ack["signature"]
    assert ack["scope_digest"]


def test_an_unacknowledged_external_authority_is_refused(
    fake_docker, tmp_path, clock, database
):
    with pytest.raises(wal_fence.ExternalAuthorityUnacknowledged) as caught:
        wal_fence.fence(
            **fence_kwargs(fake_docker, tmp_path, clock=clock, external_authority_ack=None)
        )

    assert caught.value.code == "EXTERNAL_AUTHORITY_UNACKNOWLEDGED"


def test_the_inventory_is_measured_again_when_the_lease_is_claimed(
    data_dir, tmp_path, clock, database
):
    fake = _host_with([], data_dir)
    manifest = wal_fence.fence(
        **fence_kwargs(fake, tmp_path, clock=clock, scope=scope_of(fake))
    )
    guard = _guard(manifest, fake, clock)
    # A socket holder appears between the quiesce and the claim.
    proxy = FakeContainer(SOCKET_PROXY_ID, "socket-proxy", mounts=[socket_mount()])
    fake.containers[proxy.container_id] = proxy

    with pytest.raises(wal_fence.StartAuthorityUnbound):
        guard.claim()


def test_a_writer_that_appears_between_quiesce_and_claim_is_refused(
    data_dir, tmp_path, clock, database
):
    fake = _host_with([], data_dir)
    manifest = wal_fence.fence(
        **fence_kwargs(fake, tmp_path, clock=clock, scope=scope_of(fake))
    )
    guard = _guard(manifest, fake, clock)
    fake.containers[OTHER_PROJECT_ID] = FakeContainer(
        OTHER_PROJECT_ID, "sidecar", project="elsewhere", mounts=[bind_mount(data_dir)]
    )

    with pytest.raises(wal_fence.ScopeMismatch):
        guard.claim()


# ── 16. a silence has to be proved, not assumed ───

def test_a_probe_that_exits_one_with_a_diagnostic_is_refused(
    data_dir, tmp_path, clock, database
):
    """RED D. `lsof` looked, could not tell, and said so on stderr."""
    fake = FakeDocker(
        [
            FakeContainer(BACKEND_ID, SERVICE, mounts=[bind_mount(data_dir)]),
            FakeContainer(AGENT_ID, AGENT_SERVICE, mounts=[bind_mount(data_dir)]),
        ],
        lsof_returncode=1,
        lsof_stderr="lsof: WARNING: can't stat() nfs file system /app/data\n",
    )

    with pytest.raises(wal_fence.ProbeFailed) as caught:
        wal_fence.fence(
            **fence_kwargs(fake, tmp_path, clock=clock, scope=scope_of(fake))
        )

    assert caught.value.code == "PROBE_FAILED"
    assert "stderr" in str(caught.value).lower()


def test_a_probe_that_exits_zero_saying_nothing_is_refused(
    data_dir, tmp_path, clock, database
):
    """RED D. Exit 0 means "I found something"; silence with it is nonsense."""
    fake = FakeDocker(
        [
            FakeContainer(BACKEND_ID, SERVICE, mounts=[bind_mount(data_dir)]),
            FakeContainer(AGENT_ID, AGENT_SERVICE, mounts=[bind_mount(data_dir)]),
        ],
        lsof_returncode=0,
        lsof_stdout="",
    )

    with pytest.raises(wal_fence.ProbeFailed) as caught:
        wal_fence.fence(
            **fence_kwargs(fake, tmp_path, clock=clock, scope=scope_of(fake))
        )

    assert "exited 0" in str(caught.value)
    assert "printed nothing" in str(caught.value)


def test_a_probe_whose_output_cannot_be_parsed_is_refused(
    data_dir, tmp_path, clock, database
):
    fake = FakeDocker(
        [
            FakeContainer(BACKEND_ID, SERVICE, mounts=[bind_mount(data_dir)]),
            FakeContainer(AGENT_ID, AGENT_SERVICE, mounts=[bind_mount(data_dir)]),
        ],
        lsof_returncode=0,
        lsof_stdout="COMMAND  PID USER  FD  TYPE\npython 991 x 11u REG\n",
    )

    with pytest.raises(wal_fence.ProbeFailed) as caught:
        wal_fence.fence(
            **fence_kwargs(fake, tmp_path, clock=clock, scope=scope_of(fake))
        )

    # Reading a format we do not understand is not the same as reading an
    # empty one, and only one of those is a silence worth trusting.
    assert "cannot" in str(caught.value) and "read" in str(caught.value)


def test_a_probe_that_cannot_see_our_own_descriptor_is_refused(
    data_dir, tmp_path, clock, database
):
    """A probe that cannot see a descriptor we are holding proves nothing.

    An unprivileged `lsof`, or one in the wrong namespace, returns the same
    silence as a genuinely idle file. The fence opens the database itself and
    demands the probe report it back before it will believe any silence.
    """
    fake = FakeDocker(
        [
            FakeContainer(BACKEND_ID, SERVICE, mounts=[bind_mount(data_dir)]),
            FakeContainer(AGENT_ID, AGENT_SERVICE, mounts=[bind_mount(data_dir)]),
        ],
        blind_to_own_fds=True,
    )

    with pytest.raises(wal_fence.ProbeFailed) as caught:
        wal_fence.fence(
            **fence_kwargs(fake, tmp_path, clock=clock, scope=scope_of(fake))
        )

    assert "visib" in str(caught.value).lower() or "see" in str(caught.value).lower()


def test_the_visibility_check_and_the_real_probe_are_both_recorded(
    fake_docker, tmp_path, clock, database
):
    manifest = wal_fence.fence(**fence_kwargs(fake_docker, tmp_path, clock=clock))

    probe = manifest.data["fd_probe"]
    assert probe["open_pids"] == []
    assert probe["returncode"] == 1
    assert probe["visibility_verified"] is True
    assert probe["visibility"]["observed_own_fd"] is True


# ── 17. stopping without killing ───

def test_the_stop_asks_docker_to_wait_indefinitely(
    fake_docker, tmp_path, clock, database
):
    wal_fence.fence(**fence_kwargs(fake_docker, tmp_path, clock=clock))

    stops = fake_docker.ran("stop")
    assert stops
    for argv in stops:
        assert "--timeout=-1" in argv
        assert not any(a.startswith("--signal") for a in argv)
        assert not any(a.startswith("--time=") for a in argv)


def test_no_workload_is_ever_sigkilled(fake_docker, tmp_path, clock, database):
    wal_fence.fence(**fence_kwargs(fake_docker, tmp_path, clock=clock))

    assert fake_docker.sigkilled() == []


def test_a_workload_that_will_not_exit_is_left_running_and_reported(
    data_dir, tmp_path, clock, database
):
    """The host-side deadline ends OUR wait, not the workload."""
    stubborn = FakeContainer(
        AGENT_ID, AGENT_SERVICE, mounts=[bind_mount(data_dir)],
        stop_grace_seconds=float("inf"),
    )
    fake = _host_with([], data_dir)
    fake.containers[AGENT_ID] = stubborn

    with pytest.raises(wal_fence.StopIncomplete) as caught:
        wal_fence.fence(
            **fence_kwargs(
                fake, tmp_path, clock=clock, scope=scope_of(fake),
                stop_deadline_seconds=5,
            )
        )

    assert caught.value.code == "STOP_INCOMPLETE"
    assert fake.sigkilled() == []
    assert fake.container(AGENT_ID).running is True
    assert not fake.ran("kill")
    # The message has to say which clock ran out. "It did not stop" and "we
    # stopped waiting" are different situations for whoever reads this.
    assert "abandoned" in str(caught.value)
    assert "NOT been killed" in str(caught.value)


@pytest.mark.parametrize(
    "argv",
    [
        ["docker", "stop", "--timeout=30", BACKEND_ID],
        ["docker", "stop", "--time=30", BACKEND_ID],
        ["docker", "stop", "--timeout=-1", "--signal=SIGKILL", BACKEND_ID],
        ["docker", "stop", "--signal=SIGKILL", "--timeout=-1", BACKEND_ID],
        # Not a kill, and still refused: choosing the signal is not this
        # wrapper's decision to make, and a container's own STOPSIGNAL is.
        ["docker", "stop", "--timeout=-1", "--signal=SIGTERM", BACKEND_ID],
        ["docker", "stop", "-s", "SIGTERM", "--timeout=-1", BACKEND_ID],
        ["docker", "stop", BACKEND_ID],
    ],
)
def test_a_stop_that_could_kill_the_workload_is_refused(argv):
    """RED I. A finite timeout is a promise to SIGKILL; so is the signal flag."""
    calls = []

    with pytest.raises(wal_fence.ForbiddenCommand) as caught:
        wal_fence.run_host_command(_refusing_runner(calls), argv)

    assert caught.value.code == "FORBIDDEN_COMMAND"
    assert calls == []


# ── 18. the claim: atomic, one-shot, and held ───

def test_a_claim_is_one_shot(fake_docker, tmp_path, clock, database):
    manifest = wal_fence.fence(**fence_kwargs(fake_docker, tmp_path, clock=clock))
    guard = _guard(manifest, fake_docker, clock)

    first = guard.claim()

    with pytest.raises(wal_fence.ClaimUnavailable) as caught:
        guard.claim()

    assert caught.value.code == "CLAIM_UNAVAILABLE"
    assert first.is_active is True


def test_two_concurrent_claims_leave_exactly_one_winner(
    fake_docker, tmp_path, clock, database
):
    """RED E. Two processes, one lease."""
    import threading

    manifest = wal_fence.fence(**fence_kwargs(fake_docker, tmp_path, clock=clock))
    results, errors = [], []
    barrier = threading.Barrier(2)

    def attempt():
        guard = _guard(manifest, fake_docker, clock)
        barrier.wait()
        try:
            results.append(guard.claim())
        except wal_fence.WalFenceError as exc:
            errors.append(exc)

    threads = [threading.Thread(target=attempt) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert len(results) == 1
    assert len(errors) == 1
    assert isinstance(errors[0], wal_fence.ClaimUnavailable)


def test_completing_a_claim_invalidates_it(fake_docker, tmp_path, clock, database):
    manifest = wal_fence.fence(**fence_kwargs(fake_docker, tmp_path, clock=clock))
    capability = _guard(manifest, fake_docker, clock).claim()
    capability.check_before_source_open(db_path=str(database))
    # A completion is about a pass over the source, so there has to be one.
    capability.begin_apply()

    capability.complete()

    assert capability.is_active is False
    with pytest.raises(wal_fence.ClaimSpent) as caught:
        capability.check_before_source_open(db_path=str(database))
    assert caught.value.code == "CLAIM_SPENT"


def test_failing_a_claim_invalidates_it(fake_docker, tmp_path, clock, database):
    manifest = wal_fence.fence(**fence_kwargs(fake_docker, tmp_path, clock=clock))
    capability = _guard(manifest, fake_docker, clock).claim()

    capability.fail("checkpoint refused")

    assert capability.is_active is False
    with pytest.raises(wal_fence.ClaimSpent):
        capability.check_before_source_open(db_path=str(database))


def test_release_cannot_invalidate_a_claim_that_is_still_open(
    fake_docker, tmp_path, clock, database
):
    """The previous round let release cut in here. It no longer can.

    A release landing between the grant and the checkpoint would restore the
    restart policies and, if asked, start the containers — handing the database
    back to a writer while it is being rewritten.
    """
    manifest = wal_fence.fence(**fence_kwargs(fake_docker, tmp_path, clock=clock))
    capability = _guard(manifest, fake_docker, clock).claim()

    with pytest.raises(wal_fence.ClaimInFlight):
        wal_fence.release(manifest_path=manifest.path, runner=fake_docker, clock=clock)

    # The claim is untouched and still good, which is the point.
    grant = capability.check_before_source_open(db_path=str(database))
    assert grant.ok is True


def test_a_lease_reused_after_release_is_refused(
    fake_docker, tmp_path, clock, database
):
    """RED F."""
    manifest = wal_fence.fence(**fence_kwargs(fake_docker, tmp_path, clock=clock))
    wal_fence.release(manifest_path=manifest.path, runner=fake_docker, clock=clock)
    guard = _guard(manifest, fake_docker, clock)

    with pytest.raises(wal_fence.ClaimUnavailable) as caught:
        guard.claim()

    assert "release" in str(caught.value).lower()


def test_a_grant_carries_the_identity_the_recovery_must_match(
    fake_docker, tmp_path, clock, database, data_dir
):
    manifest = wal_fence.fence(**fence_kwargs(fake_docker, tmp_path, clock=clock))
    capability = _guard(manifest, fake_docker, clock).claim()

    grant = capability.check_before_source_open(db_path=str(database))

    assert grant.ok is True
    assert grant.claim_id == capability.claim_id
    assert grant.lease_id == manifest.lease_id
    assert grant.db_path == str(database)
    assert grant.wal_path == str(data_dir / f"{DB_RELPATH}-wal")
    assert grant.db_inode == os.stat(database).st_ino
    assert grant.db_device == os.stat(database).st_dev


def test_a_lease_that_expires_during_the_checks_is_not_approved(
    fake_docker, tmp_path, clock, database
):
    manifest = wal_fence.fence(
        **fence_kwargs(fake_docker, tmp_path, clock=clock, lease_ttl_seconds=300)
    )
    capability = _guard(manifest, fake_docker, clock).claim()
    real_inspect = fake_docker._inspect

    def slow_inspect(argv):
        clock.advance(200)          # the checks themselves take time
        return real_inspect(argv)

    fake_docker._inspect = slow_inspect

    with pytest.raises(wal_fence.FenceStale):
        capability.check_before_source_open(db_path=str(database))


# ── 19. release starts nothing it was not told to ───

def test_a_container_that_was_already_stopped_is_not_started_by_release(
    data_dir, tmp_path, clock, database
):
    """RED J. It was down before we arrived; putting it up is not "restoring"."""
    dormant = FakeContainer(
        AGENT_ID, AGENT_SERVICE, mounts=[bind_mount(data_dir)],
        status="exited", running=False, pid=0, restart_policy="on-failure",
        restart_max_retries=3,
    )
    fake = _host_with([], data_dir)
    fake.containers[AGENT_ID] = dormant
    manifest = wal_fence.fence(
        **fence_kwargs(fake, tmp_path, clock=clock, scope=scope_of(fake))
    )

    record = wal_fence.release(
        manifest_path=manifest.path, runner=fake, clock=clock, start_containers=True
    )

    assert set(record.started_containers) == {BACKEND_ID}
    assert AGENT_ID in record.not_started
    assert fake.container(AGENT_ID).running is False


def test_a_dormant_container_can_be_started_only_by_naming_it(
    data_dir, tmp_path, clock, database
):
    dormant = FakeContainer(
        AGENT_ID, AGENT_SERVICE, mounts=[bind_mount(data_dir)],
        status="exited", running=False, pid=0,
    )
    fake = _host_with([], data_dir)
    fake.containers[AGENT_ID] = dormant
    manifest = wal_fence.fence(
        **fence_kwargs(fake, tmp_path, clock=clock, scope=scope_of(fake))
    )

    record = wal_fence.release(
        manifest_path=manifest.path, runner=fake, clock=clock,
        start_containers=True, start_allowlist=(AGENT_ID,),
    )

    assert set(record.started_containers) == {BACKEND_ID, AGENT_ID}


# ── 20. quiescence that is actually proved ───────────

def test_a_probe_that_hides_other_processes_fails_closed(
    data_dir, tmp_path, clock, database
):
    """The dangerous `lsof`: chatty about us, silent about everyone else.

    It answers our visibility self-test perfectly — it can see the descriptor
    we are holding — and then omits every other process on the host. Its
    silence about a running backend looks exactly like a quiet file. Nothing
    but a witness in another process can tell those apart.
    """
    fake = FakeDocker(
        [
            FakeContainer(BACKEND_ID, SERVICE, mounts=[bind_mount(data_dir)]),
            FakeContainer(AGENT_ID, AGENT_SERVICE, mounts=[bind_mount(data_dir)]),
        ],
        hide_foreign_pids=True,
    )

    with pytest.raises(wal_fence.ProbeFailed) as caught:
        wal_fence.fence(**fence_kwargs(fake, tmp_path, clock=clock, scope=scope_of(fake)))

    assert caught.value.code == "PROBE_FAILED"
    assert "other process" in str(caught.value) or "another process" in str(caught.value)


def test_the_cross_process_witness_is_recorded_when_it_is_seen(
    fake_docker, tmp_path, clock, database
):
    witness = ScriptedWitness(fake_docker)
    manifest = _fence(fake_docker, tmp_path, clock, visibility_witness=witness)

    visibility = manifest.data["fd_probe"]["visibility"]
    assert visibility["observed_own_fd"] is True
    assert visibility["observed_foreign_fd"] is True
    assert visibility["witness_pid"] == witness.pid
    assert witness.opened, "the witness was never asked to hold anything open"


def test_a_run_with_no_witness_available_is_refused(
    fake_docker, tmp_path, clock, database
):
    """If cross-process visibility cannot be demonstrated, it is not assumed."""

    @contextlib.contextmanager
    def no_witness(path):
        raise OSError(1, "Operation not permitted")
        yield  # pragma: no cover

    with pytest.raises(wal_fence.ProbeFailed):
        _fence(fake_docker, tmp_path, clock, visibility_witness=no_witness)


# ── 21. mount sharers by storage identity, not by string ───

def test_a_writer_reaching_the_data_by_another_path_is_refused(
    data_dir, tmp_path, clock, database
):
    """The same directory, spelled differently, is the same directory.

    A second container can bind the data by a symlinked or otherwise aliased
    path. Comparing the `Source` strings says they are unrelated; comparing
    (device, inode) says they are the same bytes, and the second container can
    write to them.
    """
    alias = tmp_path / "alias-to-data"
    alias.symlink_to(data_dir)
    intruder = FakeContainer(
        OTHER_PROJECT_ID, "sidecar", project="elsewhere",
        mounts=[bind_mount(alias)],
    )
    fake = FakeDocker(
        [
            FakeContainer(BACKEND_ID, SERVICE, mounts=[bind_mount(data_dir)]),
            FakeContainer(AGENT_ID, AGENT_SERVICE, mounts=[bind_mount(data_dir)]),
            intruder,
        ]
    )

    with pytest.raises(wal_fence.ScopeMismatch) as caught:
        wal_fence.fence(
            **fence_kwargs(
                fake, tmp_path, clock=clock, data_dir=data_dir,
                scope=(
                    wal_fence.ScopedContainer(AGENT_SERVICE, AGENT_ID),
                    wal_fence.ScopedContainer(SERVICE, BACKEND_ID),
                ),
            )
        )

    assert OTHER_PROJECT_ID in str(caught.value)


def test_a_mount_whose_identity_cannot_be_established_is_refused(
    data_dir, tmp_path, clock, database
):
    """A source we cannot stat might be the same storage. Fail closed."""
    ghost = FakeContainer(
        OTHER_PROJECT_ID, "sidecar", project="elsewhere",
        mounts=[bind_mount(tmp_path / "not-on-this-host")],
    )
    fake = FakeDocker(
        [
            FakeContainer(BACKEND_ID, SERVICE, mounts=[bind_mount(data_dir)]),
            FakeContainer(AGENT_ID, AGENT_SERVICE, mounts=[bind_mount(data_dir)]),
            ghost,
        ]
    )

    with pytest.raises(wal_fence.ScopeMismatch) as caught:
        wal_fence.fence(
            **fence_kwargs(
                fake, tmp_path, clock=clock, data_dir=data_dir,
                scope=(
                    wal_fence.ScopedContainer(AGENT_SERVICE, AGENT_ID),
                    wal_fence.ScopedContainer(SERVICE, BACKEND_ID),
                ),
            )
        )

    assert "identity" in str(caught.value).lower()


# ── 22. the acknowledgement has to be checkable ───

def test_an_acknowledgement_with_empty_fields_is_refused(
    fake_docker, tmp_path, clock, database, data_dir
):
    blank = signed_ack(
        container_ids=[BACKEND_ID, AGENT_ID],
        mount_identity=mount_identity_of(data_dir),
        issuer="   ",
    )

    with pytest.raises(wal_fence.ExternalAuthorityUnacknowledged) as caught:
        _fence(fake_docker, tmp_path, clock, external_authority_ack=blank)

    assert "issuer" in str(caught.value)


def test_an_acknowledgement_that_does_not_verify_is_refused(
    fake_docker, tmp_path, clock, database, data_dir
):
    forged = signed_ack(
        container_ids=[BACKEND_ID, AGENT_ID],
        mount_identity=mount_identity_of(data_dir),
        key=b"not-the-real-key",
    )

    with pytest.raises(wal_fence.ExternalAuthorityUnverified) as caught:
        _fence(fake_docker, tmp_path, clock, external_authority_ack=forged)

    assert caught.value.code == "EXTERNAL_AUTHORITY_UNVERIFIED"


def test_an_acknowledgement_for_a_different_scope_is_refused(
    fake_docker, tmp_path, clock, database, data_dir
):
    elsewhere = signed_ack(
        container_ids=[BACKEND_ID],  # the agent is missing
        mount_identity=mount_identity_of(data_dir),
    )

    with pytest.raises(wal_fence.ExternalAuthorityUnverified):
        _fence(fake_docker, tmp_path, clock, external_authority_ack=elsewhere)


def test_an_expired_acknowledgement_is_refused(
    fake_docker, tmp_path, clock, database, data_dir
):
    stale = signed_ack(
        container_ids=[BACKEND_ID, AGENT_ID],
        mount_identity=mount_identity_of(data_dir),
        expires_at=clock.now - 1,
    )

    with pytest.raises(wal_fence.ExternalAuthorityUnverified) as caught:
        _fence(fake_docker, tmp_path, clock, external_authority_ack=stale)

    assert "expire" in str(caught.value).lower()


def test_an_acknowledgement_with_no_way_to_verify_it_is_refused(
    fake_docker, tmp_path, clock, database
):
    """Without a key it is a claim, not a signature, and it is not evidence."""
    with pytest.raises(wal_fence.ExternalAuthorityUnverified) as caught:
        _fence(fake_docker, tmp_path, clock, authority_verifier=None)

    assert "verif" in str(caught.value).lower()


def test_the_manifest_binds_the_acknowledgement_to_this_lease(
    fake_docker, tmp_path, clock, database
):
    manifest = _fence(fake_docker, tmp_path, clock)

    ack = manifest.data["external_authority_ack"]
    assert ack["issuer"] == "ops-oncall"
    assert ack["signature"]
    assert manifest.data["ack_lease_binding"]
    # Re-pairing a real acknowledgement with a different lease has to be
    # detectable after the fact, not just at the moment it was checked.
    assert manifest.data["ack_lease_binding"] != ack["signature"]


# ── 23. one apply, and release cannot cut in ───

def test_beginning_an_apply_twice_is_refused(fake_docker, tmp_path, clock, database):
    manifest = _fence(fake_docker, tmp_path, clock)
    capability = _guard(manifest, fake_docker, clock).claim()

    first = capability.begin_apply()

    assert first
    with pytest.raises(wal_fence.ApplyAlreadyStarted) as caught:
        capability.begin_apply()
    assert caught.value.code == "APPLY_ALREADY_STARTED"


def test_two_concurrent_applies_leave_exactly_one_winner(
    fake_docker, tmp_path, clock, database
):
    import threading

    manifest = _fence(fake_docker, tmp_path, clock)
    capability = _guard(manifest, fake_docker, clock).claim()
    started, refused = [], []
    barrier = threading.Barrier(2)

    def attempt():
        barrier.wait()
        try:
            started.append(capability.begin_apply())
        except wal_fence.WalFenceError as exc:
            refused.append(exc)

    threads = [threading.Thread(target=attempt) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert len(started) == 1
    assert len(refused) == 1


def test_release_is_refused_while_a_claim_is_live(
    fake_docker, tmp_path, clock, database
):
    manifest = _fence(fake_docker, tmp_path, clock)
    _guard(manifest, fake_docker, clock).claim()
    policies_before = {
        c.container_id: c.restart_policy for c in fake_docker.containers.values()
    }

    with pytest.raises(wal_fence.ClaimInFlight) as caught:
        wal_fence.release(
            manifest_path=manifest.path, runner=fake_docker, clock=clock,
            start_containers=True,
        )

    assert caught.value.code == "CLAIM_IN_FLIGHT"
    # Nothing moved: not a policy, not a container. A release that lands in the
    # middle of an apply is the fence undoing itself.
    assert {
        c.container_id: c.restart_policy for c in fake_docker.containers.values()
    } == policies_before
    assert not fake_docker.ran("start")


def test_release_is_refused_while_an_apply_is_in_flight(
    fake_docker, tmp_path, clock, database
):
    manifest = _fence(fake_docker, tmp_path, clock)
    capability = _guard(manifest, fake_docker, clock).claim()
    capability.begin_apply()

    with pytest.raises(wal_fence.ClaimInFlight):
        wal_fence.release(manifest_path=manifest.path, runner=fake_docker, clock=clock)

    assert not fake_docker.ran("start")


def test_release_proceeds_once_the_apply_has_finished(
    fake_docker, tmp_path, clock, database
):
    manifest = _fence(fake_docker, tmp_path, clock)
    capability = _guard(manifest, fake_docker, clock).claim()
    capability.begin_apply()
    capability.complete()

    record = wal_fence.release(
        manifest_path=manifest.path, runner=fake_docker, clock=clock
    )

    assert set(record.restored) == {BACKEND_ID, AGENT_ID}


# ── 24. durability of the claim itself ───

def test_the_claim_record_is_fsynced_with_its_directory_before_it_is_handed_out(
    fake_docker, tmp_path, clock, database, monkeypatch
):
    manifest = _fence(fake_docker, tmp_path, clock)
    synced = []
    real_fsync = wal_fence.os.fsync
    handed_out = []

    def record_fsync(fd):
        synced.append(fd)
        return real_fsync(fd)

    monkeypatch.setattr(wal_fence.os, "fsync", record_fsync)
    real_capability = wal_fence.SourceApplyCapability

    class Watching(real_capability):
        def __init__(self, *args, **kwargs):
            handed_out.append(len(synced))
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(wal_fence, "SourceApplyCapability", Watching)
    _guard(manifest, fake_docker, clock).claim()

    # Both the file and the directory entry that names it: a claim whose name
    # never reached the platter cannot stop a second claim after a power loss.
    assert handed_out and handed_out[0] >= 2


def test_a_live_capability_whose_record_is_rebound_stops_authorising(
    fake_docker, tmp_path, clock, database
):
    """Checked on every use, not once at the start.

    The record sits on disk where anything can edit it, and every field in it
    is something a later step will trust.
    """
    manifest = _fence(fake_docker, tmp_path, clock)
    capability = _guard(manifest, fake_docker, clock).claim()
    record = Path(capability.claim_record_path)
    body = json.loads(record.read_text())
    body.pop("content_sha256")
    body["lease_id"] = "someone-elses-lease"
    body["content_sha256"] = hashlib.sha256(
        json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    record.write_text(json.dumps(body))

    with pytest.raises(wal_fence.ClaimUnavailable):
        capability.check_before_source_open(db_path=str(database))


def test_a_claim_record_that_does_not_bind_to_this_manifest_is_refused(
    fake_docker, tmp_path, clock, database
):
    manifest = _fence(fake_docker, tmp_path, clock)
    claim_path = Path(f"{manifest.path}.claim.json")
    guard = _guard(manifest, fake_docker, clock)
    guard.claim()
    body = json.loads(claim_path.read_text())
    body["lease_id"] = "someone-elses-lease"
    # Re-sealed, so the digest is valid and only the BINDING is wrong. A record
    # that merely fails its checksum would prove nothing about whether the
    # binding is checked at all.
    body.pop("content_sha256")
    body["content_sha256"] = hashlib.sha256(
        json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    claim_path.write_text(json.dumps(body))

    with pytest.raises(wal_fence.ClaimUnavailable) as caught:
        guard.reattach()

    assert "lease_id" in str(caught.value)


def test_a_torn_claim_record_is_not_read_as_a_live_claim(
    fake_docker, tmp_path, clock, database
):
    manifest = _fence(fake_docker, tmp_path, clock)
    guard = _guard(manifest, fake_docker, clock)
    guard.claim()
    claim_path = Path(f"{manifest.path}.claim.json")
    text = claim_path.read_text()
    claim_path.write_text(text[: len(text) // 2])

    with pytest.raises(wal_recovery.ApplyRecordRejected):
        guard.reattach()


def test_two_concurrent_fences_leave_exactly_one_winner(
    data_dir, tmp_path, clock, database
):
    """One winner, and the loser must not have touched the host on its way out.

    A clash discovered only at the final write is a service that was stopped
    for a run that was never going to produce a manifest. The exclusion has to
    be taken for the whole operation, not for the last step of it.
    """
    import threading

    fake = FakeDocker(
        [
            FakeContainer(BACKEND_ID, SERVICE, mounts=[bind_mount(data_dir)]),
            FakeContainer(AGENT_ID, AGENT_SERVICE, mounts=[bind_mount(data_dir)]),
        ]
    )
    made, refused = [], []
    barrier = threading.Barrier(2)

    def attempt():
        kwargs = fence_kwargs(fake, tmp_path, clock=clock, data_dir=data_dir)
        barrier.wait()
        try:
            made.append(wal_fence.fence(**kwargs))
        except wal_fence.WalFenceError as exc:
            refused.append(exc)

    threads = [threading.Thread(target=attempt) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert len(made) == 1, f"{len(made)} fences claimed the same manifest path"
    assert len(refused) == 1
    # Two containers, stopped once each. The loser issued nothing.
    assert len(fake.ran("stop")) == 2
    assert len(fake.ran("update")) == 2


def test_two_record_writes_never_share_a_scratch_name(tmp_path, monkeypatch):
    """A shared scratch name is a second way for two writers to corrupt each
    other: one process's rename can publish the other's half-written bytes."""
    scratches = []
    real_replace = wal_fence.os.replace

    def watch(src, dst):
        scratches.append(src)
        return real_replace(src, dst)

    monkeypatch.setattr(wal_fence.os, "replace", watch)
    body = {"schema": wal_fence.SCHEMA, "state": "PREPARED", "scope": []}
    wal_fence._write_record(str(tmp_path / "a.json"), body)
    wal_fence._write_record(str(tmp_path / "a.json"), body, overwrite=True)

    assert len(scratches) == 2
    assert scratches[0] != scratches[1]


# ── 25. a lease may not outlive the acknowledgement behind it ───

def _reseal_manifest(path: Path, **changes):
    """Rewrite a manifest so only the FIELDS differ, not the digest."""
    body = json.loads(Path(path).read_text())
    body.pop("content_sha256")
    body.update(changes)
    body["content_sha256"] = hashlib.sha256(
        json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    Path(path).write_text(json.dumps(body, indent=2, sort_keys=True))


def test_a_lease_outlasting_the_acknowledgement_is_refused(
    fake_docker, tmp_path, clock, database, data_dir
):
    """RED A1. A signature covers a window; a lease may not reach past it.

    Asking for fifteen minutes of authority on the back of an acknowledgement
    that lapses in one second is not a rounding error — it is the fence
    quietly extending permission nobody gave. It is refused at the fence, and
    nothing on the host has been touched by the time it is.
    """
    brief = signed_ack(
        container_ids=[BACKEND_ID, AGENT_ID],
        mount_identity=mount_identity_of(data_dir),
        issued_at=clock.now - 10,
        expires_at=clock.now + 1,
    )

    with pytest.raises(wal_fence.LeaseExceedsAcknowledgement) as caught:
        _fence(
            fake_docker, tmp_path, clock,
            external_authority_ack=brief, lease_ttl_seconds=900,
        )

    assert caught.value.code == "LEASE_EXCEEDS_ACK"
    assert not fake_docker.ran("update")
    assert not fake_docker.ran("stop")
    assert not Path(_prepared_path(tmp_path)).exists()


def test_a_lease_inside_the_acknowledgement_is_allowed(
    fake_docker, tmp_path, clock, database, data_dir
):
    generous = signed_ack(
        container_ids=[BACKEND_ID, AGENT_ID],
        mount_identity=mount_identity_of(data_dir),
        issued_at=clock.now - 10,
        expires_at=clock.now + 1000,
    )

    manifest = _fence(
        fake_docker, tmp_path, clock,
        external_authority_ack=generous, lease_ttl_seconds=900,
    )

    assert manifest.data["lease"]["expires_at"] <= generous.expires_at


def test_an_acknowledgement_that_lapsed_after_the_quiesce_stops_the_claim(
    fake_docker, tmp_path, clock, database, data_dir
):
    """RED A2. The stored acknowledgement is re-read, not remembered."""
    manifest = _fence(fake_docker, tmp_path, clock, lease_ttl_seconds=900)
    lapsed = signed_ack(
        container_ids=[BACKEND_ID, AGENT_ID],
        mount_identity=mount_identity_of(data_dir),
        issued_at=clock.now - 100,
        expires_at=clock.now - 1,
    )
    _reseal_manifest(Path(manifest.path), external_authority_ack=lapsed.as_json())
    guard = wal_fence.ManifestFence.from_file(
        manifest.path, runner=fake_docker, clock=clock,
        authority_verifier=ACK_KEY, visibility_witness=ScriptedWitness(fake_docker),
    )

    with pytest.raises(wal_fence.ExternalAuthorityUnverified):
        guard.claim()


def test_an_acknowledgement_that_lapses_after_the_claim_stops_the_check(
    fake_docker, tmp_path, clock, database, data_dir
):
    manifest = _fence(fake_docker, tmp_path, clock, lease_ttl_seconds=900)
    capability = _guard(manifest, fake_docker, clock).claim()
    lapsed = signed_ack(
        container_ids=[BACKEND_ID, AGENT_ID],
        mount_identity=mount_identity_of(data_dir),
        issued_at=clock.now - 100,
        expires_at=clock.now - 1,
    )
    _reseal_manifest(Path(manifest.path), external_authority_ack=lapsed.as_json())
    capability.manifest = wal_fence.read_manifest(manifest.path)
    probes_before = len([a for a in fake_docker.calls if a[0] == "lsof"])

    with pytest.raises(wal_fence.ExternalAuthorityUnverified):
        capability.check_before_source_open(db_path=str(database))

    assert not Path(f"{manifest.path}.applying.json").exists()
    # Asked first, before any of the work: an authority that is already void
    # is no reason to go poking at the host.
    assert len([a for a in fake_docker.calls if a[0] == "lsof"]) == probes_before


def test_an_acknowledgement_that_lapses_while_the_checks_run_is_refused(
    fake_docker, tmp_path, clock, database, data_dir
):
    """Valid when the checks began, void by the time they finished.

    The checks take real time — inspects, a probe, hashing a database — and
    an authority that ran out somewhere in the middle never covered the thing
    it is about to be used for.
    """
    manifest = _fence(fake_docker, tmp_path, clock, lease_ttl_seconds=900)
    capability = _guard(manifest, fake_docker, clock).claim()
    lapsed = signed_ack(
        container_ids=[BACKEND_ID, AGENT_ID],
        mount_identity=mount_identity_of(data_dir),
        issued_at=clock.now - 100,
        expires_at=clock.now - 1,
    )
    real_inspect = fake_docker._inspect
    swapped = []

    def lapse_midway(argv):
        if not swapped:
            swapped.append(True)
            _reseal_manifest(
                Path(manifest.path), external_authority_ack=lapsed.as_json()
            )
            capability.manifest = wal_fence.read_manifest(manifest.path)
        return real_inspect(argv)

    fake_docker._inspect = lapse_midway

    with pytest.raises(wal_fence.ExternalAuthorityUnverified):
        capability.check_before_source_open(db_path=str(database))


def test_an_acknowledgement_that_lapses_after_the_check_stops_the_apply(
    fake_docker, tmp_path, clock, database, data_dir
):
    """RED A2/A3. Checked again at the last gate, and nothing is written."""
    manifest = _fence(fake_docker, tmp_path, clock, lease_ttl_seconds=900)
    capability = _guard(manifest, fake_docker, clock).claim()
    capability.check_before_source_open(db_path=str(database))
    lapsed = signed_ack(
        container_ids=[BACKEND_ID, AGENT_ID],
        mount_identity=mount_identity_of(data_dir),
        issued_at=clock.now - 100,
        expires_at=clock.now - 1,
    )
    _reseal_manifest(Path(manifest.path), external_authority_ack=lapsed.as_json())
    capability.manifest = wal_fence.read_manifest(manifest.path)

    with pytest.raises(wal_fence.ExternalAuthorityUnverified):
        capability.begin_apply()

    assert not Path(f"{manifest.path}.applying.json").exists()


def test_a_lease_that_lapses_after_the_check_stops_the_apply(
    fake_docker, tmp_path, clock, database
):
    """RED A3. The check passed; by the last gate the lease had run out."""
    manifest = _fence(fake_docker, tmp_path, clock, lease_ttl_seconds=300)
    capability = _guard(manifest, fake_docker, clock).claim()
    capability.check_before_source_open(db_path=str(database))
    clock.advance(301)

    with pytest.raises(wal_fence.FenceStale):
        capability.begin_apply()

    assert not Path(f"{manifest.path}.applying.json").exists()


def test_a_rebound_acknowledgement_is_refused(
    fake_docker, tmp_path, clock, database, data_dir
):
    """The pairing of acknowledgement and lease is recomputed, not trusted.

    A genuine signature moved onto a different lease would otherwise pass
    every field check — the signature really is valid, just not for this.
    """
    manifest = _fence(fake_docker, tmp_path, clock)
    _reseal_manifest(Path(manifest.path), ack_lease_binding="a" * 64)
    guard = wal_fence.ManifestFence.from_file(
        manifest.path, runner=fake_docker, clock=clock,
        authority_verifier=ACK_KEY, visibility_witness=ScriptedWitness(fake_docker),
    )

    with pytest.raises(wal_fence.ExternalAuthorityUnverified) as caught:
        guard.claim()

    assert "not bound to this lease" in str(caught.value)


def test_time_running_out_does_not_interrupt_an_apply_already_under_way(
    fake_docker, tmp_path, clock, database
):
    """Once APPLYING has begun, the clock is no longer a reason to stop.

    Abandoning a checkpoint half way because a lease lapsed would leave the
    database in exactly the state the whole procedure exists to avoid.
    """
    manifest = _fence(fake_docker, tmp_path, clock, lease_ttl_seconds=300)
    capability = _guard(manifest, fake_docker, clock).claim()
    capability.check_before_source_open(db_path=str(database))
    capability.begin_apply()

    clock.advance(10_000)

    capability.complete()
    assert capability.is_active is False


# ── 26. one instant decides, and it decides at the transition ───

class _ReadingClock:
    """A clock that moves on every read, and remembers what it handed out.

    Real clocks do exactly this. Two reads inside one decision are two
    different instants, and an expiry that falls between them is invisible to
    both halves of the decision.
    """

    def __init__(self, start, step=1.0):
        self.now = float(start)
        self.step = float(step)
        self.readings = []

    def __call__(self):
        self.readings.append(self.now)
        self.now += self.step
        return self.readings[-1]


def test_the_apply_decision_is_taken_at_one_instant_and_stamped_with_it(
    fake_docker, tmp_path, clock, database
):
    """RED B1a. `begin_apply` must not straddle its own authority check.

    The lease is read at one instant, the acknowledgement at the next, and the
    APPLYING record is stamped at a third. With the expiry sitting between
    those reads, nothing refuses — and what lands on disk says the apply began
    after the authority behind it had already ended.
    """
    manifest = _fence(fake_docker, tmp_path, clock, lease_ttl_seconds=300)
    capability = _guard(manifest, fake_docker, clock).claim()
    capability.check_before_source_open(db_path=str(database))

    expires_at = float(manifest.data["lease"]["expires_at"])
    stepping = _ReadingClock(expires_at - 0.5)
    capability.clock = stepping

    capability.begin_apply()

    record = json.loads(Path(capability.claim_record_path).read_text())
    assert stepping.readings, "the apply decided without reading the clock at all"
    # One reading decided it, and that same reading is what was written down.
    assert stepping.readings == [expires_at - 0.5]
    assert record["apply_started_at"] == stepping.readings[0]
    assert record["apply_started_at"] <= expires_at


def test_the_acknowledgement_is_judged_at_the_same_instant_as_the_lease(
    fake_docker, tmp_path, clock, database, monkeypatch
):
    """RED B1b. Both halves of the authority answer to one `now`.

    A lease checked against one reading and an acknowledgement against a later
    one is not one decision; it is two, with a window between them that either
    can be true in.
    """
    manifest = _fence(fake_docker, tmp_path, clock, lease_ttl_seconds=300)
    capability = _guard(manifest, fake_docker, clock).claim()
    capability.check_before_source_open(db_path=str(database))

    seen = []
    real_verify = wal_fence._verify_ack

    def recording(ack, verifier, *, now, **rest):
        seen.append(now)
        return real_verify(ack, verifier, now=now, **rest)

    monkeypatch.setattr(wal_fence, "_verify_ack", recording)
    stepping = _ReadingClock(float(manifest.data["lease"]["expires_at"]) - 0.5)
    capability.clock = stepping

    capability.begin_apply()

    assert seen == [stepping.readings[0]]


def test_a_lease_that_runs_out_during_the_host_inventory_creates_no_claim(
    fake_docker, tmp_path, clock, database
):
    """RED B1c. The claim is judged where it is taken, not where it started.

    `claim()` checks the lease, then goes and inventories every container on
    the host — inspects, mount identities, socket holders. On a busy host that
    takes real time, and the current code never looks at the clock again
    before it creates the CLAIMED file.
    """
    manifest = _fence(fake_docker, tmp_path, clock, lease_ttl_seconds=300)
    guard = _guard(manifest, fake_docker, clock)
    real_ps = fake_docker._ps

    def slow_inventory(argv):
        clock.advance(301)
        return real_ps(argv)

    fake_docker._ps = slow_inventory

    with pytest.raises(wal_fence.FenceStale):
        guard.claim()

    assert not Path(f"{manifest.path}.claim.json").exists()
    assert not Path(f"{manifest.path}.applying.json").exists()


def test_the_claim_is_stamped_with_the_moment_it_was_actually_authorised(
    fake_docker, tmp_path, clock, database
):
    """RED B1d. `claimed_at` is the judgement, not the intention.

    A stamp taken before the inventory says the claim was authorised at a time
    when nothing had yet been checked. The instant that authorised it is the
    one after the inventory came back.
    """
    manifest = _fence(fake_docker, tmp_path, clock, lease_ttl_seconds=900)
    guard = _guard(manifest, fake_docker, clock)
    real_ps = fake_docker._ps

    def slow_inventory(argv):
        clock.advance(60)
        return real_ps(argv)

    fake_docker._ps = slow_inventory
    started = clock.now

    capability = guard.claim()

    record = json.loads(Path(capability.claim_record_path).read_text())
    assert record["claimed_at"] >= started + 60


def test_an_acknowledgement_that_lapses_during_the_inventory_creates_no_claim(
    fake_docker, tmp_path, clock, database, data_dir
):
    """RED B1e. The same re-judgement covers the acknowledgement too."""
    manifest = _fence(fake_docker, tmp_path, clock, lease_ttl_seconds=900)
    guard = _guard(manifest, fake_docker, clock)
    lapsed = signed_ack(
        container_ids=[BACKEND_ID, AGENT_ID],
        mount_identity=mount_identity_of(data_dir),
        issued_at=clock.now - 100,
        expires_at=clock.now + 30,
    )
    real_ps = fake_docker._ps

    def lapse_during_inventory(argv):
        _reseal_manifest(Path(manifest.path), external_authority_ack=lapsed.as_json())
        guard.manifest = wal_fence.read_manifest(manifest.path)
        clock.advance(60)
        return real_ps(argv)

    fake_docker._ps = lapse_during_inventory

    with pytest.raises(wal_fence.ExternalAuthorityUnverified):
        guard.claim()

    assert not Path(f"{manifest.path}.claim.json").exists()


# ── 27. a terminal record is only spent once it is durable ───

class _DirFsyncFailure:
    """A host on which a file's bytes land but its directory entry does not.

    This is the failure `_write_record` cannot paper over: the rename has
    already happened, so the terminal record is right there for this process
    to read, and only the fsync that would make its NAME survive a power loss
    has failed.
    """

    def __init__(self, monkeypatch):
        self.armed = True
        self.real = os.fsync
        monkeypatch.setattr(wal_fence.os, "fsync", self)

    def __call__(self, fd):
        if self.armed and stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError(5, "Input/output error")
        return self.real(fd)


def _spent_fence_with_undurable_terminal(fake_docker, tmp_path, clock, database,
                                         monkeypatch):
    """Run a claim to a terminal state whose directory entry never synced."""
    manifest = _fence(fake_docker, tmp_path, clock)
    capability = _guard(manifest, fake_docker, clock).claim()
    capability.check_before_source_open(db_path=str(database))
    capability.begin_apply()

    failing = _DirFsyncFailure(monkeypatch)
    with pytest.raises(OSError):
        capability.complete()

    # The file is visible. That is exactly the problem.
    assert Path(f"{manifest.path}.claim.done.json").exists()
    return manifest, capability, failing


def test_release_refuses_a_terminal_record_it_cannot_confirm_is_durable(
    fake_docker, tmp_path, clock, database, monkeypatch
):
    """RED B3a. Presence is not durability, and release acts on the host.

    Restoring the restart policies is the moment the containers can come back.
    Doing it on the strength of a terminal record whose name may not survive a
    power loss is the fence handing the database to a writer over a claim that
    could still read as live.
    """
    manifest, _, _ = _spent_fence_with_undurable_terminal(
        fake_docker, tmp_path, clock, database, monkeypatch
    )
    updates = len(fake_docker.ran("update"))

    with pytest.raises(wal_fence.ReleaseFailed):
        wal_fence.release(
            manifest_path=manifest.path,
            runner=fake_docker,
            clock=clock,
            start_containers=True,
        )

    assert len(fake_docker.ran("update")) == updates
    assert not fake_docker.ran("start")
    assert not Path(f"{manifest.path}.release.json").exists()


def test_release_proceeds_once_the_terminal_record_is_confirmed_durable(
    fake_docker, tmp_path, clock, database, monkeypatch
):
    """RED B3b. Fail-closed, not permanently closed.

    The refusal is about an unanswered question. Once the record and its
    directory do sync, the same release is exactly the ordinary one.
    """
    manifest, _, failing = _spent_fence_with_undurable_terminal(
        fake_docker, tmp_path, clock, database, monkeypatch
    )
    with pytest.raises(wal_fence.ReleaseFailed):
        wal_fence.release(manifest_path=manifest.path, runner=fake_docker, clock=clock)

    failing.armed = False

    record = wal_fence.release(
        manifest_path=manifest.path, runner=fake_docker, clock=clock
    )

    assert sorted(record.restored) == sorted([AGENT_ID, BACKEND_ID])
    assert Path(f"{manifest.path}.release.json").exists()


def _reseal_apply_record(path: Path, **changes):
    """Rewrite a claim record so only the FIELDS differ, not the digest."""
    body = json.loads(Path(path).read_text())
    body.pop("content_sha256")
    body.update(changes)
    body["content_sha256"] = hashlib.sha256(
        json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    Path(path).write_text(json.dumps(body, indent=2, sort_keys=True))


def test_release_refuses_a_terminal_record_bound_to_another_claim(
    fake_docker, tmp_path, clock, database
):
    """RED B3c. The record has to be about THIS claim, not merely present."""
    manifest = _fence(fake_docker, tmp_path, clock)
    capability = _guard(manifest, fake_docker, clock).claim()
    capability.check_before_source_open(db_path=str(database))
    capability.begin_apply()
    capability.complete()
    _reseal_apply_record(
        Path(f"{manifest.path}.claim.done.json"), claim_id="b" * 32
    )
    updates = len(fake_docker.ran("update"))

    with pytest.raises(wal_fence.ReleaseFailed):
        wal_fence.release(manifest_path=manifest.path, runner=fake_docker, clock=clock)

    assert len(fake_docker.ran("update")) == updates
    assert not fake_docker.ran("start")


def test_release_refuses_a_terminal_record_that_is_not_terminal(
    fake_docker, tmp_path, clock, database
):
    """RED B3d. A file at the terminal name is not a terminal state."""
    manifest = _fence(fake_docker, tmp_path, clock)
    capability = _guard(manifest, fake_docker, clock).claim()
    capability.check_before_source_open(db_path=str(database))
    capability.begin_apply()
    capability.complete()
    _reseal_apply_record(
        Path(f"{manifest.path}.claim.done.json"), state=wal_recovery.APPLYING
    )
    updates = len(fake_docker.ran("update"))

    with pytest.raises(wal_fence.ReleaseFailed):
        wal_fence.release(manifest_path=manifest.path, runner=fake_docker, clock=clock)

    assert len(fake_docker.ran("update")) == updates


def test_release_refuses_a_terminal_record_that_does_not_match_its_digest(
    fake_docker, tmp_path, clock, database
):
    """RED B3e. A record edited after it was written is not a smaller truth."""
    manifest = _fence(fake_docker, tmp_path, clock)
    capability = _guard(manifest, fake_docker, clock).claim()
    capability.check_before_source_open(db_path=str(database))
    capability.begin_apply()
    capability.complete()
    done = Path(f"{manifest.path}.claim.done.json")
    body = json.loads(done.read_text())
    body["lease_id"] = "not-the-lease-this-fence-holds"
    done.write_text(json.dumps(body, indent=2, sort_keys=True))
    updates = len(fake_docker.ran("update"))

    with pytest.raises(wal_fence.ReleaseFailed):
        wal_fence.release(manifest_path=manifest.path, runner=fake_docker, clock=clock)

    assert len(fake_docker.ran("update")) == updates


# ── 28. "I could not find out" is not "it is not there" ───

class _UnreadablePath:
    """A host where `lstat` on one path answers with an error, not an answer.

    An unreadable directory, a failing disk, a mount that went away: `lstat`
    raises, and `os.path.lexists` turns every one of those into `False`. On
    the release path that `False` reads as "no claim was ever taken here",
    which is the one answer that lets the containers come back.
    """

    def __init__(self, monkeypatch, target, errno_value=errno.EIO):
        self.target = str(target)
        self.errno = errno_value
        self.real = os.lstat
        monkeypatch.setattr(wal_fence.os, "lstat", self)

    def __call__(self, path, *args, **kwargs):
        if str(path) == self.target:
            raise OSError(self.errno, os.strerror(self.errno))
        return self.real(path, *args, **kwargs)


def _release_leaves_the_host_alone(fake_docker, **kwargs):
    """Call release, and report what it did to the host either way.

    Deliberately not `pytest.raises`: what is under test is what did NOT
    happen to the containers, and that has to be checked whether the call
    refused or returned.
    """
    updates = len(fake_docker.ran("update"))
    starts = len(fake_docker.ran("start"))
    caught = None
    try:
        wal_fence.release(runner=fake_docker, **kwargs)
    except BaseException as exc:  # noqa: BLE001 - the point is to inspect it
        caught = exc
    assert len(fake_docker.ran("update")) == updates, (
        "restart policies were restored while it was unknown whether a claim "
        "record exists"
    )
    assert len(fake_docker.ran("start")) == starts, (
        "containers were started while it was unknown whether a claim record "
        "exists"
    )
    return caught


def test_release_refuses_when_it_cannot_tell_whether_a_claim_is_live(
    fake_docker, tmp_path, clock, database, monkeypatch
):
    """RED C1a. An unreadable claim path must not read as an absent one.

    The claim here is genuinely live — an apply is in flight — and the only
    thing that has gone wrong is that the fence cannot see the record. Reading
    that silence as "nothing was ever claimed" restores the restart policies
    and starts the containers on top of a running checkpoint.
    """
    manifest = _fence(fake_docker, tmp_path, clock)
    capability = _guard(manifest, fake_docker, clock).claim()
    capability.check_before_source_open(db_path=str(database))
    capability.begin_apply()

    _UnreadablePath(monkeypatch, f"{manifest.path}.claim.json")

    caught = _release_leaves_the_host_alone(
        fake_docker,
        manifest_path=manifest.path,
        clock=clock,
        start_containers=True,
    )
    assert isinstance(caught, wal_fence.WalFenceError)
    assert not Path(f"{manifest.path}.release.json").exists()


def test_release_refuses_when_it_cannot_tell_whether_a_claim_finished(
    fake_docker, tmp_path, clock, database, monkeypatch
):
    """RED C1b. The same question, asked of the terminal record.

    Here the fall-through is fail-closed by luck rather than by design: an
    unreadable terminal record makes a finished claim look live, so release
    stops — but it stops with the wrong diagnosis, telling the operator to
    wait for an apply that finished. The refusal has to name the question it
    could not answer.
    """
    manifest = _fence(fake_docker, tmp_path, clock)
    capability = _guard(manifest, fake_docker, clock).claim()
    capability.check_before_source_open(db_path=str(database))
    capability.begin_apply()
    capability.complete()

    _UnreadablePath(monkeypatch, f"{manifest.path}.claim.done.json")

    caught = _release_leaves_the_host_alone(
        fake_docker,
        manifest_path=manifest.path,
        clock=clock,
        start_containers=True,
    )
    assert isinstance(caught, wal_fence.ReleaseFailed)
    assert ".claim.done.json" in str(caught)


def test_release_refuses_when_it_cannot_tell_whether_it_already_released(
    fake_docker, tmp_path, clock, database, monkeypatch
):
    """RED C1c. An unreadable release record is a second release.

    `AlreadyReleased` is the only thing standing between a repeated call and a
    second pass of `docker update` and `docker start` over containers somebody
    may have deliberately arranged since. An `lstat` that errors removes it.
    """
    manifest = _fence(fake_docker, tmp_path, clock)
    capability = _guard(manifest, fake_docker, clock).claim()
    capability.check_before_source_open(db_path=str(database))
    capability.begin_apply()
    capability.complete()
    wal_fence.release(manifest_path=manifest.path, runner=fake_docker, clock=clock)
    before = Path(f"{manifest.path}.release.json").read_text()

    _UnreadablePath(monkeypatch, f"{manifest.path}.release.json")

    caught = _release_leaves_the_host_alone(
        fake_docker,
        manifest_path=manifest.path,
        clock=clock,
        start_containers=True,
    )
    assert isinstance(caught, wal_fence.WalFenceError)
    assert Path(f"{manifest.path}.release.json").read_text() == before


def test_release_refuses_when_it_cannot_tell_whether_a_manifest_is_there(
    fake_docker, tmp_path, clock, database, monkeypatch
):
    """RED C1d. A lease that cannot be read is not a fence without one.

    The lease id read here is what the terminal record is later checked
    against. An unreadable manifest silently becomes an empty lease id, and
    the check it was meant to feed quietly stops checking anything.
    """
    manifest = _fence(fake_docker, tmp_path, clock)
    capability = _guard(manifest, fake_docker, clock).claim()
    capability.check_before_source_open(db_path=str(database))
    capability.begin_apply()
    capability.complete()

    _UnreadablePath(monkeypatch, manifest.path)

    caught = _release_leaves_the_host_alone(
        fake_docker,
        manifest_path=manifest.path,
        clock=clock,
        start_containers=True,
    )
    assert isinstance(caught, wal_fence.WalFenceError)


def test_release_still_works_when_no_claim_was_ever_taken(
    fake_docker, tmp_path, clock, database
):
    """The ordinary absent case keeps behaving exactly as it did."""
    manifest = _fence(fake_docker, tmp_path, clock)

    released = wal_fence.release(
        manifest_path=manifest.path, runner=fake_docker, clock=clock
    )

    assert sorted(released.restored) == sorted([AGENT_ID, BACKEND_ID])


# ── 29. a capability closes out its own claim, or none ───

def _forged_capability(manifest, fake_docker, clock, claim_id="f" * 32):
    """A capability built by hand, carrying a claim id nobody ever issued."""
    return wal_fence.SourceApplyCapability(
        manifest,
        claim_id,
        runner=fake_docker,
        clock=clock,
        authority_verifier=ACK_KEY,
        visibility_witness=ScriptedWitness(fake_docker),
    )


def test_a_capability_for_a_claim_nobody_issued_writes_no_terminal_record(
    fake_docker, tmp_path, clock, database
):
    """RED C2a. `_finish` writes on nothing but its own say-so.

    Every other transition on this capability reads the claim record and
    checks it binds here first. Closing one out does not — so an object
    holding a claim id that was never issued can put a FAILED record at the
    terminal name.
    """
    manifest = _fence(fake_docker, tmp_path, clock)
    real = _guard(manifest, fake_docker, clock).claim()
    real.check_before_source_open(db_path=str(database))
    real.begin_apply()

    forged = _forged_capability(manifest, fake_docker, clock)

    with pytest.raises(wal_fence.ClaimUnavailable):
        forged.fail("a failure the live claim never had")

    assert not Path(f"{manifest.path}.claim.done.json").exists()
    live = json.loads(Path(real.claim_record_path).read_text())
    assert live["claim_id"] == real.claim_id
    assert live["state"] == wal_recovery.APPLYING
    assert real.is_active is True


def test_a_forged_terminal_record_cannot_lock_the_real_fence_shut(
    fake_docker, tmp_path, clock, database
):
    """RED C2b. The harm, end to end.

    A terminal record about some other claim occupies the one name the real
    claim has to write to. The real capability's own `complete()` then reads
    the file as "already finished", and `release` refuses forever because the
    record it finds is about a claim this fence never issued: the containers
    stay down and the restart policies stay pinned to `no`.
    """
    manifest = _fence(fake_docker, tmp_path, clock)
    real = _guard(manifest, fake_docker, clock).claim()
    real.check_before_source_open(db_path=str(database))
    real.begin_apply()

    with contextlib.suppress(wal_fence.WalFenceError):
        _forged_capability(manifest, fake_docker, clock).fail("not this claim")

    assert not Path(f"{manifest.path}.claim.done.json").exists()

    real.complete()
    released = wal_fence.release(
        manifest_path=manifest.path, runner=fake_docker, clock=clock
    )
    assert sorted(released.restored) == sorted([AGENT_ID, BACKEND_ID])


def test_a_foreign_capability_cannot_ride_an_existing_terminal_record(
    fake_docker, tmp_path, clock, database
):
    """RED C2c. Idempotence belongs to the claim that earned it.

    Returning quietly because a file is there tells a capability that never
    held this claim that it has just closed one out.
    """
    manifest = _fence(fake_docker, tmp_path, clock)
    real = _guard(manifest, fake_docker, clock).claim()
    real.check_before_source_open(db_path=str(database))
    real.begin_apply()
    real.complete()
    done = Path(f"{manifest.path}.claim.done.json")
    before = done.read_text()

    forged = _forged_capability(manifest, fake_docker, clock)

    with pytest.raises(wal_fence.ClaimUnavailable):
        forged.complete()

    assert done.read_text() == before


def test_a_terminal_record_edited_underneath_is_not_read_as_idempotence(
    fake_docker, tmp_path, clock, database
):
    """RED C2e. The record is on disk, where anything can rewrite it.

    Closing out a second time returns quietly because a file is already at the
    terminal name. If that file has since been made to say it is about another
    claim, returning quietly is this capability accepting somebody else's
    record as its own completion — and `release` will then refuse over it.
    """
    manifest = _fence(fake_docker, tmp_path, clock)
    real = _guard(manifest, fake_docker, clock).claim()
    real.check_before_source_open(db_path=str(database))
    real.begin_apply()
    real.complete()
    done = Path(f"{manifest.path}.claim.done.json")
    _reseal_apply_record(done, claim_id="c" * 32)
    tampered = done.read_text()

    with pytest.raises(wal_fence.ClaimUnavailable):
        real.complete()

    assert done.read_text() == tampered


def test_closing_the_same_claim_out_twice_is_still_a_no_op(
    fake_docker, tmp_path, clock, database
):
    """Idempotence for the claim that does bind here is unchanged."""
    manifest = _fence(fake_docker, tmp_path, clock)
    real = _guard(manifest, fake_docker, clock).claim()
    real.check_before_source_open(db_path=str(database))
    real.begin_apply()

    real.complete()
    before = Path(f"{manifest.path}.claim.done.json").read_text()
    real.complete()

    assert Path(f"{manifest.path}.claim.done.json").read_text() == before


# ── 30. existing is not the same as readable ───

class _UnreadableFile:
    """A file whose bytes cannot be read, though its name is right there.

    `lstat` succeeds and `open` does not — an unreadable mode, a disk
    returning EIO. Everything that asks "is it there?" gets a yes, and the
    record itself is still unknown.
    """

    def __init__(self, monkeypatch, target, errno_value=errno.EIO):
        self.target = str(target)
        self.errno = errno_value
        self.real = open
        monkeypatch.setattr(wal_recovery, "open", self, raising=False)

    def __call__(self, path, *args, **kwargs):
        if str(path) == self.target:
            raise OSError(self.errno, os.strerror(self.errno))
        return self.real(path, *args, **kwargs)


def _break_digest(path: Path) -> None:
    """Edit a record's body and leave its old digest in place."""
    body = json.loads(Path(path).read_text())
    body["db_path"] = body.get("db_path", "") + "-tampered"
    Path(path).write_text(json.dumps(body, indent=2, sort_keys=True))


def _spent_fence(fake_docker, tmp_path, clock, database):
    """A fence whose one claim ran a full apply and finished."""
    manifest = _fence(fake_docker, tmp_path, clock)
    capability = _guard(manifest, fake_docker, clock).claim()
    capability.check_before_source_open(db_path=str(database))
    capability.begin_apply()
    capability.complete()
    return manifest, capability


def test_release_refuses_a_manifest_that_is_present_but_unreadable(
    fake_docker, tmp_path, clock, database
):
    """RED D1a. A lease that cannot be read is not an empty lease.

    The lease id read from the manifest is what the terminal record is later
    checked against. Swallowing a `ManifestRejected` turns it into `""`, and
    the check it feeds quietly stops checking — on a manifest that is sitting
    right there, merely corrupt.
    """
    manifest, _ = _spent_fence(fake_docker, tmp_path, clock, database)
    _break_digest(Path(manifest.path))

    caught = _release_leaves_the_host_alone(
        fake_docker,
        manifest_path=manifest.path,
        clock=clock,
        start_containers=True,
    )
    assert isinstance(caught, wal_fence.ReleaseFailed)
    assert not Path(f"{manifest.path}.release.json").exists()


def test_release_refuses_a_live_claim_record_that_fails_its_own_digest(
    fake_docker, tmp_path, clock, database
):
    """RED D1b. The live claim is what the terminal record is bound TO.

    Suppressing a rejected read here does not skip a check; it removes the
    `claim_id` and `lease_id` the terminal record was going to be compared
    against, and what is left passes.
    """
    manifest, _ = _spent_fence(fake_docker, tmp_path, clock, database)
    _break_digest(Path(f"{manifest.path}.claim.json"))

    caught = _release_leaves_the_host_alone(
        fake_docker,
        manifest_path=manifest.path,
        clock=clock,
        start_containers=True,
    )
    assert isinstance(caught, wal_fence.ReleaseFailed)


def test_release_refuses_a_live_claim_record_that_is_truncated(
    fake_docker, tmp_path, clock, database
):
    """RED D1c. A torn write is not a smaller claim."""
    manifest, _ = _spent_fence(fake_docker, tmp_path, clock, database)
    claim = Path(f"{manifest.path}.claim.json")
    claim.write_text(claim.read_text()[: len(claim.read_text()) // 2])

    caught = _release_leaves_the_host_alone(
        fake_docker,
        manifest_path=manifest.path,
        clock=clock,
        start_containers=True,
    )
    assert isinstance(caught, wal_fence.ReleaseFailed)


def test_release_refuses_a_live_claim_record_it_cannot_read(
    fake_docker, tmp_path, clock, database, monkeypatch
):
    """RED D1d. EIO on the read, after the existence check said yes."""
    manifest, _ = _spent_fence(fake_docker, tmp_path, clock, database)
    _UnreadableFile(monkeypatch, f"{manifest.path}.claim.json")

    caught = _release_leaves_the_host_alone(
        fake_docker,
        manifest_path=manifest.path,
        clock=clock,
        start_containers=True,
    )
    assert isinstance(caught, wal_fence.ReleaseFailed)


def test_release_refuses_a_terminal_record_with_no_live_claim_behind_it(
    fake_docker, tmp_path, clock, database
):
    """RED D1e. A terminal record alone proves nothing about which claim.

    Without the live claim there is nothing to bind the terminal record's
    `claim_id` to, and a record that binds to nothing is not evidence that
    this fence's claim finished.
    """
    manifest, _ = _spent_fence(fake_docker, tmp_path, clock, database)
    Path(f"{manifest.path}.claim.json").unlink()

    caught = _release_leaves_the_host_alone(
        fake_docker,
        manifest_path=manifest.path,
        clock=clock,
        start_containers=True,
    )
    assert isinstance(caught, wal_fence.ReleaseFailed)


def test_release_from_a_prepared_record_alone_is_unchanged(
    fake_docker, tmp_path, clock, database
):
    """A quiesce that died before writing a manifest still releases.

    Nothing was ever claimed, so there is nothing to prove about a claim —
    which is exactly the case the checks above must not swallow.
    """
    manifest = _fence(fake_docker, tmp_path, clock)
    Path(manifest.path).unlink()

    released = wal_fence.release(
        prepared_path=_prepared_path(tmp_path), runner=fake_docker, clock=clock
    )

    assert sorted(released.restored) == sorted([AGENT_ID, BACKEND_ID])


# ── 31. idempotence is about the outcome, not just the claim ───

def test_failing_the_same_claim_out_twice_is_still_a_no_op(
    fake_docker, tmp_path, clock, database
):
    """A repeated failure is the same failure."""
    manifest = _fence(fake_docker, tmp_path, clock)
    real = _guard(manifest, fake_docker, clock).claim()

    real.fail("the run stopped")
    before = Path(f"{manifest.path}.claim.done.json").read_text()
    real.fail("the run stopped")

    assert Path(f"{manifest.path}.claim.done.json").read_text() == before


def test_a_completed_claim_cannot_afterwards_be_recorded_as_failed(
    fake_docker, tmp_path, clock, database
):
    """RED D2a. The durable answer is COMPLETED; nothing may say otherwise.

    A second run over a spent capability fails — correctly — and then closes
    the claim out as FAILED. The record on disk still says COMPLETED, so the
    run is handed back a `claim_outcome` its own fence disagrees with.
    """
    manifest, real = _spent_fence(fake_docker, tmp_path, clock, database)
    done = Path(f"{manifest.path}.claim.done.json")
    before = done.read_text()
    live_before = Path(real.claim_record_path).read_text()

    with pytest.raises(wal_fence.ClaimSpent):
        real.fail("a later run decided this had failed")

    assert done.read_text() == before
    assert Path(real.claim_record_path).read_text() == live_before
    assert json.loads(before)["state"] == wal_recovery.COMPLETED


def test_a_failed_claim_cannot_afterwards_be_recorded_as_completed(
    fake_docker, tmp_path, clock, database
):
    """RED D2b. And the other way round."""
    manifest = _fence(fake_docker, tmp_path, clock)
    real = _guard(manifest, fake_docker, clock).claim()
    real.fail("the run stopped before the apply")
    done = Path(f"{manifest.path}.claim.done.json")
    before = done.read_text()

    with pytest.raises(wal_fence.ClaimSpent):
        real.complete()

    assert done.read_text() == before


def test_a_non_terminal_record_at_the_terminal_name_is_not_idempotence(
    fake_docker, tmp_path, clock, database
):
    """RED D2c. APPLYING at the terminal name is a half-written state.

    It binds to this claim perfectly — it is this claim's own record, put
    where the terminal one goes — and treating it as "already finished" is
    how a claim that never finished is reported as spent.
    """
    manifest = _fence(fake_docker, tmp_path, clock)
    real = _guard(manifest, fake_docker, clock).claim()
    real.check_before_source_open(db_path=str(database))
    real.begin_apply()
    done = Path(f"{manifest.path}.claim.done.json")
    shutil.copyfile(real.claim_record_path, done)
    before = done.read_text()
    assert json.loads(before)["state"] == wal_recovery.APPLYING

    with pytest.raises(wal_fence.ClaimSpent):
        real.complete()

    assert done.read_text() == before


def test_a_completion_needs_an_apply_to_have_begun(
    fake_docker, tmp_path, clock, database
):
    """RED D2d. A completion says one pass over the source finished.

    A claim still sitting at CLAIMED has never opened the source, so there is
    no pass for a completion to be about. That run failed; it did not succeed
    quietly.
    """
    manifest = _fence(fake_docker, tmp_path, clock)
    real = _guard(manifest, fake_docker, clock).claim()

    with pytest.raises(wal_fence.ClaimSpent):
        real.complete()

    assert not Path(f"{manifest.path}.claim.done.json").exists()


def test_failing_a_claim_that_never_began_an_apply_still_works(
    fake_docker, tmp_path, clock, database
):
    """A recovery can stop long before the source, and must still close out."""
    manifest = _fence(fake_docker, tmp_path, clock)
    real = _guard(manifest, fake_docker, clock).claim()

    real.fail("the backup could not be verified")

    record = json.loads(Path(f"{manifest.path}.claim.done.json").read_text())
    assert record["state"] == wal_recovery.FAILED
    assert real.is_active is False


# ── 32. a claim's other records are not a fence without its manifest ───

def test_release_refuses_when_the_manifest_is_gone_but_a_claim_remains(
    fake_docker, tmp_path, clock, database
):
    """RED E1a. Losing the manifest must not look like never having claimed.

    A claim leaves three files behind, and the manifest is the one that says
    which lease and which database they were about. With it gone the other
    two bind to nothing — and the fence reads as a quiesce that never got as
    far as a claim, which is precisely the case where release goes ahead.
    """
    manifest, _ = _spent_fence(fake_docker, tmp_path, clock, database)
    Path(manifest.path).unlink()
    assert Path(f"{manifest.path}.claim.json").exists()
    assert Path(f"{manifest.path}.claim.done.json").exists()

    caught = _release_leaves_the_host_alone(
        fake_docker,
        manifest_path=manifest.path,
        clock=clock,
        start_containers=True,
    )
    assert isinstance(caught, wal_fence.ReleaseFailed)
    assert not Path(f"{manifest.path}.release.json").exists()


def test_release_refuses_when_the_manifest_is_gone_and_a_claim_is_live(
    fake_docker, tmp_path, clock, database
):
    """RED E1b. The same, with the claim still in flight.

    Fail-closed either way, but the refusal has to be about the manifest that
    is missing rather than about an apply the fence can no longer identify.
    """
    manifest = _fence(fake_docker, tmp_path, clock)
    capability = _guard(manifest, fake_docker, clock).claim()
    capability.check_before_source_open(db_path=str(database))
    capability.begin_apply()
    Path(manifest.path).unlink()

    caught = _release_leaves_the_host_alone(
        fake_docker,
        manifest_path=manifest.path,
        clock=clock,
        start_containers=True,
    )
    assert isinstance(caught, wal_fence.ReleaseFailed)
    assert not Path(f"{manifest.path}.release.json").exists()


def test_release_with_a_manifest_and_a_finished_claim_is_unchanged(
    fake_docker, tmp_path, clock, database
):
    """The ordinary path: everything present, everything checkable."""
    manifest, _ = _spent_fence(fake_docker, tmp_path, clock, database)

    released = wal_fence.release(
        manifest_path=manifest.path, runner=fake_docker, clock=clock
    )

    assert sorted(released.restored) == sorted([AGENT_ID, BACKEND_ID])
    assert Path(f"{manifest.path}.release.json").exists()


# ── 33. which outcomes a live claim still has left ───

def test_a_lost_terminal_record_does_not_let_a_completed_claim_be_failed(
    fake_docker, tmp_path, clock, database
):
    """RED E2a. A missing terminal record is not a claim that never finished.

    `complete()` already asks what the live claim says; `fail()` does not. So
    when the terminal record is the file that goes missing, a later run walks
    up to a claim whose own record reads COMPLETED and writes a fresh FAILED
    over the top — turning a recovery that worked into one that did not.
    """
    manifest, real = _spent_fence(fake_docker, tmp_path, clock, database)
    done = Path(f"{manifest.path}.claim.done.json")
    done.unlink()
    live_before = Path(real.claim_record_path).read_text()
    assert json.loads(live_before)["state"] == wal_recovery.COMPLETED

    with pytest.raises(wal_fence.ClaimSpent):
        real.fail("a later run decided this had failed")

    assert not done.exists()
    assert Path(real.claim_record_path).read_text() == live_before


def test_a_lost_terminal_record_does_not_let_a_failed_claim_be_failed_again(
    fake_docker, tmp_path, clock, database
):
    """RED E2b. The live claim is already spent, whichever way it went."""
    manifest = _fence(fake_docker, tmp_path, clock)
    real = _guard(manifest, fake_docker, clock).claim()
    real.fail("the backup could not be verified")
    done = Path(f"{manifest.path}.claim.done.json")
    done.unlink()
    live_before = Path(real.claim_record_path).read_text()
    assert json.loads(live_before)["state"] == wal_recovery.FAILED

    with pytest.raises(wal_fence.ClaimSpent):
        real.fail("and again")

    assert not done.exists()
    assert Path(real.claim_record_path).read_text() == live_before


def test_failing_a_claim_that_is_applying_still_works(
    fake_docker, tmp_path, clock, database
):
    """A recovery that dies mid-apply still has to close its claim out."""
    manifest = _fence(fake_docker, tmp_path, clock)
    real = _guard(manifest, fake_docker, clock).claim()
    real.check_before_source_open(db_path=str(database))
    real.begin_apply()

    real.fail("the source checkpoint did not complete")

    record = json.loads(Path(f"{manifest.path}.claim.done.json").read_text())
    assert record["state"] == wal_recovery.FAILED
    assert real.is_active is False


def test_completing_a_claim_that_is_applying_still_works(
    fake_docker, tmp_path, clock, database
):
    """And the ordinary success path is untouched."""
    manifest = _fence(fake_docker, tmp_path, clock)
    real = _guard(manifest, fake_docker, clock).claim()
    real.check_before_source_open(db_path=str(database))
    real.begin_apply()

    real.complete()

    record = json.loads(Path(f"{manifest.path}.claim.done.json").read_text())
    assert record["state"] == wal_recovery.COMPLETED


# ── 12. the module's own boundaries ──────────────────

def test_the_fence_module_imports_only_the_standard_library():
    tree = ast.parse(MODULE_PY.read_text())
    roots = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.split(".")[0])

    # The fence may depend on the recovery module: that is the direction the
    # contract runs, since `app.wal_recovery` defines the capability this
    # issues. The reverse would tie recovery to Docker, and its own suite
    # forbids it.
    assert roots - {"app"} <= sys.stdlib_module_names, (
        f"non-stdlib imports: {sorted(roots - {'app'})}"
    )
    app_modules = {
        node.module
        for node in ast.walk(ast.parse(MODULE_PY.read_text()))
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("app")
    }
    assert app_modules == {"app.wal_recovery"}, app_modules


def test_the_fence_module_never_removes_a_wal_or_a_shared_memory_file():
    source = MODULE_PY.read_text()
    tree = ast.parse(source)
    called = {
        ast.unparse(node.func) for node in ast.walk(tree) if isinstance(node, ast.Call)
    }

    assert not (called & {"os.remove", "os.unlink", "shutil.rmtree", "Path.unlink"})
    upper = source.upper()
    assert "-SHM" not in upper or "UNLINK" not in upper
