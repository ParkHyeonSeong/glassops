"""Hold the host still while a WAL recovery folds a live database's WAL back in.

`app.wal_recovery` (CP-5A) proves it can carry a crashed database's WAL-only
commits into the database file without losing one. What it cannot prove on its
own is that nothing else is writing to that file while it works — and a
checkpoint run against a database a backend still has open is how a recovery
that "succeeded" ends up losing exactly the commits it was called to save.

This module is the fence around that window. It drives the host's own tools
through a caller-supplied command runner — which is what makes it testable
against a scripted host instead of a real daemon — and it refuses to hand
CP-5A a green light until, in order:

  1. the target is pinned to one exact Compose project, one service, and one
     FULL container id that Compose itself agrees on;
  2. `/app/data` resolves to exactly one read-write mount, and the database
     path is DERIVED from that mount rather than taken on trust;
  3. every container sharing that mount is named in the caller's scope, and
     nothing outside the scope shares it;
  4. the existing restart policies are written down BEFORE any of them move;
  5. every scoped container's policy is set to `no` and read back;
  6. every scoped container is stopped gracefully and observed `exited`;
  7. the host itself — not Docker — reports zero open descriptors on the
     database and its WAL;
  8. all of that, plus the database's inode/size/SHA-256, is recorded in a
     QUIESCED manifest carrying a lease with an expiry.

The states are files, in this order, and each one is on disk before the change
it authorises is attempted:

    PREPARED   what every scoped container's restart policy was, written and
               fsynced BEFORE the first `docker update`. A crash between two
               updates leaves this behind, so a retry re-reads the original
               policy from here instead of mistaking the `no` it already set
               for the way things were.
    QUIESCED   the manifest: containers stopped, descriptors proved absent,
               database identity recorded, lease granted.
    CLAIMED    one atomic, one-shot capability. Created with O_EXCL, so two
               racing claims produce exactly one winner, and it stays held for
               as long as the recovery runs.
    COMPLETED / FAILED   the claim is spent and can never authorise again.
    RELEASED   the restart policies put back from PREPARED, by hand.

Two rules run through the whole thing.

**It never escalates.** There is no kill, no `compose down`, no `docker rm`, no
volume command, and no WAL or shared-memory file is ever removed. Crucially,
`docker stop` is only ever issued with `--timeout=-1`: a finite timeout is a
promise to SIGKILL the workload once it elapses, which is precisely the thing
this fence exists to avoid. How long WE wait is a separate, host-side deadline;
when it runs out the CLI call is abandoned and the container is reported as it
actually is. A graceful stop that does not finish is a failure to report, not a
signal to try harder.

**It never puts the host back by itself.** Any failure — at any step, including
after the containers are already down — leaves the host exactly where it is and
raises. Restoring the restart policies is `release()`, a separate call a person
has to make, and even that leaves the containers stopped unless explicitly told
otherwise. A half-understood failure is not a reason to hand a database back to
a writer.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import contextlib
import hmac
import secrets
import stat
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from app.wal_recovery import (
    APPLY_SCHEMA,
    APPLYING,
    CLAIMED,
    COMPLETED,
    FAILED,
    ApplyRecordRejected,
    SourceApplyAuthority,
    SourceApplyGrant,
    read_apply_record,
)

__all__ = [
    "WalFenceError",
    "ForbiddenCommand",
    "DockerCommandFailed",
    "TargetRejected",
    "TargetNotFound",
    "AmbiguousTarget",
    "MountNotBound",
    "ScopeMismatch",
    "RestartPolicyNotApplied",
    "StopIncomplete",
    "DescriptorsOpen",
    "ProbeFailed",
    "ManifestRejected",
    "FenceStale",
    "FenceBroken",
    "FenceDrift",
    "FenceScopeMismatch",
    "ReleaseFailed",
    "AlreadyReleased",
    "CommandResult",
    "ScopedContainer",
    "MountBinding",
    "ContainerFacts",
    "FileFacts",
    "Preflight",
    "Manifest",
    "FenceCheck",
    "ReleaseRecord",
    "run_host_command",
    "subprocess_runner",
    "preflight",
    "fence",
    "read_manifest",
    "ManifestFence",
    "release",
    "ExternalAuthorityAck",
    "ExternalAuthorityUnacknowledged",
    "StartAuthority",
    "StartAuthorityUnbound",
    "PreparedRecord",
    "PreparedRecordRejected",
    "SourceApplyCapability",
    "ClaimUnavailable",
    "ClaimSpent",
    "read_prepared",
    "prepared_path_for",
    "ApplyAlreadyStarted",
    "ClaimInFlight",
    "ExternalAuthorityUnverified",
    "LeaseExceedsAcknowledgement",
    "ack_lease_binding_for",
    "mount_identity",
    "ack_signing_payload",
]

SCHEMA = "glassops.wal-fence/1"
PREPARED = "PREPARED"
QUIESCED = "QUIESCED"
RELEASED = "RELEASED"

#: With no terminal record written yet, the live claim states each outcome may
#: still be recorded from. A completion is about a pass over the source, so one
#: has to have begun; a failure can arrive at any point before that.
_FINISHABLE_FROM = {COMPLETED: (APPLYING,), FAILED: (CLAIMED, APPLYING)}

#: A container that can start any other container on this host, and therefore
#: can undo the whole fence while a checkpoint is in flight.
DOCKER_SOCKET_PATHS = frozenset({"/var/run/docker.sock", "/run/docker.sock"})

_FULL_CONTAINER_ID = re.compile(r"^[0-9a-f]{64}$")
_COMPOSE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_CHUNK = 1 << 20
_DIR_MODE = 0o700

PROJECT_LABEL = "com.docker.compose.project"
SERVICE_LABEL = "com.docker.compose.service"

#: Tokens that cannot appear in any command this wrapper issues. Matched as
#: whole argv words, so a destructive verb is refused before the host sees it
#: no matter how the rest of the command line is arranged.
_DESTRUCTIVE_TOKENS = frozenset(
    {
        "kill",
        "rm",
        "rmi",
        "remove",
        "prune",
        "down",
        "volume",
        "--volumes",
        "-9",
        "SIGKILL",
        "--signal=SIGKILL",
    }
)

#: The only `docker stop` this wrapper will issue. `-1` asks the daemon to wait
#: indefinitely; every finite value is a scheduled SIGKILL.
_INFINITE_STOP = "--timeout=-1"

#: The complete vocabulary. An allowlist rather than a filter: a command this
#: module was never meant to issue cannot be issued by accident.
_ALLOWED_DOCKER_VERBS = frozenset({"ps", "inspect", "update", "stop", "start"})
_ALLOWED_PROGRAMS = frozenset({"docker", "lsof"})


# ── typed refusals ───────────────────────────────────


class WalFenceError(Exception):
    """Base for every refusal in this module."""

    code = "WAL_FENCE_ERROR"

    def __init__(self, message: str, *, stage: str | None = None, detail=None):
        super().__init__(message)
        self.stage = stage
        self.detail = detail


class ForbiddenCommand(WalFenceError):
    """A command outside this wrapper's vocabulary, or a destructive one."""

    code = "FORBIDDEN_COMMAND"


class DockerCommandFailed(WalFenceError):
    """The host refused or could not complete a command we are allowed to run."""

    code = "DOCKER_COMMAND_FAILED"


class TargetRejected(WalFenceError):
    """The project, service, container id, or database path is not usable."""

    code = "TARGET_REJECTED"


class TargetNotFound(WalFenceError):
    """Compose reports no container for this project and service."""

    code = "TARGET_NOT_FOUND"


class AmbiguousTarget(WalFenceError):
    """More than one container answers to this target, or the named one does not."""

    code = "AMBIGUOUS_TARGET"


class MountNotBound(WalFenceError):
    """The data destination does not resolve to exactly one usable mount."""

    code = "MOUNT_NOT_BOUND"


class ScopeMismatch(WalFenceError):
    """The containers sharing the data mount are not the ones declared."""

    code = "SCOPE_MISMATCH"


class RestartPolicyNotApplied(WalFenceError):
    """A restart policy did not read back as `no` after being set."""

    code = "RESTART_POLICY_NOT_APPLIED"


class StopIncomplete(WalFenceError):
    """A container did not reach `exited` within its graceful stop timeout."""

    code = "STOP_INCOMPLETE"


class DescriptorsOpen(WalFenceError):
    """Something still holds the database or its WAL open."""

    code = "DESCRIPTORS_OPEN"

    def __init__(self, message, *, pids=(), fds=(), stage=None, detail=None):
        super().__init__(message, stage=stage, detail=detail)
        self.pids = list(pids)
        self.fds = list(fds)


class ProbeFailed(WalFenceError):
    """The descriptor probe could not answer. Not knowing is not zero."""

    code = "PROBE_FAILED"


class ManifestRejected(WalFenceError):
    """The manifest is missing, malformed, in the wrong state, or already there."""

    code = "MANIFEST_REJECTED"


class FenceStale(WalFenceError):
    """The lease has expired; the fence can no longer speak for the host."""

    code = "FENCE_STALE"


class FenceBroken(WalFenceError):
    """A scoped container is no longer stopped, or no longer pinned to `no`."""

    code = "FENCE_BROKEN"


class FenceDrift(WalFenceError):
    """The database or WAL is not the one the manifest recorded."""

    code = "FENCE_DRIFT"


class FenceScopeMismatch(WalFenceError):
    """This manifest does not speak for the database it is being asked about."""

    code = "FENCE_SCOPE_MISMATCH"


class StartAuthorityUnbound(WalFenceError):
    """Something on this host can start the containers we are stopping.

    A container with the Docker socket mounted can start any container on the
    host, including the ones the fence just stopped. Unless it is inside the
    scope — and therefore stopped too — the fence is decorative.
    """

    code = "START_AUTHORITY_UNBOUND"


class ExternalAuthorityUnacknowledged(WalFenceError):
    """Nobody has accepted the authorities no probe can see.

    A host administrator, a systemd unit, a cron job: each can start these
    containers and none of them is visible from here. That risk does not go
    away by being unmentioned, so the fence refuses to run until someone puts
    their name to it.
    """

    code = "EXTERNAL_AUTHORITY_UNACKNOWLEDGED"


class PreparedRecordRejected(WalFenceError):
    """The record of the original restart policies is missing or untrustworthy."""

    code = "PREPARED_RECORD_REJECTED"


class ClaimUnavailable(WalFenceError):
    """This lease cannot be claimed: already claimed, spent, or released."""

    code = "CLAIM_UNAVAILABLE"


class ClaimSpent(WalFenceError):
    """This capability has already been completed, failed, or released."""

    code = "CLAIM_SPENT"


class ApplyAlreadyStarted(WalFenceError):
    """The one transition from CLAIMED to APPLYING has already been taken."""

    code = "APPLY_ALREADY_STARTED"


class ClaimInFlight(WalFenceError):
    """A claim is live, so nothing may put the host back yet.

    Restoring a restart policy or starting a container while a checkpoint is
    running is the fence undoing itself at the worst possible moment.
    """

    code = "CLAIM_IN_FLIGHT"


class LeaseExceedsAcknowledgement(WalFenceError):
    """The lease being asked for reaches past the signature behind it.

    An acknowledgement covers a window somebody chose. Handing out authority
    that outlives it is the fence extending permission nobody gave, and
    quietly shortening the lease instead would hide the mismatch rather than
    surface it.
    """

    code = "LEASE_EXCEEDS_ACK"


class ExternalAuthorityUnverified(WalFenceError):
    """The acknowledgement cannot be checked, so it is not evidence.

    An unverifiable string is a claim, not a signature, and this refuses to
    treat one as grounds for believing the host is quiet.
    """

    code = "EXTERNAL_AUTHORITY_UNVERIFIED"


class ReleaseFailed(WalFenceError):
    """A restart policy could not be restored, or did not read back."""

    code = "RELEASE_FAILED"


class AlreadyReleased(WalFenceError):
    """This manifest has already been released once."""

    code = "ALREADY_RELEASED"


# ── running commands on the host ─────────────────────


@dataclass(frozen=True)
class CommandResult:
    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str
    #: True when OUR deadline ended the wait. The command itself was abandoned;
    #: whatever it had asked the daemon to do carries on without us.
    timed_out: bool = False


CommandRunner = Callable[..., CommandResult]


def subprocess_runner(argv: Sequence[str], timeout: float | None = None) -> CommandResult:
    """The real host. Every caller in the tests supplies its own instead.

    A timeout here kills the *client* process — `docker stop` waiting on the
    daemon — and nothing else. The container keeps shutting down at its own
    pace, which is the entire point of asking for an indefinite stop.
    """
    try:
        completed = subprocess.run(  # noqa: S603 - argv is built here, never shell
            list(argv), capture_output=True, text=True, check=False, timeout=timeout
        )
    except subprocess.TimeoutExpired as expired:
        return CommandResult(
            argv=tuple(argv),
            returncode=124,
            stdout=expired.stdout.decode() if isinstance(expired.stdout, bytes) else (expired.stdout or ""),
            stderr=expired.stderr.decode() if isinstance(expired.stderr, bytes) else (expired.stderr or ""),
            timed_out=True,
        )
    return CommandResult(
        argv=tuple(argv),
        returncode=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
    )


def run_host_command(
    runner: CommandRunner, argv: Sequence[str], *, timeout: float | None = None
) -> CommandResult:
    """Check a command against this wrapper's vocabulary, then run it.

    The check happens here rather than at each call site so there is exactly
    one place a destructive command could get through, and it is a place that
    refuses before the runner is ever called.
    """
    argv = tuple(str(token) for token in argv)
    if not argv:
        raise ForbiddenCommand("refusing to run an empty command")

    destructive = sorted(set(argv) & _DESTRUCTIVE_TOKENS)
    if destructive:
        raise ForbiddenCommand(
            f"refusing a destructive command: {' '.join(argv)} "
            f"(forbidden here: {', '.join(destructive)}). This wrapper stops "
            "containers gracefully and does nothing else — killing, tearing a "
            "project down, or touching a volume is never its call.",
            detail=argv,
        )
    if argv[0] not in _ALLOWED_PROGRAMS:
        raise ForbiddenCommand(
            f"{argv[0]!r} is not one of this wrapper's programs "
            f"({', '.join(sorted(_ALLOWED_PROGRAMS))})",
            detail=argv,
        )
    if argv[0] == "docker" and (len(argv) < 2 or argv[1] not in _ALLOWED_DOCKER_VERBS):
        raise ForbiddenCommand(
            f"{' '.join(argv[:2])!r} is not one of this wrapper's docker verbs "
            f"({', '.join(sorted(_ALLOWED_DOCKER_VERBS))})",
            detail=argv,
        )
    if argv[0] == "docker" and argv[1] == "stop":
        _require_non_forcing_stop(argv)
    return runner(argv, timeout=timeout)


def _require_non_forcing_stop(argv: tuple) -> None:
    """`docker stop` may only ever be the form that cannot kill the workload.

    `docker stop -t N` sends SIGTERM and then SIGKILL N seconds later. There is
    no value of N that makes that safe for a database mid-write, so the only
    accepted form is the indefinite one; the deadline that protects the
    operator from waiting forever lives on our side of the CLI, where giving up
    means giving up on the wait rather than on the process.
    """
    signals = [a for a in argv if a.startswith(("--signal", "-s"))]
    if signals:
        raise ForbiddenCommand(
            f"refusing `docker stop` with {' '.join(signals)}: this wrapper "
            "never chooses the signal a workload is stopped with",
            detail=argv,
        )
    timeouts = [a for a in argv if a.startswith(("--timeout", "--time", "-t"))]
    if timeouts != [_INFINITE_STOP]:
        raise ForbiddenCommand(
            f"refusing `docker stop` with {timeouts or 'no timeout'}: the only "
            f"accepted form is {_INFINITE_STOP}, because every finite timeout "
            "(including the 10s default) is a promise to SIGKILL the workload "
            "once it elapses",
            detail=argv,
        )


def _docker(
    runner: CommandRunner, *args: str, stage: str, timeout: float | None = None
) -> CommandResult:
    result = run_host_command(runner, ("docker", *args), timeout=timeout)
    if result.timed_out:
        return result
    if result.returncode != 0:
        raise DockerCommandFailed(
            f"`docker {' '.join(args)}` exited {result.returncode}: "
            f"{result.stderr.strip() or result.stdout.strip()}",
            stage=stage,
            detail=result,
        )
    return result


# ── what the caller declares ─────────────────────────


@dataclass(frozen=True)
class ScopedContainer:
    """One container that must be stopped for the fence to hold."""

    service: str
    container_id: str


@dataclass(frozen=True)
class ExternalAuthorityAck:
    """Someone's signature against the authorities nothing can detect.

    systemd units, cron entries, a person with a shell: all of them can start
    a stopped container and none of them is discoverable from inside this
    process. Rather than pretend the inventory is complete, the fence requires
    a named person to have signed for that gap — and requires the signature to
    actually verify, against this host and this exact scope, inside a window
    they chose. A string nobody can check is a claim, not a signature, and it
    is not accepted as one.
    """

    issuer: str
    issued_at: float
    expires_at: float
    host_id: str
    scope_digest: str
    signature: str

    def as_json(self) -> dict:
        return {
            "issuer": self.issuer,
            "issued_at": self.issued_at,
            "expires_at": self.expires_at,
            "host_id": self.host_id,
            "scope_digest": self.scope_digest,
            "signature": self.signature,
        }


def ack_signing_payload(ack: ExternalAuthorityAck) -> bytes:
    """Exactly what the signature covers, in a form both sides can rebuild."""
    return json.dumps(
        {
            "issuer": ack.issuer,
            "issued_at": ack.issued_at,
            "expires_at": ack.expires_at,
            "host_id": ack.host_id,
            "scope_digest": ack.scope_digest,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


def scope_digest_for(host_id: str, container_ids, mount_identity_value) -> str:
    payload = json.dumps(
        {
            "host_id": host_id,
            "containers": sorted(container_ids),
            "mount": mount_identity_value,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(payload).hexdigest()


def ack_lease_binding_for(ack: ExternalAuthorityAck, lease_id: str, container_ids) -> str:
    """The pairing of an acknowledgement with the lease it ended up beside.

    The signature covers a host and a scope; the lease did not exist when it
    was signed. This is what makes moving a genuine signature onto a different
    lease detectable — and it is only worth writing down because it is
    recomputed and compared every time the lease is used.
    """
    return hashlib.sha256(
        ack_signing_payload(ack)
        + str(lease_id).encode()
        + json.dumps(sorted(container_ids)).encode()
    ).hexdigest()


def _verify_ack(
    ack, verifier, *, host_id: str, container_ids, mount_identity_value, now: float
) -> None:
    if ack is None or not isinstance(ack, ExternalAuthorityAck):
        raise ExternalAuthorityUnacknowledged(
            "a host administrator, a systemd unit or a cron job can start any "
            "of these containers, and no probe on this host can see them. Pass "
            "a signed ExternalAuthorityAck naming who accepts that."
        )
    for field_name in ("issuer", "host_id", "scope_digest", "signature"):
        value = getattr(ack, field_name)
        if not (isinstance(value, str) and value.strip()):
            raise ExternalAuthorityUnacknowledged(
                f"the acknowledgement has an empty {field_name}; an unsigned "
                "blank is not somebody accepting anything"
            )
    if verifier is None:
        raise ExternalAuthorityUnverified(
            "no verifier was supplied, so the acknowledgement cannot be "
            "checked. An unverifiable string is not a signature and will not "
            "be used as grounds for believing this host is quiet."
        )
    if not (ack.issued_at < ack.expires_at):
        raise ExternalAuthorityUnverified(
            f"the acknowledgement expires ({ack.expires_at}) before it was "
            f"issued ({ack.issued_at})"
        )
    if not (ack.issued_at <= now <= ack.expires_at):
        raise ExternalAuthorityUnverified(
            f"the acknowledgement is outside its window: issued {ack.issued_at}, "
            f"expires {ack.expires_at}, now {now}"
        )
    if ack.host_id != host_id:
        raise ExternalAuthorityUnverified(
            f"the acknowledgement is for host {ack.host_id!r}, not {host_id!r}"
        )
    expected = scope_digest_for(host_id, container_ids, mount_identity_value)
    if not hmac.compare_digest(ack.scope_digest, expected):
        raise ExternalAuthorityUnverified(
            "the acknowledgement was signed for a different scope: it covers "
            f"{ack.scope_digest}, and this fence is {expected}"
        )
    signed = hmac.new(verifier, ack_signing_payload(ack), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(ack.signature, signed):
        raise ExternalAuthorityUnverified(
            "the acknowledgement's signature does not verify against the key "
            "supplied; whoever wrote it, it was not signed with this one"
        )


def _reverify_manifest_authority(
    manifest: "Manifest", verifier, now: float, *, when: str
) -> None:
    """Re-read the stored acknowledgement and check it all over again.

    Not remembered from the quiesce: the manifest is a file, the host is a
    moving target, and every one of these — signature, host, scope, window,
    and the pairing with this lease — is something a later step will act on.
    """
    data = manifest.data
    stored = data.get("external_authority_ack") or {}
    try:
        ack = ExternalAuthorityAck(**stored)
    except TypeError as exc:
        raise ExternalAuthorityUnverified(
            f"the acknowledgement stored in {manifest.path!r} is not one this "
            f"fence can read ({when}): {exc}",
            stage="authority",
        ) from exc
    container_ids = [entry["container_id"] for entry in data.get("scope", [])]
    _verify_ack(
        ack,
        verifier,
        host_id=data.get("host_id", ""),
        container_ids=container_ids,
        mount_identity_value=mount_identity(data.get("mount", {}).get("source", "")),
        now=now,
    )
    expected = ack_lease_binding_for(
        ack, data.get("lease", {}).get("id", ""), container_ids
    )
    if not hmac.compare_digest(str(data.get("ack_lease_binding", "")), expected):
        raise ExternalAuthorityUnverified(
            f"the acknowledgement in {manifest.path!r} is not bound to this "
            f"lease ({when}): a genuine signature moved onto another lease "
            "would pass every field check and still be permission for "
            "something else",
            stage="authority",
        )


def _require_authority_at(
    manifest: "Manifest", verifier, now: float, *, when: str, stage: str
) -> None:
    """Lease and acknowledgement, both judged against ONE instant.

    Reading the clock twice inside a single decision opens a window: a lease
    that is still live at the first reading and an acknowledgement checked at
    the second can straddle an expiry that neither call ever sees, and the
    transition the pair authorises then begins after the authority behind it
    ended. One `now` for both is what closes it.
    """
    expires_at = float(manifest.data.get("lease", {}).get("expires_at", 0))
    if now > expires_at:
        raise FenceStale(
            f"the lease on {manifest.path!r} had expired "
            f"{now - expires_at:.0f}s {when}. Quiesce again rather than "
            "trusting a fence nobody has looked at since.",
            stage=stage,
        )
    _reverify_manifest_authority(manifest, verifier, now, when=when)


@dataclass(frozen=True)
class StartAuthority:
    """A container that could start the ones we are about to stop."""

    container_id: str
    service: str
    project: str
    reason: str

    def as_json(self) -> dict:
        return {
            "container_id": self.container_id,
            "service": self.service,
            "project": self.project,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class PreparedRecord:
    """What the host looked like before the first restart policy was changed."""

    path: str
    data: dict

    @property
    def scope(self) -> list:
        return self.data.get("scope", [])

    def policy_before(self, container_id: str) -> dict:
        for entry in self.scope:
            if entry["container_id"] == container_id:
                return entry["restart_policy_before"]
        raise PreparedRecordRejected(
            f"{self.path!r} has no recorded policy for {container_id}",
            stage="prepared",
        )


# ── what we observe ──────────────────────────────────


@dataclass(frozen=True)
class MountBinding:
    destination: str
    source: str
    type: str
    rw: bool

    def as_json(self) -> dict:
        return {
            "destination": self.destination,
            "source": self.source,
            "type": self.type,
            "rw": self.rw,
        }


@dataclass(frozen=True)
class ContainerFacts:
    container_id: str
    image_id: str
    image_ref: str
    project: str
    service: str
    status: str
    running: bool
    pid: int
    exit_code: int
    restart_policy_name: str
    restart_max_retries: int
    mounts: tuple

    @property
    def restart_policy(self) -> dict:
        return {
            "name": self.restart_policy_name,
            "maximum_retry_count": self.restart_max_retries,
        }

    @property
    def state(self) -> dict:
        return {
            "status": self.status,
            "running": self.running,
            "pid": self.pid,
            "exit_code": self.exit_code,
        }


@dataclass(frozen=True)
class FileFacts:
    path: str
    exists: bool
    size_bytes: int
    inode: int | None
    device: int | None
    sha256: str | None

    def as_json(self) -> dict:
        return {
            "path": self.path,
            "exists": self.exists,
            "size_bytes": self.size_bytes,
            "inode": self.inode,
            "device": self.device,
            "sha256": self.sha256,
        }

    def identity(self) -> tuple:
        return (self.exists, self.size_bytes, self.inode, self.sha256)


@dataclass(frozen=True)
class Preflight:
    """Everything the fence learned without changing anything."""

    project: str
    service: str
    container_id: str
    primary: ContainerFacts
    scope: tuple
    scope_facts: tuple
    mount: MountBinding
    db_path: str
    wal_path: str
    db: FileFacts
    wal: FileFacts
    start_authorities: tuple = ()
    external_authority_ack: ExternalAuthorityAck | None = None


@dataclass(frozen=True)
class Manifest:
    path: str
    data: dict

    @property
    def state(self) -> str:
        return self.data.get("state", "")

    @property
    def lease_id(self) -> str:
        return self.data.get("lease", {}).get("id", "")

    @property
    def db_path(self) -> str:
        return self.data.get("database", {}).get("db_path", "")


@dataclass(frozen=True)
class FenceCheck:
    """What the fence saw the moment before CP-5A opened the source."""

    lease_id: str
    checked_at: float
    ok: bool
    open_pids: list
    open_fds: list
    containers: tuple
    db: FileFacts
    wal: FileFacts


@dataclass(frozen=True)
class ReleaseRecord:
    path: str
    lease_id: str
    restored: tuple
    started_containers: tuple = ()
    #: Containers deliberately left down: they were already stopped when the
    #: fence arrived, so starting them would not be restoring anything.
    not_started: tuple = ()


# ── file identity ────────────────────────────────────


def _sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _file_facts(path: str) -> FileFacts:
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return FileFacts(path, False, 0, None, None, None)
    return FileFacts(
        path=path,
        exists=True,
        size_bytes=info.st_size,
        inode=info.st_ino,
        device=info.st_dev,
        sha256=_sha256(path) if stat.S_ISREG(info.st_mode) else None,
    )


def _wal_path(db_path: str) -> str:
    return f"{db_path}-wal"


# ── inspecting containers ────────────────────────────


def _inspect(runner: CommandRunner, container_id: str, *, stage: str) -> ContainerFacts:
    result = _docker(
        runner, "inspect", "--format", "{{json .}}", container_id, stage=stage
    )
    try:
        raw = json.loads(result.stdout.strip().splitlines()[0])
    except (ValueError, IndexError) as exc:
        raise DockerCommandFailed(
            f"could not read `docker inspect {container_id}`: {exc}", stage=stage
        ) from exc
    labels = (raw.get("Config") or {}).get("Labels") or {}
    policy = ((raw.get("HostConfig") or {}).get("RestartPolicy")) or {}
    state = raw.get("State") or {}
    return ContainerFacts(
        container_id=raw.get("Id", ""),
        image_id=raw.get("Image", ""),
        image_ref=(raw.get("Config") or {}).get("Image", ""),
        project=labels.get(PROJECT_LABEL, ""),
        service=labels.get(SERVICE_LABEL, ""),
        status=state.get("Status", ""),
        running=bool(state.get("Running", False)),
        pid=int(state.get("Pid", 0) or 0),
        exit_code=int(state.get("ExitCode", 0) or 0),
        restart_policy_name=policy.get("Name", ""),
        restart_max_retries=int(policy.get("MaximumRetryCount", 0) or 0),
        mounts=tuple(raw.get("Mounts") or ()),
    )


def _try_inspect(runner: CommandRunner, container_id: str) -> ContainerFacts | None:
    try:
        return _inspect(runner, container_id, stage="inspect")
    except DockerCommandFailed:
        return None


def _list_host_containers(runner: CommandRunner, *, stage: str) -> list:
    """Every container on this host, filtered by nothing.

    A project-scoped listing cannot see a writer that belongs to someone else,
    and "belongs to someone else" is exactly the writer that will not stop when
    this project does.
    """
    result = _docker(
        runner, "ps", "--all", "--no-trunc", "--format", "{{.ID}}", stage=stage
    )
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def _host_inventory(runner: CommandRunner, *, stage: str) -> dict:
    return {
        cid: _inspect(runner, cid, stage=stage)
        for cid in _list_host_containers(runner, stage=stage)
    }


def mount_identity(source: str):
    """What this mount source actually IS, or None if that cannot be settled.

    Two containers can reach the same bytes by different spellings — a symlink,
    a second bind, a path with a `..` in it. Comparing the strings says they
    are unrelated; comparing the storage the strings resolve to says they are
    the same directory, and the second container can write to it.
    """
    try:
        info = os.stat(source)
    except OSError:
        return None
    return {
        "realpath": os.path.realpath(source),
        "device": info.st_dev,
        "inode": info.st_ino,
    }


def _mount_sharers(inventory: dict, identity, *, stage: str) -> set:
    """Containers whose mounts land on the same storage as ours.

    A source we cannot stat is not evidence of a different directory; it is an
    unanswered question about whether something else can write to this
    database, and it is refused rather than assumed away.
    """
    sharers, unknown = set(), []
    for cid, facts in inventory.items():
        for mount in facts.mounts:
            source = str(mount.get("Source", ""))
            if not source:
                continue
            here = mount_identity(source)
            if here is None:
                unknown.append((cid, source))
                continue
            if (here["device"], here["inode"]) == (identity["device"], identity["inode"]):
                sharers.add(cid)
                break
    if unknown:
        raise ScopeMismatch(
            "the storage identity of these mounts cannot be established, so "
            "whether they reach this database is unanswered: "
            + ", ".join(f"{cid[:12]}…={source!r}" for cid, source in unknown)
            + ". An unanswered question about who else can write is refused.",
            stage=stage,
            detail=[{"container_id": cid, "source": s} for cid, s in unknown],
        )
    return sharers


def _start_authorities(inventory: dict) -> list:
    """Containers that can start any container on this host.

    Today that means the Docker socket, mounted directly or handed on by a
    socket proxy. Anything holding it can undo a stop the instant it is made.
    """
    found = []
    for cid, facts in inventory.items():
        for mount in facts.mounts:
            if os.path.realpath(str(mount.get("Source", ""))) in {
                os.path.realpath(p) for p in DOCKER_SOCKET_PATHS
            } or str(mount.get("Source", "")) in DOCKER_SOCKET_PATHS:
                found.append(
                    StartAuthority(
                        container_id=cid,
                        service=facts.service,
                        project=facts.project,
                        reason="docker-socket",
                    )
                )
                break
    return sorted(found, key=lambda a: a.container_id)


def _require_inventory_bound(
    inventory: dict, mount_source: str, declared: set, *, stage: str
) -> list:
    """Nothing that can write to, or restart, this database is left loose.

    Two populations have to be in the scope, and they are not the same one:
    containers that share the data mount (they can write) and containers
    holding the Docker socket (they can start the ones we stop). The scope has
    to be exactly their union — no more, so it cannot quietly stop unrelated
    services, and no less, so nothing is left able to interfere.
    """
    identity = mount_identity(mount_source)
    if identity is None:
        raise ScopeMismatch(
            f"the data mount {mount_source!r} cannot be stat-ed, so its storage "
            "identity is unknown and nothing can be said about who shares it",
            stage=stage,
        )
    sharing = _mount_sharers(inventory, identity, stage=stage)
    authorities = _start_authorities(inventory)
    authority_ids = {a.container_id for a in authorities}

    unbound = [a for a in authorities if a.container_id not in declared]
    if unbound:
        raise StartAuthorityUnbound(
            "these containers can start any container on this host and are not "
            "in the scope, so they can undo the fence while a checkpoint is "
            f"running: {', '.join(a.container_id for a in unbound)}. Put them "
            "in the scope so they are stopped too.",
            stage=stage,
            detail=[a.as_json() for a in unbound],
        )

    required = sharing | authority_ids
    if declared != required:
        missing = sorted(required - declared)
        extra = sorted(declared - required)
        raise ScopeMismatch(
            f"the containers that can touch {mount_source} are not the ones "
            f"declared. Able to interfere but not declared: {missing or 'none'}. "
            f"Declared but neither a writer nor a start authority: "
            f"{extra or 'none'}. This is a whole-host question: a writer in "
            "another Compose project, or with no labels at all, will not stop "
            "because this project did.",
            stage=stage,
            detail={
                "sharing": sorted(sharing),
                "start_authorities": sorted(authority_ids),
                "declared": sorted(declared),
            },
        )
    return authorities


def _list_project_containers(
    runner: CommandRunner, project: str, service: str | None, *, stage: str
) -> list:
    argv = ["ps", "--all", "--no-trunc", "--filter", f"label={PROJECT_LABEL}={project}"]
    if service is not None:
        argv += ["--filter", f"label={SERVICE_LABEL}={service}"]
    argv += ["--format", "{{.ID}}"]
    result = _docker(runner, *argv, stage=stage)
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


# ── mount binding ────────────────────────────────────


def _normalise(destination: str) -> str:
    # lstrip first: normpath keeps a leading "//" on POSIX, so "/" + "/app"
    # would come back as "//app" and never match anything.
    collapsed = os.path.normpath("/" + destination.strip().lstrip("/"))
    return collapsed.rstrip("/") or "/"


def _bind_mount(facts: ContainerFacts, destination: str) -> MountBinding:
    wanted = _normalise(destination)
    exact, overlapping = [], []
    for mount in facts.mounts:
        here = _normalise(str(mount.get("Destination", "")))
        if here == wanted:
            exact.append(mount)
        elif here == "/" or wanted.startswith(here + "/") or here.startswith(wanted + "/"):
            overlapping.append(here)

    if len(exact) != 1:
        raise MountNotBound(
            f"{wanted} must resolve to exactly one mount on container "
            f"{facts.container_id}, found {len(exact)}. Which host bytes back "
            "the database has to have a single answer before anything is "
            "checkpointed.",
            stage="mount",
            detail=tuple(exact),
        )
    if overlapping:
        raise MountNotBound(
            f"{wanted} is bound, but {', '.join(sorted(set(overlapping)))} "
            "also overlap it — the database path would have two possible "
            "sources of truth.",
            stage="mount",
            detail=tuple(sorted(set(overlapping))),
        )

    mount = exact[0]
    if not bool(mount.get("RW", False)):
        raise MountNotBound(
            f"{wanted} is mounted read-only; a checkpoint has to write, so a "
            "read-write mount is required.",
            stage="mount",
        )
    source = str(mount.get("Source", ""))
    if not os.path.isabs(source) or not os.path.isdir(source):
        raise MountNotBound(
            f"the host side of {wanted} is not an existing directory: {source!r}",
            stage="mount",
        )
    return MountBinding(
        destination=wanted,
        source=source,
        type=str(mount.get("Type", "")),
        rw=True,
    )


def _derive_db_path(mount: MountBinding, db_relpath: str) -> str:
    if not db_relpath or os.path.isabs(db_relpath):
        raise TargetRejected(
            f"the database must be named relative to {mount.destination}, "
            f"got {db_relpath!r}",
            stage="mount",
        )
    candidate = os.path.normpath(os.path.join(mount.source, db_relpath))
    if candidate != os.path.join(mount.source, db_relpath) or not candidate.startswith(
        mount.source + os.sep
    ):
        raise TargetRejected(
            f"{db_relpath!r} escapes the data mount at {mount.source!r}",
            stage="mount",
        )
    return candidate


# ── preflight: look, change nothing ──────────────────


def preflight(
    *,
    project: str,
    service: str,
    container_id: str,
    scope: Sequence[ScopedContainer],
    data_destination: str,
    db_relpath: str,
    runner: CommandRunner,
    external_authority_ack: ExternalAuthorityAck | None,
    authority_verifier: bytes | None = None,
    host_id: str = "",
    now: float | None = None,
) -> Preflight:
    """Resolve and bind the target without touching the host's state.

    Nothing here changes a restart policy, stops a container, or writes a file.
    Everything it can refuse, it refuses before the fence has done anything
    that would need undoing.
    """
    if not _COMPOSE_NAME.match(project or ""):
        raise TargetRejected(f"compose project must be an exact name, got {project!r}")
    if not _COMPOSE_NAME.match(service or ""):
        raise TargetRejected(f"compose service must be an exact name, got {service!r}")
    if not _FULL_CONTAINER_ID.match(container_id or ""):
        raise TargetRejected(
            f"a full 64-character container id is required, got {container_id!r}. "
            "A short id is a prefix, and a prefix can start matching a second "
            "container at any time."
        )
    if not scope:
        raise ScopeMismatch("at least one container must be declared in scope")
    for entry in scope:
        if not _FULL_CONTAINER_ID.match(entry.container_id or ""):
            raise TargetRejected(
                f"scope entry {entry.service!r} needs a full container id, "
                f"got {entry.container_id!r}"
            )

    reported = _list_project_containers(runner, project, service, stage="resolve")
    if not reported:
        raise TargetNotFound(
            f"compose reports no container for project {project!r} service "
            f"{service!r}",
            stage="resolve",
        )
    if len(reported) > 1:
        raise AmbiguousTarget(
            f"project {project!r} service {service!r} matches {len(reported)} "
            f"containers ({', '.join(sorted(reported))}); the fence needs one.",
            stage="resolve",
            detail=tuple(sorted(reported)),
        )
    if reported[0] != container_id:
        raise AmbiguousTarget(
            f"compose reports {reported[0]} for {project}/{service}, but the "
            f"target names {container_id}",
            stage="resolve",
        )

    primary = _inspect(runner, container_id, stage="resolve")
    if (primary.project, primary.service) != (project, service):
        raise AmbiguousTarget(
            f"container {container_id} carries labels "
            f"{primary.project!r}/{primary.service!r}, not {project!r}/{service!r}",
            stage="resolve",
        )
    if primary.container_id != container_id:
        raise AmbiguousTarget(
            f"docker inspect answered for {primary.container_id}, not {container_id}",
            stage="resolve",
        )

    mount = _bind_mount(primary, data_destination)
    db_path = _derive_db_path(mount, db_relpath)
    db = _file_facts(db_path)
    if not db.exists or db.sha256 is None:
        raise MountNotBound(
            f"no database at {db_path!r} on the bound mount — there is nothing "
            "here to fence.",
            stage="mount",
        )
    if os.path.islink(db_path) or os.path.islink(_wal_path(db_path)):
        raise TargetRejected(
            f"the database or WAL at {db_path!r} is a symlink", stage="mount"
        )

    # Every container on the HOST, so a writer nobody declared cannot hide in
    # another project or behind missing labels.
    facts_by_id = _host_inventory(runner, stage="scope")
    declared = {entry.container_id for entry in scope}
    if container_id not in declared:
        raise ScopeMismatch(
            f"the target container {container_id} is not in the declared scope",
            stage="scope",
        )
    authorities = _require_inventory_bound(
        facts_by_id, mount.source, declared, stage="scope"
    )
    for entry in scope:
        facts = facts_by_id.get(entry.container_id)
        if facts is None:
            raise ScopeMismatch(
                f"scoped container {entry.container_id} is not in project "
                f"{project!r}",
                stage="scope",
            )
        if facts.service != entry.service:
            raise ScopeMismatch(
                f"scoped container {entry.container_id} is service "
                f"{facts.service!r}, not {entry.service!r}",
                stage="scope",
            )

    _verify_ack(
        external_authority_ack,
        authority_verifier,
        host_id=host_id,
        container_ids=[entry.container_id for entry in scope],
        mount_identity_value=mount_identity(mount.source),
        now=float(now if now is not None else time.time()),
    )

    ordered = tuple(sorted(scope, key=lambda e: e.container_id))
    return Preflight(
        project=project,
        service=service,
        container_id=container_id,
        primary=primary,
        scope=ordered,
        scope_facts=tuple(facts_by_id[e.container_id] for e in ordered),
        mount=mount,
        db_path=db_path,
        wal_path=_wal_path(db_path),
        db=db,
        wal=_file_facts(_wal_path(db_path)),
        start_authorities=tuple(authorities),
        external_authority_ack=external_authority_ack,
    )


# ── the descriptor probe ─────────────────────────────


_PROBE_TAGS = frozenset({"p", "c", "f", "n"})


def _run_probe(runner: CommandRunner, paths: Sequence[str], *, stage: str) -> dict:
    """Ask the host, not Docker, who holds these files open — and be strict.

    `lsof` has more answers than "found" and "not found", and only one of them
    is a silence worth trusting:

      * exit 1 with nothing on either stream is the real "no matches";
      * exit 1 WITH a diagnostic means it looked and could not tell;
      * exit 0 with no output is nonsense — 0 means it found something;
      * output that does not parse means we are reading a format we do not
        understand, which is not the same as reading an empty one.

    Everything except the first is `PROBE_FAILED`. Not knowing is not zero.
    """
    argv = ["lsof", "-F", "pcfn", "--", *paths]
    result = run_host_command(runner, argv)
    stdout, stderr = result.stdout.strip(), result.stderr.strip()

    if result.returncode not in (0, 1):
        raise ProbeFailed(
            f"the descriptor probe exited {result.returncode} and could not say "
            f"whether {', '.join(paths)} are open: {stderr or stdout}",
            stage=stage,
            detail=result,
        )
    if result.returncode == 1:
        if stderr:
            raise ProbeFailed(
                "the descriptor probe exited 1 but wrote to stderr, so its "
                f"silence is a diagnostic and not an answer: {stderr}",
                stage=stage,
                detail=result,
            )
        if stdout:
            raise ProbeFailed(
                "the descriptor probe exited 1 (no matches) and printed "
                f"matches anyway: {stdout}",
                stage=stage,
                detail=result,
            )
        return {
            "command": list(argv),
            "paths": list(paths),
            "returncode": 1,
            "entries": [],
            "open_pids": [],
            "open_fds": [],
        }

    if not stdout:
        raise ProbeFailed(
            "the descriptor probe exited 0, which means it found something, "
            "and then printed nothing",
            stage=stage,
            detail=result,
        )

    entries, pid, command, fd = [], None, None, None
    for line in stdout.splitlines():
        tag, value = line[0], line[1:]
        if tag not in _PROBE_TAGS:
            raise ProbeFailed(
                f"the descriptor probe printed a line this wrapper cannot "
                f"read: {line!r}",
                stage=stage,
                detail=result,
            )
        if tag == "p":
            try:
                pid = int(value)
            except ValueError:
                raise ProbeFailed(
                    f"the descriptor probe printed a malformed pid: {line!r}",
                    stage=stage,
                    detail=result,
                ) from None
            command, fd = None, None
        elif tag == "c":
            command = value
        elif tag == "f":
            fd = value
        elif tag == "n":
            if pid is None:
                raise ProbeFailed(
                    f"the descriptor probe named a file with no process: {line!r}",
                    stage=stage,
                    detail=result,
                )
            entries.append(
                {"pid": pid, "command": command, "fd": fd, "path": value}
            )
    if not entries:
        raise ProbeFailed(
            "the descriptor probe exited 0 but named no open file",
            stage=stage,
            detail=result,
        )
    return {
        "command": list(argv),
        "paths": list(paths),
        "returncode": 0,
        "entries": entries,
        "open_pids": sorted({e["pid"] for e in entries}),
        "open_fds": [e["fd"] for e in entries],
    }


_WITNESS_SOURCE = (
    "import sys, time\n"
    "handle = open(sys.argv[1], 'rb')\n"
    "sys.stdout.write('ready\\n'); sys.stdout.flush()\n"
    "time.sleep(float(sys.argv[2]))\n"
)


@contextlib.contextmanager
def spawn_visibility_witness(path: str, *, seconds: float = 60.0):
    """A second process holding `path` open, so the probe has to find one.

    The self-test on our own descriptors proves the probe can see THIS
    process. That is not the question. An unprivileged `lsof`, or one in
    another PID namespace, answers about itself perfectly and silently omits
    every other process on the host — and its silence about a running backend
    is then indistinguishable from a quiet file.
    """
    child = subprocess.Popen(  # noqa: S603 - argv is built here, never shell
        [sys.executable, "-c", _WITNESS_SOURCE, str(path), str(seconds)],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        line = child.stdout.readline()
        if line.strip() != "ready":
            raise ProbeFailed(
                "could not start a witness process to test whether the "
                f"descriptor probe can see other processes: {line!r}",
                stage="probe",
            )
        yield child.pid
    finally:
        child.terminate()
        try:
            child.wait(timeout=10)
        finally:
            if child.stdout is not None:
                child.stdout.close()


def _verify_probe_visibility(
    runner: CommandRunner, paths: Sequence[str], *, stage: str, witness=None
) -> dict:
    """Prove the probe can see a descriptor before believing it sees none.

    An unprivileged `lsof`, or one looking at the wrong namespace, answers a
    quiet file and a busy one identically. So we open the database ourselves
    and require the probe to hand it back. If it cannot see a descriptor this
    very process is holding, its silence about everyone else is worthless.
    """
    handles = []
    try:
        for path in paths:
            try:
                handles.append((path, os.open(path, os.O_RDONLY)))
            except OSError as exc:
                raise ProbeFailed(
                    f"cannot open {path!r} to test whether the probe can see "
                    f"it: {exc}. Without read access here there is no way to "
                    "show the probe is looking at the right thing.",
                    stage=stage,
                ) from exc
        if not handles:
            raise ProbeFailed(
                "there is nothing to probe, so nothing can be proved",
                stage=stage,
            )
        watched = [path for path, _ in handles]
        witness = witness or spawn_visibility_witness
        try:
            witness_context = witness(watched[0])
            entered = witness_context.__enter__()
        except OSError as exc:
            raise ProbeFailed(
                f"no witness process could be started, so cross-process "
                f"visibility cannot be demonstrated: {exc}",
                stage=stage,
            ) from exc
        with contextlib.ExitStack() as closing:
            closing.callback(witness_context.__exit__, None, None, None)
            witness_pid = entered
            probe = _run_probe(runner, watched, stage=stage)
            mine = os.getpid()
            seen = {(entry["pid"], entry["path"]) for entry in probe["entries"]}
            unseen = [path for path in watched if (mine, path) not in seen]
            if unseen:
                raise ProbeFailed(
                    "the descriptor probe cannot see descriptors this process "
                    f"is holding on {', '.join(unseen)}, so it has no "
                    "visibility to report anyone else's. Its silence proves "
                    "nothing.",
                    stage=stage,
                    detail=probe,
                )
            foreign = {
                entry["pid"] for entry in probe["entries"] if entry["pid"] != mine
            }
            if witness_pid not in foreign:
                raise ProbeFailed(
                    f"the descriptor probe cannot see process {witness_pid}, "
                    f"which is holding {watched[0]} open right now. It can see "
                    "this process and not another process, so its silence "
                    "about the rest of the host says nothing about whether "
                    "anything is still writing.",
                    stage=stage,
                    detail={"witness_pid": witness_pid, "observed": sorted(foreign)},
                )
            return {
                "observed_own_fd": True,
                "observed_foreign_fd": True,
                "pid": mine,
                "witness_pid": witness_pid,
                "paths": watched,
                "returncode": probe["returncode"],
            }
    finally:
        for _, handle in handles:
            os.close(handle)


def _require_no_descriptors(
    runner: CommandRunner, paths: Sequence[str], *, stage: str, witness=None
):
    visibility = _verify_probe_visibility(runner, paths, stage=stage, witness=witness)
    probe = _run_probe(runner, paths, stage=stage)
    if probe["entries"]:
        raise DescriptorsOpen(
            f"{len(probe['entries'])} descriptor(s) are still open on "
            f"{', '.join(paths)}: {probe['entries']}. The fence does not hold "
            "while anything can still write.",
            pids=probe["open_pids"],
            fds=probe["open_fds"],
            stage=stage,
            detail=probe,
        )
    probe["visibility"] = visibility
    probe["visibility_verified"] = True
    return probe


# ── records that survive a crash ─────────────────────


def prepared_path_for(manifest_path: str | os.PathLike[str]) -> str:
    return f"{os.fspath(manifest_path)}.prepared.json"


def _digest(record: dict) -> str:
    payload = json.dumps(record, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(payload).hexdigest()


def _seal(data: dict) -> dict:
    body = {key: value for key, value in data.items() if key != "content_sha256"}
    return {**body, "content_sha256": _digest(body)}


def _write_record(path: str, data: dict, *, overwrite: bool = False) -> dict:
    """Write a record so that a crash cannot leave a half-record behind.

    Two things make it crash-safe, and both are needed. The bytes land in a
    scratch file that is fsynced and then renamed, so the final name only ever
    appears complete — and the record carries a digest of itself, so a file
    that somehow arrives torn, or is edited afterwards, is refused rather than
    read as a smaller truth.
    """
    parent = os.path.dirname(path) or "."
    os.makedirs(parent, mode=_DIR_MODE, exist_ok=True)
    if not overwrite and os.path.lexists(path):
        raise ManifestRejected(f"refusing to write over {path!r}", stage="record")
    sealed = _seal(data)
    # Unique per writer: a shared scratch name is a second way for two
    # concurrent operations to corrupt each other's record.
    scratch = f"{path}.{os.getpid()}.{secrets.token_hex(8)}.partial"
    with open(scratch, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(sealed, indent=2, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(scratch, path)
    directory = os.open(parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
    return sealed


def _read_record(path: str, *, state: str, error):
    target = os.fspath(path)
    try:
        with open(target, encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError:
        raise error(f"no record at {target!r}", stage="record") from None
    except ValueError as exc:
        raise error(
            f"the record at {target!r} is not readable JSON — a torn or "
            f"truncated write is not a smaller record: {exc}",
            stage="record",
        ) from exc
    if data.get("schema") != SCHEMA:
        raise error(
            f"{target!r} is schema {data.get('schema')!r}, expected {SCHEMA!r}",
            stage="record",
        )
    if data.get("state") != state:
        raise error(
            f"{target!r} is in state {data.get('state')!r}, expected {state!r}",
            stage="record",
        )
    body = {key: value for key, value in data.items() if key != "content_sha256"}
    if data.get("content_sha256") != _digest(body):
        raise error(
            f"{target!r} does not match its own digest; it was truncated or "
            "edited after it was written, and nothing here will act on it",
            stage="record",
        )
    return target, data


def read_prepared(path: str | os.PathLike[str]) -> PreparedRecord:
    target, data = _read_record(path, state=PREPARED, error=PreparedRecordRejected)
    return PreparedRecord(path=target, data=data)


def read_manifest(path: str | os.PathLike[str]) -> Manifest:
    target, data = _read_record(path, state=QUIESCED, error=ManifestRejected)
    return Manifest(path=target, data=data)


# ── the fence ────────────────────────────────────────


def _format_policy(name: str, max_retries: int) -> str:
    if not name:
        return "no"
    if name == "on-failure" and max_retries > 0:
        return f"on-failure:{max_retries}"
    return name


def _prepared_body(pre: Preflight, clock) -> dict:
    return {
        "schema": SCHEMA,
        "state": PREPARED,
        "prepared_at": float(clock()),
        "target": {
            "project": pre.project,
            "service": pre.service,
            "container_id": pre.container_id,
            "image_id": pre.primary.image_id,
            "image_ref": pre.primary.image_ref,
        },
        "mount": pre.mount.as_json(),
        "database": {"db_path": pre.db_path, "wal_path": pre.wal_path},
        "scope": [
            {
                "container_id": facts.container_id,
                "service": facts.service,
                "image_id": facts.image_id,
                "restart_policy_before": facts.restart_policy,
                "state_before": facts.state,
            }
            for facts in pre.scope_facts
        ],
    }


def _require_prepared_matches(record: PreparedRecord, pre: Preflight) -> None:
    """A reusable record has to be about the same host, not just the same path."""
    target = record.data.get("target", {})
    expected = {
        "project": pre.project,
        "service": pre.service,
        "container_id": pre.container_id,
        "image_id": pre.primary.image_id,
    }
    for key, value in expected.items():
        if target.get(key) != value:
            raise PreparedRecordRejected(
                f"{record.path!r} was written for {key}={target.get(key)!r}, "
                f"not {value!r}; it cannot speak for this host",
                stage="prepared",
            )
    if record.data.get("mount", {}).get("source") != pre.mount.source:
        raise PreparedRecordRejected(
            f"{record.path!r} was written for mount "
            f"{record.data.get('mount', {}).get('source')!r}, not "
            f"{pre.mount.source!r}",
            stage="prepared",
        )
    recorded = {entry["container_id"] for entry in record.scope}
    declared = {entry.container_id for entry in pre.scope}
    if recorded != declared:
        raise PreparedRecordRejected(
            f"{record.path!r} records {sorted(recorded)} but the scope is "
            f"{sorted(declared)}; restoring from it would leave a policy behind",
            stage="prepared",
        )


def fence(
    *,
    project: str,
    service: str,
    container_id: str,
    scope: Sequence[ScopedContainer],
    data_destination: str,
    db_relpath: str,
    manifest_path: str | os.PathLike[str],
    runner: CommandRunner,
    external_authority_ack: ExternalAuthorityAck | None = None,
    authority_verifier: bytes | None = None,
    host_id: str = "",
    visibility_witness=None,
    prepared_path: str | os.PathLike[str] | None = None,
    stop_deadline_seconds: float = 120.0,
    lease_ttl_seconds: int = 900,
    clock: Callable[[], float] = time.time,
) -> Manifest:
    """Quiesce the host and write the manifest CP-5A will demand.

    On success the scoped containers are stopped, pinned to `restart=no`, and
    the manifest at `manifest_path` records the whole state with a lease.

    On ANY failure this raises and leaves the host as it is. It does not
    restart what it stopped and it does not restore a restart policy it
    changed — but it has already written the PREPARED record that says what
    those policies were, so a later `release()` can put them back exactly.
    That record is also what a retry reads: the second attempt must not
    mistake the `no` the first attempt set for the way the host used to be.
    """
    manifest_path = os.fspath(manifest_path)
    # One quiesce at a time for this manifest. Taken before the target is even
    # resolved, so two processes racing on the same name cannot both go on to
    # stop containers and then discover the clash at the write.
    with _operation_lock(manifest_path):
        return _fence_locked(
            project=project,
            service=service,
            container_id=container_id,
            scope=scope,
            data_destination=data_destination,
            db_relpath=db_relpath,
            manifest_path=manifest_path,
            runner=runner,
            external_authority_ack=external_authority_ack,
            authority_verifier=authority_verifier,
            host_id=host_id,
            visibility_witness=visibility_witness,
            prepared_path=prepared_path,
            stop_deadline_seconds=stop_deadline_seconds,
            lease_ttl_seconds=lease_ttl_seconds,
            clock=clock,
        )


@contextlib.contextmanager
def _operation_lock(manifest_path: str):
    """Exclusive for the whole operation, not just for the final write."""
    path = f"{manifest_path}.lock"
    parent = os.path.dirname(path) or "."
    os.makedirs(parent, mode=_DIR_MODE, exist_ok=True)
    try:
        handle = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        raise ManifestRejected(
            f"another quiesce is already running for {manifest_path!r} "
            f"(lock at {path!r}). If nothing is running, remove the lock by "
            "hand after checking why it is there.",
            stage="lock",
        ) from None
    try:
        os.write(handle, f"{os.getpid()}\n".encode())
        os.fsync(handle)
        yield
    finally:
        os.close(handle)
        # Renamed rather than removed, on every way out including a raised
        # exception: a failed quiesce must not wedge every retry behind a lock
        # nobody holds. A process that dies outright still leaves the lock,
        # which is the case that genuinely wants a human to look.
        with contextlib.suppress(OSError):
            os.replace(path, f"{path}.released")


def _fence_locked(
    *,
    project,
    service,
    container_id,
    scope,
    data_destination,
    db_relpath,
    manifest_path,
    runner,
    external_authority_ack,
    authority_verifier,
    host_id,
    visibility_witness,
    prepared_path,
    stop_deadline_seconds,
    lease_ttl_seconds,
    clock,
) -> Manifest:
    pre = preflight(
        project=project,
        service=service,
        container_id=container_id,
        scope=scope,
        data_destination=data_destination,
        db_relpath=db_relpath,
        runner=runner,
        external_authority_ack=external_authority_ack,
        authority_verifier=authority_verifier,
        host_id=host_id,
        now=float(clock()),
    )

    # Settled before the first mutation and before the PREPARED record: a lease
    # that reaches past its acknowledgement is refused outright rather than
    # trimmed to fit, so the mismatch is visible instead of absorbed.
    granted_at = float(clock())
    lease_expires_at = granted_at + int(lease_ttl_seconds)
    if lease_expires_at > pre.external_authority_ack.expires_at:
        raise LeaseExceedsAcknowledgement(
            f"a {int(lease_ttl_seconds)}s lease would run to {lease_expires_at}, "
            f"past the acknowledgement that lapses at "
            f"{pre.external_authority_ack.expires_at}. Ask for a shorter lease "
            "or a longer acknowledgement; this will not quietly shorten one to "
            "fit the other.",
            stage="lease",
        )

    prepared = os.fspath(prepared_path or prepared_path_for(manifest_path))
    # Checked before the first mutation, so a name clash costs nothing: a run
    # that discovers it after stopping the service has stopped it for nothing.
    if os.path.lexists(manifest_path):
        raise ManifestRejected(
            f"a manifest already exists at {manifest_path!r}; pick a new path "
            "rather than writing over the record of an earlier quiesce.",
            stage="manifest",
        )

    # PREPARED, on disk and fsynced, BEFORE the first `docker update`. If this
    # process dies between two updates, this file is the only thing that knows
    # the agent used to be `on-failure:3`.
    if os.path.lexists(prepared):
        record = read_prepared(prepared)
        _require_prepared_matches(record, pre)
    else:
        _write_record(prepared, _prepared_body(pre, clock))
        record = read_prepared(prepared)

    entries = [dict(entry) for entry in record.scope]

    # Pin every scoped container, believing the readback rather than the exit
    # status. The wanted-from value comes from the PREPARED record, never from
    # what the host says right now.
    for entry in entries:
        cid = entry["container_id"]
        _docker(runner, "update", "--restart=no", cid, stage="restart-policy")
        after = _inspect(runner, cid, stage="restart-policy")
        if after.restart_policy_name != "no":
            raise RestartPolicyNotApplied(
                f"container {cid} still has restart policy "
                f"{after.restart_policy_name!r} after being set to `no`; the "
                "daemon could bring it back mid-recovery.",
                stage="restart-policy",
                detail=after.restart_policy,
            )
        entry["restart_policy_after"] = after.restart_policy

    # Graceful stop with no deadline of Docker's own, and our deadline on the
    # wait. Abandoning the wait abandons the CLI, never the workload.
    for entry in entries:
        cid = entry["container_id"]
        result = _docker(
            runner,
            "stop",
            _INFINITE_STOP,
            cid,
            stage="stop",
            timeout=stop_deadline_seconds,
        )
        after = _inspect(runner, cid, stage="stop")
        if result.timed_out:
            raise StopIncomplete(
                f"container {cid} had not stopped after "
                f"{stop_deadline_seconds}s, so the wait was abandoned; it is "
                f"still {after.status!r} (running={after.running}) and it has "
                "NOT been killed. Find out what it is doing and stop it by "
                "hand — a database mid-write is not something to SIGKILL.",
                stage="stop",
                detail=after.state,
            )
        if after.running or after.status != "exited":
            raise StopIncomplete(
                f"container {cid} is {after.status!r} (running={after.running}) "
                "after a graceful stop. Nothing here will kill it.",
                stage="stop",
                detail=after.state,
            )
        entry["state_after_stop"] = after.state

    probe = _require_no_descriptors(
        runner,
        (pre.db_path, pre.wal_path),
        stage="probe",
        witness=visibility_witness,
    )

    db, wal = _file_facts(pre.db_path), _file_facts(pre.wal_path)
    data = {
        "schema": SCHEMA,
        "state": QUIESCED,
        "prepared_path": prepared,
        "lease": {
            "id": secrets.token_hex(16),
            "granted_at": granted_at,
            "ttl_seconds": int(lease_ttl_seconds),
            "expires_at": lease_expires_at,
        },
        "target": {
            "project": project,
            "service": service,
            "container_id": container_id,
            "image_id": pre.primary.image_id,
            "image_ref": pre.primary.image_ref,
        },
        "mount": pre.mount.as_json(),
        "scope": entries,
        "start_authorities": [a.as_json() for a in pre.start_authorities],
        "external_authority_ack": pre.external_authority_ack.as_json(),
        "host_id": host_id,
        "database": {
            "db_path": pre.db_path,
            "wal_path": pre.wal_path,
            "db": db.as_json(),
            "wal": wal.as_json(),
        },
        "fd_probe": {
            "command": probe["command"],
            "paths": probe["paths"],
            "returncode": probe["returncode"],
            "open_pids": probe["open_pids"],
            "open_fds": probe["open_fds"],
            "visibility_verified": probe["visibility_verified"],
            "visibility": probe["visibility"],
        },
        "stop_deadline_seconds": float(stop_deadline_seconds),
    }
    # The acknowledgement was signed for a host and a scope, not for a lease
    # that did not exist yet. Recording the pairing makes a later re-pairing of
    # a genuine signature with a different lease detectable after the fact.
    data["ack_lease_binding"] = ack_lease_binding_for(
        pre.external_authority_ack,
        data["lease"]["id"],
        [entry["container_id"] for entry in entries],
    )
    sealed = _write_record(manifest_path, data)
    return Manifest(path=manifest_path, data=sealed)


# ── the claim: atomic, one-shot, and held ────────────


def _claim_path(manifest_path: str) -> str:
    return f"{manifest_path}.claim.json"


def _claim_done_path(manifest_path: str) -> str:
    return f"{manifest_path}.claim.done.json"


def _applying_path(manifest_path: str) -> str:
    return f"{manifest_path}.applying.json"


def _release_path(manifest_path: str) -> str:
    return f"{manifest_path}.release.json"


def _require_claim_binds(path: str, manifest: Manifest, claim_id: str) -> dict:
    """Existence is not enough: the record has to be about this exact claim.

    Checked every time rather than once, because the file is on disk where
    anything can edit it, and every one of these fields is what a later step
    will trust.
    """
    record = read_apply_record(path)
    checks = (
        ("lease_id", manifest.lease_id),
        ("manifest_path", manifest.path),
        ("db_path", manifest.data["database"]["db_path"]),
    )
    if claim_id is not None:
        checks = (("claim_id", claim_id), *checks)
    for field_name, expected in checks:
        if record.get(field_name) != expected:
            raise ClaimUnavailable(
                f"the claim record at {path!r} says {field_name}="
                f"{record.get(field_name)!r}, and this fence is {expected!r}",
                stage="claim",
            )
    return record


class SourceApplyCapability(SourceApplyAuthority):
    """The one thing that may authorise a source checkpoint, once.

    It exists only because `ManifestFence.claim()` won an exclusive create on
    the claim file, and it stops authorising the moment it is completed,
    failed, or the fence is released. `app.wal_recovery` demands an instance of
    the abstract type this implements, so an object that merely has the right
    method name cannot stand in for it.
    """

    def __init__(
        self,
        manifest: Manifest,
        claim_id: str,
        *,
        runner,
        clock,
        authority_verifier=None,
        visibility_witness=None,
    ):
        self.manifest = manifest
        self.claim_id = claim_id
        self.runner = runner
        self.clock = clock
        self.authority_verifier = authority_verifier
        self.visibility_witness = visibility_witness

    @property
    def lease_id(self) -> str:
        return self.manifest.lease_id

    @property
    def claim_record_path(self) -> str:
        return _claim_path(self.manifest.path)

    @property
    def is_active(self) -> bool:
        base = self.manifest.path
        return not (
            os.path.lexists(_claim_done_path(base))
            or os.path.lexists(_release_path(base))
        )

    def begin_apply(self) -> str:
        """CLAIMED -> APPLYING, atomically, once. Everything after is protected.

        The exclusive create is the whole mechanism: a second pass over the
        same capability, a concurrent one, and a release trying to cut in all
        meet a file that is already there. Only once that has succeeded is the
        claim record rewritten to say APPLYING, which is what `release` and any
        other reader consult.
        """
        self._require_active()
        # Asked one last time, and BEFORE the marker exists: an apply that was
        # never authorised must leave nothing behind saying it was.
        started_at = self._require_authority(
            when="at the moment the apply would begin"
        )
        base = self.manifest.path
        apply_id = secrets.token_hex(16)
        try:
            handle = os.open(
                _applying_path(base), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600
            )
        except FileExistsError:
            raise ApplyAlreadyStarted(
                f"an apply has already begun against claim {self.claim_id}; a "
                "claim authorises one pass over the source and no more",
                stage="apply",
            ) from None
        try:
            os.write(handle, (apply_id + "\n").encode())
            os.fsync(handle)
        finally:
            os.close(handle)
        record = read_apply_record(self.claim_record_path, expect_states={CLAIMED})
        _write_record(
            self.claim_record_path,
            {**record, "state": APPLYING, "apply_id": apply_id,
             "apply_started_at": started_at},
            overwrite=True,
        )
        return apply_id

    def _require_active(self) -> None:
        base = self.manifest.path
        _require_claim_binds(self.claim_record_path, self.manifest, self.claim_id)
        if os.path.lexists(_claim_done_path(base)):
            raise ClaimSpent(
                f"claim {self.claim_id} has already finished; a spent claim "
                "never authorises anything again",
                stage="claim",
            )
        if os.path.lexists(_release_path(base)):
            raise ClaimSpent(
                f"the fence behind claim {self.claim_id} has been released; "
                "the containers may be running again",
                stage="claim",
            )

    def check_before_source_open(self, *, db_path: str) -> SourceApplyGrant:
        """Re-prove the whole fence inside the claim, or refuse.

        Everything is measured again here — the host inventory, the container
        states, the descriptors, the file identity — because every one of them
        can have changed since the quiesce. The lease is checked at both ends,
        so a check that takes long enough to outlive its own authorisation is
        refused rather than rounded down.
        """
        self._require_active()
        data = self.manifest.data
        self._require_authority(when="before the checks")

        recorded_db = data["database"]["db_path"]
        recorded_wal = data["database"]["wal_path"]
        if os.path.abspath(str(db_path)) != recorded_db:
            raise FenceScopeMismatch(
                f"this manifest was taken for {recorded_db!r}, but the "
                f"recovery is about to open {db_path!r}",
                stage="fence-check",
            )

        declared = {entry["container_id"] for entry in data["scope"]}
        inventory = _host_inventory(self.runner, stage="fence-check")
        _require_inventory_bound(
            inventory, data["mount"]["source"], declared, stage="fence-check"
        )

        containers = []
        for entry in data["scope"]:
            cid = entry["container_id"]
            facts = inventory.get(cid)
            if facts is None:
                raise FenceBroken(
                    f"scoped container {cid} is no longer on this host; the "
                    "fence cannot be shown to hold.",
                    stage="fence-check",
                )
            if facts.image_id != entry.get("image_id"):
                raise FenceBroken(
                    f"scoped container {cid} is running image "
                    f"{facts.image_id!r}, not the {entry.get('image_id')!r} the "
                    "manifest recorded — this is not the container we stopped.",
                    stage="fence-check",
                )
            if facts.running or facts.status != "exited":
                raise FenceBroken(
                    f"scoped container {cid} is {facts.status!r} "
                    f"(running={facts.running}) — something started it again "
                    "after the quiesce.",
                    stage="fence-check",
                    detail=facts.state,
                )
            if facts.restart_policy_name != "no":
                raise FenceBroken(
                    f"scoped container {cid} is back on restart policy "
                    f"{facts.restart_policy_name!r}; the daemon could bring it "
                    "up mid-checkpoint.",
                    stage="fence-check",
                    detail=facts.restart_policy,
                )
            containers.append(
                {
                    "container_id": cid,
                    "status": facts.status,
                    "restart_policy": facts.restart_policy_name,
                }
            )

        _require_no_descriptors(
            self.runner,
            (recorded_db, recorded_wal),
            stage="fence-check",
            witness=self.visibility_witness,
        )

        db, wal = _file_facts(recorded_db), _file_facts(recorded_wal)
        for label, current, snapshot in (
            ("database", db, data["database"]["db"]),
            ("WAL", wal, data["database"]["wal"]),
        ):
            if current.identity() != (
                snapshot["exists"],
                snapshot["size_bytes"],
                snapshot["inode"],
                snapshot["sha256"],
            ):
                raise FenceDrift(
                    f"the {label} is not the one this manifest recorded "
                    f"(now size={current.size_bytes} inode={current.inode} "
                    f"sha256={current.sha256}, manifest "
                    f"size={snapshot['size_bytes']} inode={snapshot['inode']} "
                    f"sha256={snapshot['sha256']}). Either something wrote to "
                    "it, or this lease has already been spent on a run that "
                    "changed it.",
                    stage="fence-check",
                    detail={"label": label},
                )

        # The checks themselves take time; a lease that ran out while they ran
        # never authorised anything.
        self._require_authority(when="by the time the checks finished")
        self._require_active()

        return SourceApplyGrant(
            claim_record_path=self.claim_record_path,
            manifest_path=self.manifest.path,
            claim_id=self.claim_id,
            lease_id=self.lease_id,
            ok=True,
            db_path=recorded_db,
            wal_path=recorded_wal,
            db_inode=db.inode,
            db_device=db.device,
            db_sha256=db.sha256,
            wal_inode=wal.inode,
            wal_device=wal.device,
            wal_sha256=wal.sha256,
        )

    def _require_authority(self, *, when: str) -> float:
        """Lease and acknowledgement, both, every time they are relied on.

        The instant is read once and handed back, so whatever the caller goes
        on to write down can be stamped with the very reading that authorised
        it. A record stamped from a later reading asserts that the transition
        began at a time nothing was ever checked at.
        """
        now = float(self.clock())
        _require_authority_at(
            self.manifest,
            self.authority_verifier,
            now,
            when=when,
            stage="fence-check",
        )
        return now

    def complete(self) -> None:
        self._finish(COMPLETED, None)

    def fail(self, reason: str) -> None:
        self._finish(FAILED, str(reason))

    def _finish(self, outcome: str, reason: str | None) -> None:
        # Read the live claim FIRST, exactly as every other transition on this
        # capability does. Without it an object built by hand, carrying a
        # claim id nobody ever issued, can put a FAILED record at the one name
        # the real claim has to write to — and `release` then refuses forever
        # over a record about a claim this fence never granted.
        live = _require_claim_binds(
            self.claim_record_path, self.manifest, self.claim_id
        )
        path = _claim_done_path(self.manifest.path)
        if os.path.lexists(path):
            # Idempotent for the claim that earned it, and for no other: a
            # capability that does not bind here has not just closed anything
            # out by finding somebody else's record in its place.
            existing = _require_claim_binds(path, self.manifest, self.claim_id)
            # And idempotent only for the SAME answer. A record already saying
            # COMPLETED is not a run that failed, and a half-written CLAIMED
            # or APPLYING at the terminal name is not a run that finished at
            # all — reporting either as this outcome hands the caller a result
            # its own fence disagrees with.
            if existing.get("state") != outcome:
                raise ClaimSpent(
                    f"claim {self.claim_id} already finished as "
                    f"{existing.get('state')!r} and cannot now be recorded as "
                    f"{outcome!r}. The durable record is the answer; nothing "
                    "here will write over it.",
                    stage="claim",
                    detail={"path": path, "state": existing.get("state")},
                )
            return
        # With no terminal record on disk, the live claim's own state is the
        # only thing left saying which outcomes are still available. A
        # completion asserts that one pass over the source finished, so it
        # needs APPLYING; a failure can arrive from anywhere before that, so
        # it takes CLAIMED too. A live claim that already reads COMPLETED or
        # FAILED means the terminal record was LOST rather than never
        # written, and writing a fresh one over the top is how a recovery
        # that worked gets recorded as one that did not.
        allowed = _FINISHABLE_FROM[outcome]
        if live.get("state") not in allowed:
            raise ClaimSpent(
                f"claim {self.claim_id} is {live.get('state')!r}, and "
                f"{outcome} can only be recorded from "
                f"{' or '.join(allowed)}. There is no terminal record here to "
                "be idempotent with, so nothing is written.",
                stage="claim",
                detail={"state": live.get("state"), "outcome": outcome},
            )
        _write_record(
            path,
            {
                "schema": APPLY_SCHEMA,
                "state": outcome,
                "claim_id": self.claim_id,
                "lease_id": self.lease_id,
                "manifest_path": self.manifest.path,
                "db_path": self.manifest.data["database"]["db_path"],
                "wal_path": self.manifest.data["database"]["wal_path"],
                "finished_at": float(self.clock()),
                "reason": reason,
            },
        )
        with contextlib.suppress(OSError, ApplyRecordRejected):
            record = read_apply_record(self.claim_record_path)
            _write_record(
                self.claim_record_path,
                {**record, "state": outcome},
                overwrite=True,
            )


class ManifestFence:
    """A QUIESCED manifest, and the one claim that can be taken against it."""

    def __init__(
        self,
        manifest: Manifest,
        *,
        runner: CommandRunner,
        clock: Callable[[], float] = time.time,
        authority_verifier: bytes | None = None,
        visibility_witness=None,
    ):
        self.manifest = manifest
        self.runner = runner
        self.clock = clock
        self.authority_verifier = authority_verifier
        self.visibility_witness = visibility_witness

    @classmethod
    def from_file(
        cls,
        path: str | os.PathLike[str],
        *,
        runner: CommandRunner,
        clock: Callable[[], float] = time.time,
        authority_verifier: bytes | None = None,
        visibility_witness=None,
    ) -> "ManifestFence":
        return cls(
            read_manifest(path),
            runner=runner,
            clock=clock,
            authority_verifier=authority_verifier,
            visibility_witness=visibility_witness,
        )

    def reattach(self) -> SourceApplyCapability:
        """Pick a claim back up after a restart, having checked it is ours."""
        record = _require_claim_binds(
            _claim_path(self.manifest.path),
            self.manifest,
            None,
        )
        return SourceApplyCapability(
            self.manifest,
            record["claim_id"],
            runner=self.runner,
            clock=self.clock,
            authority_verifier=self.authority_verifier,
            visibility_witness=self.visibility_witness,
        )

    def claim(self) -> SourceApplyCapability:
        """Take the single capability this fence can issue, or refuse.

        The exclusive create is what makes it atomic: two processes racing here
        produce exactly one winner and one `ClaimUnavailable`, with no window
        in which both believe they hold the lease.
        """
        base = self.manifest.path
        data = self.manifest.data
        if data.get("state") != QUIESCED:
            raise ManifestRejected(
                f"manifest {base!r} is in state {data.get('state')!r}, not "
                f"{QUIESCED}",
                stage="claim",
            )
        if os.path.lexists(_release_path(base)):
            raise ClaimUnavailable(
                f"{base!r} has already been released; its lease cannot be "
                "claimed again. Quiesce the host afresh.",
                stage="claim",
            )
        if os.path.lexists(_claim_done_path(base)):
            raise ClaimUnavailable(
                f"the claim on {base!r} has already been spent", stage="claim"
            )
        _require_authority_at(
            self.manifest,
            self.authority_verifier,
            float(self.clock()),
            when="at the claim",
            stage="claim",
        )

        # Re-measured here, not trusted from the quiesce: a writer or a start
        # authority that appeared in between is exactly what this is for.
        declared = {entry["container_id"] for entry in data["scope"]}
        inventory = _host_inventory(self.runner, stage="claim")
        _require_inventory_bound(
            inventory, data["mount"]["source"], declared, stage="claim"
        )

        # Asked again, HERE. The inventory above is a `docker ps` and an
        # inspect per container on the host, and on a busy one that is not
        # instant. The reading that authorises the claim has to be the one
        # taken immediately before the CLAIMED file exists — otherwise the
        # transition starts on the strength of a check that has since lapsed —
        # and it is that reading the record is stamped with.
        claimed_at = float(self.clock())
        _require_authority_at(
            self.manifest,
            self.authority_verifier,
            claimed_at,
            when="once the host inventory came back",
            stage="claim",
        )

        claim_id = secrets.token_hex(16)
        path = _claim_path(base)
        try:
            handle = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            raise ClaimUnavailable(
                f"{base!r} is already claimed; a lease authorises one source "
                "apply and no more",
                stage="claim",
            ) from None
        body = json.dumps(
            _seal(
                {
                    "schema": APPLY_SCHEMA,
                    "state": CLAIMED,
                    "claim_id": claim_id,
                    "lease_id": self.manifest.lease_id,
                    "manifest_path": base,
                    "db_path": data["database"]["db_path"],
                    "wal_path": data["database"]["wal_path"],
                    "claimed_at": claimed_at,
                }
            ),
            indent=2,
            sort_keys=True,
        )
        try:
            os.write(handle, (body + "\n").encode())
            os.fsync(handle)
        finally:
            os.close(handle)
        # The directory entry too. A claim whose NAME never reached the platter
        # cannot stop a second claim after a power loss, however durable its
        # bytes were.
        directory = os.open(os.path.dirname(path) or ".", os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        return SourceApplyCapability(
            self.manifest,
            claim_id,
            runner=self.runner,
            clock=self.clock,
            authority_verifier=self.authority_verifier,
            visibility_witness=self.visibility_witness,
        )


# ── release: separate, explicit, and stopped by default ───


def _release_path_exists(path: str, *, what: str) -> bool:
    """Does this path exist? — keeping "I could not find out" separate.

    `os.path.lexists` answers `False` for every error `lstat` can raise, so a
    directory this process cannot read, a disk returning EIO, and a name that
    genuinely holds no record all come back as the same word. Everywhere else
    that conflation is merely untidy; here the `False` is what lets the
    restart policies go back and the containers start, so the unanswered case
    is raised instead of rounded down to absent.
    """
    try:
        os.lstat(path)
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise ReleaseFailed(
            f"cannot tell whether the {what} at {path!r} exists ({exc}). "
            "Restoring restart policies on an unanswered question is how a "
            "database gets handed back to a writer mid-checkpoint, so nothing "
            "on the host is changed until it has an answer.",
            stage="release",
            detail={"path": path},
        ) from exc
    return True


def _confirm_durable(path: str, *, stage: str) -> None:
    """Ask the filesystem, now, to make this record and its NAME durable.

    `_write_record` renames into place and only then fsyncs the directory, so
    a failure at that last step leaves a file this process can read whose
    directory entry may not survive a power loss. Redoing both — the file and
    its parent — is the only thing that turns a record which is merely visible
    into one that is known to be on the platter.
    """
    parent = os.path.dirname(path) or "."
    try:
        handle = os.open(path, os.O_RDONLY)
        try:
            os.fsync(handle)
        finally:
            os.close(handle)
        directory = os.open(parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except OSError as exc:
        raise ReleaseFailed(
            f"the terminal record at {path!r} cannot be confirmed durable "
            f"({exc}). It is visible here, and after a power loss it may not "
            "be — in which case the claim comes back as live while the "
            "containers are running again. Nothing on the host is changed "
            "until that question has an answer.",
            stage=stage,
            detail={"path": path},
        ) from exc


def _read_release_record(path: str, *, what: str, expect_states=None) -> dict:
    """Read a record release is about to act on, or refuse.

    Nothing here is suppressed. A record that cannot be opened, or that does
    not match its own digest, is not a smaller record — and on this path the
    difference between "checked" and "could not check" is whether the
    containers come back up.
    """
    try:
        return read_apply_record(path, expect_states=expect_states)
    except ApplyRecordRejected as exc:
        raise ReleaseFailed(
            f"the {what} at {path!r} is not one this fence will act on: "
            f"{exc}. A claim that cannot be shown to have finished is treated "
            "as one that has not.",
            stage="release",
            detail={"path": path},
        ) from exc
    except OSError as exc:
        raise ReleaseFailed(
            f"the {what} at {path!r} cannot be read ({exc}). Nothing on the "
            "host is changed while a record release depends on is unreadable.",
            stage="release",
            detail={"path": path},
        ) from exc


def _require_durable_terminal(base: str, lease_id: str) -> dict:
    """Prove the claim is spent, rather than reading a file name as proof.

    A path that exists says a write reached the directory once. It does not
    say the record is about this claim, that its state is terminal, that it
    was not edited afterwards, or that its name is durable — and every one of
    those has to hold before release puts the restart policies back.
    """
    path = _claim_done_path(base)
    record = _read_release_record(
        path, what="terminal claim record", expect_states={COMPLETED, FAILED}
    )
    # The live claim is what the terminal record has to be ABOUT, so it is
    # required rather than consulted when convenient. Skipping it over a bad
    # read does not skip a check — it removes the `claim_id` and `lease_id`
    # the terminal record was going to be compared against, and what is left
    # passes.
    live = _read_release_record(_claim_path(base), what="live claim record")

    bindings = [
        ("manifest_path", base),
        ("claim_id", live.get("claim_id")),
        ("lease_id", live.get("lease_id")),
    ]
    # The live claim has to belong to this fence before it can vouch for
    # anything: once it does, comparing the terminal record to it is the same
    # comparison as against the manifest.
    if lease_id and live.get("lease_id") != lease_id:
        raise ReleaseFailed(
            f"the live claim record at {_claim_path(base)!r} was issued "
            f"against lease {live.get('lease_id')!r}, and this manifest holds "
            f"{lease_id!r}.",
            stage="release",
        )
    if live.get("manifest_path") != base:
        raise ReleaseFailed(
            f"the live claim record at {_claim_path(base)!r} belongs to "
            f"{live.get('manifest_path')!r}, not to this fence.",
            stage="release",
        )
    for field_name, expected in bindings:
        if record.get(field_name) != expected:
            raise ReleaseFailed(
                f"the terminal record at {path!r} says {field_name}="
                f"{record.get(field_name)!r}, and this fence is {expected!r}. "
                "A record about some other claim is not evidence that this "
                "one finished.",
                stage="release",
            )

    _confirm_durable(path, stage="release")
    return record


def release(
    *,
    runner: CommandRunner,
    manifest_path: str | os.PathLike[str] | None = None,
    prepared_path: str | os.PathLike[str] | None = None,
    start_containers: bool = False,
    start_allowlist: Sequence[str] = (),
    clock: Callable[[], float] = time.time,
) -> ReleaseRecord:
    """Put the recorded restart policies back. Nothing else, unless asked.

    The policies come from the PREPARED record, which is on disk before the
    first change is made — so this works even when the quiesce died halfway and
    no manifest was ever written.

    Containers stay stopped by default, and `start_containers=True` still only
    starts the ones that were running when the fence arrived. A container that
    was already down is not "restored" by being brought up; naming it in
    `start_allowlist` is the only way to start it.
    """
    if manifest_path is None and prepared_path is None:
        raise ManifestRejected(
            "release needs a manifest or a prepared record to restore from",
            stage="release",
        )
    if manifest_path is not None:
        base = os.fspath(manifest_path)
        prepared = os.fspath(prepared_path or prepared_path_for(base))
    else:
        prepared = os.fspath(prepared_path)
        base = (
            prepared[: -len(".prepared.json")]
            if prepared.endswith(".prepared.json")
            else prepared
        )

    record = read_prepared(prepared)
    # Every one of these questions is asked once, before anything on the host
    # moves, and each has to come back as a real answer rather than as the
    # `False` an unreadable path would otherwise produce.
    done = _claim_done_path(base)
    record_path = _release_path(base)
    manifest_exists = _release_path_exists(base, what="manifest")
    claim_exists = _release_path_exists(_claim_path(base), what="claim record")
    done_exists = _release_path_exists(done, what="terminal claim record")
    already_released = _release_path_exists(record_path, what="release record")

    lease_id = ""
    if manifest_exists:
        # Present but unreadable is not the same as absent. The lease named
        # here is what the terminal claim record is checked against, and an
        # empty one checks nothing.
        try:
            lease_id = read_manifest(base).lease_id
        except (ManifestRejected, OSError) as exc:
            raise ReleaseFailed(
                f"the manifest at {base!r} is there and cannot be read "
                f"({exc}). Its lease is what proves the claim record belongs "
                "to this fence, so nothing on the host is changed.",
                stage="release",
                detail={"path": base},
            ) from exc
    elif claim_exists or done_exists:
        # A quiesce that died before writing a manifest leaves a PREPARED
        # record and nothing else, and releasing from that alone is the
        # ordinary thing to do. This is not that: a claim was taken, and the
        # one file that says which lease and which database it was about is
        # the file that is gone. What is left binds to nothing, and treating
        # it as a fence that never got as far as a claim starts the
        # containers over records nobody can account for.
        raise ReleaseFailed(
            f"the manifest at {base!r} is gone, but this fence still has "
            f"{'a claim record' if claim_exists else ''}"
            f"{' and ' if claim_exists and done_exists else ''}"
            f"{'a terminal claim record' if done_exists else ''}. A claim was "
            "taken here and there is no longer anything to bind it to, so "
            "nothing on the host is changed.",
            stage="release",
            detail={"path": base, "claim": claim_exists, "terminal": done_exists},
        )

    # Nothing goes back while a claim is live. A restart policy restored, or a
    # container started, in the middle of a checkpoint is the fence undoing
    # itself at the worst possible moment.
    if claim_exists and not done_exists:
        state = APPLYING if os.path.lexists(_applying_path(base)) else CLAIMED
        raise ClaimInFlight(
            f"{base!r} is {state}: a source apply is authorised and has not "
            "finished. Nothing will be restored or started until it records a "
            "terminal state.",
            stage="release",
        )

    if already_released:
        raise AlreadyReleased(
            f"{base!r} was already released; its record is at {record_path!r}",
            stage="release",
        )

    # Everything below this line changes the host: the first `docker update`
    # is the moment the daemon can bring the containers back. A claim was
    # taken against this fence, so before that happens the record saying it
    # finished is read, checked against this fence, and confirmed durable —
    # the file merely being there is what a half-written terminal state looks
    # like too.
    if done_exists or claim_exists:
        _require_durable_terminal(base, lease_id)

    allow = set(start_allowlist)
    restored, started, not_started = [], [], []
    for entry in record.scope:
        cid = entry["container_id"]
        before = entry.get("restart_policy_before", {})
        wanted_name = before.get("name", "") or "no"
        wanted_retries = int(before.get("maximum_retry_count", 0) or 0)
        if _try_inspect(runner, cid) is None:
            raise ReleaseFailed(
                f"scoped container {cid} can no longer be inspected; its "
                f"restart policy ({wanted_name!r}) has not been restored.",
                stage="release",
                detail={"restored": list(restored)},
            )
        _docker(
            runner,
            "update",
            f"--restart={_format_policy(wanted_name, wanted_retries)}",
            cid,
            stage="release",
        )
        after = _inspect(runner, cid, stage="release")
        if (after.restart_policy_name, after.restart_max_retries) != (
            wanted_name,
            wanted_retries,
        ):
            raise ReleaseFailed(
                f"container {cid} reads back restart policy "
                f"{after.restart_policy!r} after being restored to "
                f"{before!r}; the host is not where the record says it should "
                "be, and nothing further will be changed.",
                stage="release",
                detail={"restored": list(restored), "readback": after.restart_policy},
            )
        restored.append(cid)

    if start_containers:
        for entry in record.scope:
            cid = entry["container_id"]
            was_running = bool(entry.get("state_before", {}).get("running", False))
            if not was_running and cid not in allow:
                # It was down before any of this. Starting it now would not be
                # restoring anything; it would be a decision nobody made.
                not_started.append(cid)
                continue
            _docker(runner, "start", cid, stage="release-start")
            after = _inspect(runner, cid, stage="release-start")
            if not after.running:
                raise ReleaseFailed(
                    f"container {cid} is {after.status!r} after being started",
                    stage="release-start",
                    detail={"started": list(started)},
                )
            started.append(cid)
    else:
        not_started = [entry["container_id"] for entry in record.scope]

    _write_record(
        record_path,
        {
            "schema": SCHEMA,
            "state": RELEASED,
            "released_at": float(clock()),
            "manifest_path": base,
            "prepared_path": prepared,
            "lease_id": lease_id,
            "restored": restored,
            "started_containers": started,
            "not_started": not_started,
        },
    )
    return ReleaseRecord(
        path=record_path,
        lease_id=lease_id,
        restored=tuple(restored),
        started_containers=tuple(started),
        not_started=tuple(not_started),
    )
