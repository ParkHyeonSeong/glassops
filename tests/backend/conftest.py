"""A scripted host for the WAL fence: fake `docker` and `lsof`, no daemon.

Nothing here talks to a Docker daemon, a Compose project, or a real `/app/data`.
`app.wal_fence` takes its command runner as an argument so the fence — and
every way it is supposed to refuse — can be exercised against a host we write
down instead of one we hope about.

The fake is deliberately strict about the two things that made the previous
round's green misleading:

**`docker stop` really does kill.** A finite `--timeout=N` means "SIGTERM, then
SIGKILL after N seconds", and `--signal=SIGKILL` means it immediately. A
container here that needs longer than N to shut down is recorded as
`sigkilled`, so a test can catch a stop that escalated instead of only checking
that the container ended up `exited`. `--timeout=-1` waits indefinitely, which
is what the fence must ask for; the caller's own deadline then decides how long
to wait for the CLI, and letting go of the CLI leaves the workload alone.

**`lsof` has more answers than "found" and "not found".** It can exit 1 with a
diagnostic on stderr (it looked and could not tell), exit 0 saying nothing, or
print something we cannot parse. The fake can be told to do each, and it also
reports THIS process's own open descriptors by scanning `/dev/fd` — so the
fence's visibility self-test is measuring something real rather than a
convention the fake agreed to.

Anything destructive that reaches the runner raises immediately: if the
wrapper's guard is ever removed, the host says so rather than playing along.
"""

import contextlib
import hashlib
import hmac
import json
import os
import shlex
from dataclasses import dataclass, field, replace

import pytest

PROJECT = "glassops"
SERVICE = "backend"
AGENT_SERVICE = "agent"
BACKEND_ID = "b" * 64
AGENT_ID = "a" * 64
SOCKET_PROXY_ID = "5" * 64
OTHER_PROJECT_ID = "7" * 64
UNLABELED_ID = "8" * 64
IMAGE_ID = "sha256:" + "c" * 64
IMAGE_REF = "glassops/backend@sha256:" + "d" * 64
DATA_DESTINATION = "/app/data"
DB_RELPATH = "glassops.db"
DOCKER_SOCKET = "/var/run/docker.sock"
HOST_ID = "test-host"
ACK_KEY = b"an-external-authority-signing-key-for-tests"

#: Commands that must never reach a host, however the wrapper is changed.
DESTRUCTIVE = {"kill", "rm", "rmi", "prune", "down", "volume", "--volumes", "-9"}


@dataclass
class FakeContainer:
    container_id: str
    service: str
    project: str = PROJECT
    image_id: str = IMAGE_ID
    image_ref: str = IMAGE_REF
    status: str = "running"
    running: bool = True
    pid: int = 4242
    exit_code: int = 0
    restart_policy: str = "unless-stopped"
    restart_max_retries: int = 0
    mounts: list = field(default_factory=list)
    labelled: bool = True
    #: Seconds this workload needs to shut down cleanly. A `docker stop` whose
    #: timeout is shorter than this SIGKILLs it — which is the behaviour the
    #: fence has to avoid, not tolerate.
    stop_grace_seconds: float = 1.0
    #: Recorded when a stop escalated to SIGKILL. Tests assert this stays False.
    sigkilled: bool = False
    #: When set, `docker update` reports success but the policy never moves.
    ignore_restart_update: bool = False

    def inspect(self) -> dict:
        labels = (
            {PROJECT_LABEL: self.project, SERVICE_LABEL: self.service}
            if self.labelled
            else {}
        )
        return {
            "Id": self.container_id,
            "Image": self.image_id,
            "Config": {"Image": self.image_ref, "Labels": labels},
            "State": {
                "Status": self.status,
                "Running": self.running,
                "Pid": self.pid,
                "ExitCode": self.exit_code,
            },
            "HostConfig": {
                "RestartPolicy": {
                    "Name": self.restart_policy,
                    "MaximumRetryCount": self.restart_max_retries,
                }
            },
            "Mounts": list(self.mounts),
        }


PROJECT_LABEL = "com.docker.compose.project"
SERVICE_LABEL = "com.docker.compose.service"


def bind_mount(source, destination=DATA_DESTINATION, rw=True):
    return {
        "Type": "bind",
        "Source": str(source),
        "Destination": destination,
        "RW": rw,
        "Mode": "rw" if rw else "ro",
    }


def socket_mount(rw=True):
    """The mount that lets a container start any other container on the host."""
    return bind_mount(DOCKER_SOCKET, destination=DOCKER_SOCKET, rw=rw)


@dataclass
class OpenFile:
    pid: int
    fd: int
    path: str
    command: str = "python3"


class FakeDocker:
    """A scripted host. Every argv it is handed is recorded, in order."""

    def __init__(
        self,
        containers,
        *,
        open_files=(),
        lsof_returncode=None,
        lsof_stderr="",
        lsof_stdout=None,
        blind_to_own_fds=False,
        hide_foreign_pids=False,
        cli_deadline_behaviour="honour",
    ):
        self.containers = {c.container_id: c for c in containers}
        self.open_files = list(open_files)
        self.lsof_returncode = lsof_returncode
        self.lsof_stderr = lsof_stderr
        self.lsof_stdout = lsof_stdout
        #: Simulates an `lsof` that cannot see descriptors it should be able to
        #: see — an unprivileged probe, or one in the wrong namespace.
        self.blind_to_own_fds = blind_to_own_fds
        #: Simulates the far more dangerous `lsof`: one that answers happily
        #: about THIS process and silently omits every other. Its silence
        #: about a running backend is indistinguishable from a quiet file.
        self.hide_foreign_pids = hide_foreign_pids
        #: Processes other than this one that hold a file open. A real probe
        #: would find them by asking the kernel; this one is told.
        self.foreign_holders = []
        self.cli_deadline_behaviour = cli_deadline_behaviour
        self.calls = []
        self.overrides = {}

    # -- helpers a test can drive ------------------------------------
    def container(self, container_id):
        return self.containers[container_id]

    def ran(self, *fragments):
        return [argv for argv in self.calls if all(f in argv for f in fragments)]

    def sigkilled(self):
        return sorted(c.container_id for c in self.containers.values() if c.sigkilled)

    # -- the runner ---------------------------------------------------
    def __call__(self, argv, timeout=None):
        from app.wal_fence import CommandResult

        argv = tuple(argv)
        forbidden = sorted(set(argv) & DESTRUCTIVE)
        if forbidden:
            raise AssertionError(
                f"a destructive command reached the host: {shlex.join(argv)} "
                f"(forbidden: {', '.join(forbidden)}). The wrapper's guard is "
                "the only thing that should have been between these."
            )
        self.calls.append(argv)
        override = self.overrides.get(argv[1] if len(argv) > 1 else None)
        if override is not None:
            outcome = override(argv)
            rc, out, err = outcome[:3]
            timed_out = outcome[3] if len(outcome) > 3 else False
            return CommandResult(
                argv=argv, returncode=rc, stdout=out, stderr=err, timed_out=timed_out
            )
        if argv[0] == "lsof":
            rc, out, err, timed_out = (*self._lsof(argv), False)
        elif argv[0] == "docker":
            rc, out, err, timed_out = self._docker(argv, timeout)
        else:  # pragma: no cover - a test that gets here has a bug
            raise AssertionError(f"unexpected command: {shlex.join(argv)}")
        return CommandResult(
            argv=argv, returncode=rc, stdout=out, stderr=err, timed_out=timed_out
        )

    def _docker(self, argv, timeout):
        verb = argv[1]
        if verb == "ps":
            return (*self._ps(argv), False)
        if verb == "inspect":
            return (*self._inspect(argv), False)
        if verb == "update":
            return (*self._update(argv), False)
        if verb == "stop":
            return self._stop(argv, timeout)
        if verb == "start":
            return (*self._start(argv), False)
        raise AssertionError(f"unexpected docker verb: {verb}")

    def _ps(self, argv):
        wanted = {}
        for index, token in enumerate(argv):
            if token == "--filter":
                key, _, value = argv[index + 1].partition("=")
                assert key == "label", f"unexpected ps filter: {argv[index + 1]}"
                label, _, label_value = value.partition("=")
                wanted[label] = label_value
        matched = []
        for container in self.containers.values():
            labels = container.inspect()["Config"]["Labels"]
            if all(labels.get(k) == v for k, v in wanted.items()):
                matched.append(container.container_id)
        return 0, "".join(f"{cid}\n" for cid in sorted(matched)), ""

    def _inspect(self, argv):
        container_id = argv[-1]
        container = self.containers.get(container_id)
        if container is None:
            return 1, "", f"Error: No such object: {container_id}"
        return 0, json.dumps(container.inspect()) + "\n", ""

    def _update(self, argv):
        container_id = argv[-1]
        container = self.containers[container_id]
        policy = next(a for a in argv if a.startswith("--restart="))
        name, _, retries = policy[len("--restart=") :].partition(":")
        if not container.ignore_restart_update:
            container.restart_policy = name
            container.restart_max_retries = int(retries or 0)
        return 0, container_id + "\n", ""

    def _stop(self, argv, deadline):
        """Model what `docker stop` actually does to a workload.

        A finite timeout is a promise to SIGKILL. An infinite one is a promise
        to wait; how long the CALLER waits for the CLI is a separate decision,
        and abandoning the CLI leaves the container alone.
        """
        container_id = argv[-1]
        container = self.containers[container_id]
        signal = next(
            (a.split("=", 1)[1] for a in argv if a.startswith("--signal=")), None
        )
        raw = next(
            (a.split("=", 1)[1] for a in argv if a.startswith(("--timeout=", "--time="))),
            None,
        )
        assert raw is not None, f"docker stop with no timeout: {shlex.join(argv)}"
        wait = float(raw)

        if signal == "SIGKILL":
            container.sigkilled = True
            container.status, container.running, container.pid = "exited", False, 0
            container.exit_code = 137
            return 0, container_id + "\n", "", False

        if wait >= 0 and container.stop_grace_seconds > wait:
            # SIGTERM, then SIGKILL. The container stops either way, which is
            # exactly why "it reached exited" is not proof of a clean shutdown.
            container.sigkilled = True
            container.status, container.running, container.pid = "exited", False, 0
            container.exit_code = 137
            return 0, container_id + "\n", "", False

        if container.stop_grace_seconds == float("inf"):
            # It never finishes. With an infinite docker timeout the CLI blocks,
            # so the caller's deadline is what ends the wait — and the workload
            # keeps running, untouched.
            if self.cli_deadline_behaviour == "honour" and deadline is not None:
                return 124, "", "", True
            raise AssertionError(
                "the fake would block forever; the caller passed no deadline"
            )

        container.status, container.running, container.pid = "exited", False, 0
        container.exit_code = 0
        return 0, container_id + "\n", "", False

    def _start(self, argv):
        container_id = argv[-1]
        container = self.containers[container_id]
        container.status, container.running, container.pid = "running", True, 5150
        return 0, container_id + "\n", ""

    def _lsof(self, argv):
        if self.lsof_returncode is not None:
            return (
                self.lsof_returncode,
                self.lsof_stdout if self.lsof_stdout is not None else "",
                self.lsof_stderr,
            )
        if self.lsof_stdout is not None:
            return 0, self.lsof_stdout, self.lsof_stderr
        paths = list(argv[argv.index("--") + 1 :])
        hits = [f for f in self.open_files if f.path in paths]
        if not self.hide_foreign_pids:
            hits += [f for f in self.foreign_holders if f.path in paths]
        if not self.blind_to_own_fds:
            hits += _own_open_fds(paths)
        if not hits:
            # What the real lsof does when nothing matches: exit 1, say nothing.
            return 1, "", self.lsof_stderr
        lines = []
        for hit in hits:
            lines.append(f"p{hit.pid}")
            lines.append(f"c{hit.command}")
            lines.append(f"f{hit.fd}")
            lines.append(f"n{hit.path}")
        return 0, "\n".join(lines) + "\n", self.lsof_stderr


def _own_open_fds(paths):
    """This process's own descriptors on `paths`, read from /dev/fd.

    Real, not simulated: the fence's visibility self-test opens a descriptor
    and demands the probe report it back, and that only means something if the
    probe is looking at the operating system.
    """
    wanted = {}
    for path in paths:
        try:
            info = os.stat(path)
        except OSError:
            continue
        wanted[(info.st_dev, info.st_ino)] = path
    if not wanted:
        return []
    found = []
    try:
        entries = os.listdir("/dev/fd")
    except OSError:  # pragma: no cover - POSIX hosts only
        return []
    for entry in entries:
        try:
            info = os.fstat(int(entry))
        except (OSError, ValueError):
            continue
        path = wanted.get((info.st_dev, info.st_ino))
        if path is not None:
            found.append(OpenFile(pid=os.getpid(), fd=int(entry), path=path))
    return found


class ScriptedWitness:
    """Stands in for a foreign process holding the database open.

    The fence proves it can see OTHER processes' descriptors by having one
    open the file and demanding the probe name it. In production that witness
    is a real child process; here it is scripted, and whether the probe admits
    to seeing it is the thing each test decides. A fake that always answered
    would be proving nothing — `hide_foreign_pids` is what makes the check
    load-bearing.
    """

    def __init__(self, fake, pid=424242, fd=91):
        self.fake = fake
        self.pid = pid
        self.fd = fd
        self.opened = []

    @contextlib.contextmanager
    def __call__(self, path):
        holder = OpenFile(pid=self.pid, fd=self.fd, path=str(path), command="witness")
        self.fake.foreign_holders.append(holder)
        self.opened.append(str(path))
        try:
            yield self.pid
        finally:
            self.fake.foreign_holders.remove(holder)


def scope_digest(host_id, container_ids, mount_identity):
    payload = json.dumps(
        {
            "host_id": host_id,
            "containers": sorted(container_ids),
            "mount": mount_identity,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(payload).hexdigest()


def signed_ack(
    *,
    container_ids,
    mount_identity,
    issuer="ops-oncall",
    issued_at=1_699_999_000.0,
    expires_at=1_700_100_000.0,
    host_id=HOST_ID,
    key=ACK_KEY,
    digest=None,
):
    """An acknowledgement whose signature actually verifies."""
    from app.wal_fence import ExternalAuthorityAck, ack_signing_payload

    digest = digest or scope_digest(host_id, container_ids, mount_identity)
    unsigned = ExternalAuthorityAck(
        issuer=issuer,
        issued_at=issued_at,
        expires_at=expires_at,
        host_id=host_id,
        scope_digest=digest,
        signature="",
    )
    signature = hmac.new(
        key, ack_signing_payload(unsigned), hashlib.sha256
    ).hexdigest()
    return replace(unsigned, signature=signature)


@pytest.fixture
def data_dir(tmp_path):
    """The host side of the container's /app/data bind mount."""
    path = tmp_path / "host-data"
    path.mkdir()
    return path


@pytest.fixture
def fake_docker(data_dir):
    """A project whose backend and agent both mount the data volume."""
    return FakeDocker(
        [
            FakeContainer(
                container_id=BACKEND_ID,
                service=SERVICE,
                mounts=[bind_mount(data_dir)],
            ),
            FakeContainer(
                container_id=AGENT_ID,
                service=AGENT_SERVICE,
                restart_policy="on-failure",
                restart_max_retries=3,
                mounts=[bind_mount(data_dir)],
            ),
        ]
    )


def scope_of(fake):
    from app.wal_fence import ScopedContainer

    return tuple(
        ScopedContainer(service=c.service, container_id=c.container_id)
        for c in sorted(fake.containers.values(), key=lambda c: c.container_id)
    )


def mount_identity_of(data_dir):
    from app.wal_fence import mount_identity

    return mount_identity(str(data_dir))


def external_ack(fake=None, data_dir=None, container_ids=None, **overrides):
    ids = container_ids or (
        [c.container_id for c in fake.containers.values()]
        if fake is not None
        else [BACKEND_ID, AGENT_ID]
    )
    identity = mount_identity_of(data_dir) if data_dir is not None else None
    return signed_ack(container_ids=ids, mount_identity=identity, **overrides)


def fence_kwargs(fake, tmp_path, data_dir=None, **overrides):
    scope = overrides.pop("scope", scope_of(fake))
    if data_dir is None:
        sources = {
            m["Source"]
            for c in fake.containers.values()
            for m in c.mounts
            if m["Destination"] == DATA_DESTINATION
        }
        data_dir = sorted(sources)[0] if sources else None
    kwargs = dict(
        project=PROJECT,
        service=SERVICE,
        container_id=BACKEND_ID,
        scope=scope,
        data_destination=DATA_DESTINATION,
        db_relpath=DB_RELPATH,
        manifest_path=str(tmp_path / "manifests" / "quiesce-1.json"),
        runner=fake,
        host_id=HOST_ID,
        external_authority_ack=external_ack(
            fake=fake, data_dir=data_dir, container_ids=[e.container_id for e in scope]
        )
        if data_dir
        else None,
        authority_verifier=ACK_KEY,
        visibility_witness=ScriptedWitness(fake),
    )
    kwargs.update(overrides)
    return kwargs


__all__ = [
    "PROJECT",
    "SERVICE",
    "AGENT_SERVICE",
    "BACKEND_ID",
    "AGENT_ID",
    "SOCKET_PROXY_ID",
    "OTHER_PROJECT_ID",
    "UNLABELED_ID",
    "IMAGE_ID",
    "IMAGE_REF",
    "DATA_DESTINATION",
    "DB_RELPATH",
    "DOCKER_SOCKET",
    "DESTRUCTIVE",
    "FakeContainer",
    "FakeDocker",
    "OpenFile",
    "bind_mount",
    "socket_mount",
    "replace",
    "scope_of",
    "external_ack",
    "fence_kwargs",
    "signed_ack",
    "scope_digest",
    "mount_identity_of",
    "ScriptedWitness",
    "HOST_ID",
    "ACK_KEY",
]
