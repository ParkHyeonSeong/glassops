"""One command that runs the WAL recovery the way it has to be run: in order.

`app.wal_fence` (CP-5B) holds the host still and `app.wal_recovery` (CP-5A)
folds a crashed database's WAL back in. Each refuses on its own terms, but
neither can make the other be called first, or at all. This is the operator's
entry point that does exactly that, and nothing else:

    fence     quiesce the host and re-read the QUIESCED manifest from disk
    claim     take the one-shot capability the manifest can issue
    recovery  back up, prove the backup, and checkpoint the database path the
              FENCE derived from the mount — never one typed on the command line
    terminal  confirm the claim was spent as COMPLETED
    release   put the recorded restart policies back; containers stay stopped

It is run from the `backend/` directory, where the `app` package lives:

    cd backend && python3 -m app.wal_recovery_cli --project ... --service ...

Every stage has its own exit code, and the first refusal ends the command with
that stage's code and name on stderr. That boundary covers the stage's work,
the line reporting it, and — for the terminal stage — the check of the record
on disk, so an interrupt or a broken pipe after the host has been changed is
still reported as the stage it landed in, never as a traceback. Nothing is
deleted, nothing is put back, and nothing is retried: a fence that refused
leaves the host stopped and pinned, a recovery that refused leaves its FAILED
claim and its backup where they are, and a terminal record that could not be
written leaves the claim live. Putting the host back after a failure is
`wal_fence.release()`, a separate call a person makes once they know what
happened.

Inputs the recovery would refuse — a run id that is not a plain name, a table
or column that is not a bare identifier, a backup root that is a symlink — are
refused here first, with the recovery's own rules, because refused there they
would fire after the containers were already stopped.

The command runners, clock and descriptor witness are arguments so the whole
sequence can be exercised against a scripted host. Running it against a real
one is a separate decision this module does not make.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import math
import os
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from app import wal_fence, wal_recovery

__all__ = [
    "EXIT_OK",
    "EXIT_USAGE",
    "EXIT_FENCE",
    "EXIT_CLAIM",
    "EXIT_RECOVERY",
    "EXIT_TERMINAL",
    "EXIT_RELEASE",
    "STAGE_CODES",
    "main",
]

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_FENCE = 10
EXIT_CLAIM = 11
EXIT_RECOVERY = 12
EXIT_TERMINAL = 13
EXIT_RELEASE = 14

#: The stages in the order they run, each with the code the command exits
#: with when that stage is the one that refused.
STAGE_CODES = {
    "fence": EXIT_FENCE,
    "claim": EXIT_CLAIM,
    "recovery": EXIT_RECOVERY,
    "terminal": EXIT_TERMINAL,
    "release": EXIT_RELEASE,
}

COMMAND = "cd backend && python3 -m app.wal_recovery_cli"


class _UsageError(Exception):
    """An argument or input file the command cannot act on. No host command
    has been issued when this is raised."""


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise _UsageError(f"{self.prog}: error: {message}\n{self.format_usage()}")


class _StageFailed(Exception):
    """One stage refused, broke, or was interrupted; the command stops here.

    `completed` means the stage's work had finished — its records are on
    disk — and only reporting or checking it failed. `released` carries the
    release record when the release itself went through.
    """

    def __init__(
        self,
        stage: str,
        cause: BaseException,
        *,
        report=None,
        completed: bool = False,
        released=None,
    ):
        super().__init__(f"{stage}: {cause}")
        self.stage = stage
        self.cause = cause
        self.report = report
        self.completed = completed
        self.released = released


@dataclass(frozen=True)
class _Inputs:
    project: str
    service: str
    container_id: str
    scope: tuple
    data_destination: str
    db_relpath: str
    manifest_path: str
    host_id: str
    ack: wal_fence.ExternalAuthorityAck
    verifier: bytes
    stop_deadline_seconds: float
    lease_ttl_seconds: int
    backup_root: str
    run_id: str
    sentinel: wal_recovery.Sentinel
    probes: tuple


# ── arguments ────────────────────────────────────────


def _positive_int(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{text!r} is not an integer") from None
    if value <= 0:
        raise argparse.ArgumentTypeError(f"must be a positive number of seconds, got {value}")
    return value


def _finite_positive(text: str) -> float:
    try:
        value = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{text!r} is not a number") from None
    # `float()` happily parses nan and inf; a deadline of either is no deadline.
    if not math.isfinite(value) or value <= 0:
        raise argparse.ArgumentTypeError(
            f"must be a finite, positive number of seconds, got {text!r}"
        )
    return value


def _build_parser() -> _Parser:
    parser = _Parser(
        prog="python3 -m app.wal_recovery_cli",
        description=(
            "Fence the host, claim the lease, recover the WAL, confirm the "
            "outcome, release the restart policies — stopping at the first "
            "refusal. Exit codes: 0 ok, 2 usage, 10 fence, 11 claim, "
            "12 recovery, 13 terminal, 14 release."
        ),
        epilog=f"Run from the backend/ directory: {COMMAND} ...",
    )
    target = parser.add_argument_group("target (wal_fence)")
    target.add_argument("--project", required=True, help="exact Compose project name")
    target.add_argument("--service", required=True, help="exact Compose service name")
    target.add_argument(
        "--container-id", required=True, help="FULL 64-character id of the service container"
    )
    target.add_argument(
        "--scope",
        action="append",
        required=True,
        metavar="SERVICE=CONTAINER_ID",
        help="a container that must be stopped for the fence to hold; repeat "
        "for every container sharing the data mount or holding the Docker socket",
    )
    target.add_argument(
        "--data-destination", default="/app/data", help="mount point inside the container"
    )
    target.add_argument(
        "--db-relpath", default="glassops.db", help="database path relative to the mount"
    )
    target.add_argument(
        "--manifest", required=True, help="where to write the QUIESCED manifest (must not exist)"
    )
    target.add_argument(
        "--stop-deadline-seconds",
        type=_finite_positive,
        default=120.0,
        help="how long to wait for each graceful stop before giving up the wait "
        "(the container is never killed)",
    )
    target.add_argument(
        "--lease-ttl-seconds",
        type=_positive_int,
        default=900,
        help="how long the fence's lease lasts; must end inside the acknowledgement window",
    )

    authority = parser.add_argument_group("external authority acknowledgement")
    authority.add_argument("--host-id", required=True, help="the host id the acknowledgement was signed for")
    authority.add_argument(
        "--ack-file",
        required=True,
        help="JSON file with issuer, issued_at, expires_at, host_id, scope_digest, signature",
    )
    authority.add_argument(
        "--ack-key-file", required=True, help="file whose exact bytes are the HMAC verifier key"
    )

    recovery = parser.add_argument_group("recovery (wal_recovery)")
    recovery.add_argument("--backup-root", required=True, help="existing directory for the backup run")
    recovery.add_argument("--run-id", required=True, help="name of this run's directory under the backup root")
    recovery.add_argument(
        "--sentinel",
        required=True,
        metavar="TABLE:COLUMN:VALUE",
        help="one row that must survive the recovery, addressed by an exact string value",
    )
    recovery.add_argument(
        "--probe",
        action="append",
        required=True,
        metavar="TABLE[:ID_COLUMN[:TIMESTAMP_COLUMN]]",
        help="a table whose row count, max id and max timestamp are compared before and after",
    )
    return parser


def _scoped(entry: str) -> wal_fence.ScopedContainer:
    service, separator, container_id = entry.partition("=")
    if not separator or not service or not container_id:
        raise _UsageError(f"--scope needs SERVICE=CONTAINER_ID, got {entry!r}")
    return wal_fence.ScopedContainer(service=service, container_id=container_id)


def _sentinel(spec: str) -> wal_recovery.Sentinel:
    parts = spec.split(":", 2)
    if len(parts) != 3 or not all(parts):
        raise _UsageError(f"--sentinel needs TABLE:COLUMN:VALUE, got {spec!r}")
    return wal_recovery.Sentinel(table=parts[0], column=parts[1], value=parts[2])


def _probe(spec: str) -> wal_recovery.Probe:
    parts = spec.split(":")
    if not 1 <= len(parts) <= 3 or not all(parts):
        raise _UsageError(
            f"--probe needs TABLE[:ID_COLUMN[:TIMESTAMP_COLUMN]], got {spec!r}"
        )
    return wal_recovery.Probe(
        table=parts[0],
        id_column=parts[1] if len(parts) > 1 else "id",
        timestamp_column=parts[2] if len(parts) > 2 else None,
    )


def _read_ack(path: str) -> wal_fence.ExternalAuthorityAck:
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
        ack = wal_fence.ExternalAuthorityAck(**data)
        return wal_fence.ExternalAuthorityAck(
            issuer=ack.issuer,
            issued_at=float(ack.issued_at),
            expires_at=float(ack.expires_at),
            host_id=ack.host_id,
            scope_digest=ack.scope_digest,
            signature=ack.signature,
        )
    except (OSError, ValueError, TypeError) as exc:
        raise _UsageError(
            f"--ack-file {path!r} is not an acknowledgement this command can "
            f"read: {exc}"
        ) from exc


def _read_key(path: str) -> bytes:
    try:
        with open(path, "rb") as handle:
            key = handle.read()
    except OSError as exc:
        raise _UsageError(f"--ack-key-file {path!r} cannot be read: {exc}") from exc
    if not key:
        raise _UsageError(f"--ack-key-file {path!r} is empty")
    return key


def _recovery_rule(check, *args) -> None:
    """Apply one of the recovery's own input rules now, as a usage error.

    These are the recovery module's validators, called rather than copied:
    the rule has to be exactly the one `rehearse_wal_recovery` will apply,
    and the only reason to apply it here is timing — there it fires after
    the containers have been stopped and a claim taken.
    """
    try:
        check(*args)
    except wal_recovery.WalRecoveryError as exc:
        raise _UsageError(str(exc)) from exc


def _load_inputs(args) -> _Inputs:
    """Everything the stages need, checked as far as it can be checked
    without touching the host — a refusal here costs nothing, one after the
    fence has stopped the service costs a release and a rerun."""
    backup_root = os.path.abspath(args.backup_root)
    _recovery_rule(wal_recovery._validate_backup_root, backup_root)
    _recovery_rule(wal_recovery._validate_run_id, args.run_id)
    sentinel = _sentinel(args.sentinel)
    probes = tuple(_probe(spec) for spec in args.probe)
    _recovery_rule(wal_recovery._validate_query_spec, sentinel, probes)
    if os.path.lexists(os.path.join(backup_root, args.run_id)):
        raise _UsageError(
            f"--run-id {args.run_id!r} already has a directory under {backup_root!r}; "
            "pick a new run id rather than writing over an earlier backup"
        )
    return _Inputs(
        project=args.project,
        service=args.service,
        container_id=args.container_id,
        scope=tuple(_scoped(entry) for entry in args.scope),
        data_destination=args.data_destination,
        db_relpath=args.db_relpath,
        manifest_path=os.path.abspath(args.manifest),
        host_id=args.host_id,
        ack=_read_ack(args.ack_file),
        verifier=_read_key(args.ack_key_file),
        stop_deadline_seconds=args.stop_deadline_seconds,
        lease_ttl_seconds=args.lease_ttl_seconds,
        backup_root=backup_root,
        run_id=args.run_id,
        sentinel=sentinel,
        probes=probes,
    )


# ── the stages, in order ─────────────────────────────


@contextlib.contextmanager
def _stage(name: str, *, report=None, completed: bool = False, released=None):
    """Everything inside belongs to stage `name`, whatever goes wrong in it.

    `BaseException`, deliberately: from the first `docker update` on, an
    interrupt is a thing that happened to the host at a known point, and the
    exit code and the stderr account are the only things the operator will
    have. Letting it unwind into a traceback would lose both.
    """
    try:
        yield
    except _StageFailed:
        raise
    except BaseException as exc:
        raise _StageFailed(
            name, exc, report=report, completed=completed, released=released
        ) from exc


def _run(inputs: _Inputs, runner, clock, witness, out) -> None:
    with _stage("fence"):
        manifest = wal_fence.fence(
            project=inputs.project,
            service=inputs.service,
            container_id=inputs.container_id,
            scope=inputs.scope,
            data_destination=inputs.data_destination,
            db_relpath=inputs.db_relpath,
            manifest_path=inputs.manifest_path,
            runner=runner,
            external_authority_ack=inputs.ack,
            authority_verifier=inputs.verifier,
            host_id=inputs.host_id,
            visibility_witness=witness,
            stop_deadline_seconds=inputs.stop_deadline_seconds,
            lease_ttl_seconds=inputs.lease_ttl_seconds,
            clock=clock,
        )
        # Re-read from disk rather than trusted from memory: the record on
        # disk is what every later step, and every later person, will act on.
        guard = wal_fence.ManifestFence.from_file(
            manifest.path,
            runner=runner,
            clock=clock,
            authority_verifier=inputs.verifier,
            visibility_witness=witness,
        )
    with _stage("fence", completed=True):
        lease = guard.manifest.data.get("lease", {})
        _say(
            out,
            f"[fence] quiesced {inputs.project}/{inputs.service} "
            f"{inputs.container_id[:12]}: manifest={guard.manifest.path} "
            f"lease={guard.manifest.lease_id} expires_at={lease.get('expires_at')} "
            f"db={guard.manifest.db_path}",
        )

    with _stage("claim"):
        capability = guard.claim()
    with _stage("claim", completed=True):
        _say(out, f"[claim] claim={capability.claim_id} record={capability.claim_record_path}")

    try:
        report = wal_recovery.rehearse_wal_recovery(
            db_path=guard.manifest.db_path,
            backup_root=inputs.backup_root,
            run_id=inputs.run_id,
            sentinel=inputs.sentinel,
            probes=inputs.probes,
            fence=capability,
        )
    except BaseException as exc:
        # The recovery raises exactly this pairing when the run itself
        # finished and only the COMPLETED record could not be written: the
        # source has been checkpointed and the claim is still live, which is
        # a different situation from a recovery that refused. Anything else
        # — a refusal, a driver error, an interrupt — is the recovery stage,
        # and carries the recovery's own account of how far it got.
        terminal_lost = (
            isinstance(exc, wal_recovery.SourceApplyFailed) and exc.stage == "claim"
        )
        raise _StageFailed(
            "terminal" if terminal_lost else "recovery", exc, report=_report_of(exc)
        ) from exc
    with _stage("recovery", report=report, completed=True):
        checkpoint = report.source_checkpoint
        _say(
            out,
            f"[recovery] run={report.run_id} dir={report.run_dir} "
            f"db {checkpoint.db_bytes_before}->{checkpoint.db_bytes_after} bytes, "
            f"wal {checkpoint.wal_bytes_before}->{checkpoint.wal_bytes_after} bytes; "
            f"stages={','.join(report.stages)}",
        )

    # The report says the claim was spent; the disk has to agree before the
    # restart policies go anywhere near the host.
    with _stage("terminal", report=report):
        active = capability.is_active
    if report.claim_outcome != "completed" or active:
        raise _StageFailed(
            "terminal",
            RuntimeError(
                f"the recovery reported claim_outcome={report.claim_outcome!r} "
                f"but the capability is {'still active' if active else 'spent'}; "
                "the record on disk does not say COMPLETED, so nothing is released"
            ),
            report=report,
        )
    with _stage("terminal", report=report, completed=True):
        _say(out, f"[terminal] COMPLETED claim={capability.claim_id}")

    with _stage("release", report=report):
        record = wal_fence.release(
            manifest_path=guard.manifest.path, runner=runner, clock=clock
        )
    with _stage("release", report=report, completed=True, released=record):
        _say(
            out,
            f"[release] restored={len(record.restored)} "
            f"started={len(record.started_containers)} "
            f"not_started={len(record.not_started)} record={record.path}",
        )
        _say(
            out,
            "OK: WAL recovered and restart policies restored; the containers "
            "were left stopped and are yours to start.",
        )


def _report_of(exc: BaseException):
    """The recovery's account of how far it got, wherever it was attached."""
    return getattr(exc, "report", None) or getattr(exc, "recovery_report", None)


def _say(out, line: str) -> None:
    print(line, file=out, flush=True)


def _records_beside(manifest_path: str) -> list:
    """Every file the fence and the recovery left next to the manifest, by name.

    Listed rather than named one by one: the point is to show the operator
    what is on disk, and a list from the directory cannot be out of date with
    the modules that wrote it.
    """
    directory = os.path.dirname(manifest_path) or "."
    prefix = os.path.basename(manifest_path)
    try:
        names = os.listdir(directory)
    except OSError:
        return []
    return sorted(
        os.path.join(directory, name)
        for name in names
        if name == prefix or name.startswith(prefix + ".")
    )


def _next_step(failure: _StageFailed, inputs: _Inputs) -> str:
    """What the operator does now — which depends on what the claim is doing."""
    reattach = (
        f"wal_fence.ManifestFence.from_file({inputs.manifest_path!r}, "
        "runner=..., authority_verifier=...).reattach()"
    )
    if failure.stage == "claim" and failure.completed:
        return (
            "The claim WAS taken and is live, so release would refuse it: "
            f"reattach it with {reattach} and record fail(...) on it by hand "
            "before wal_fence.release()."
        )
    report = failure.report
    if (
        report is not None
        and report.claim_spend_error is not None
        and report.claim_outcome is None
    ):
        return (
            "The claim is still live because its outcome could not be "
            "recorded (see claim_spend_error above). Fix that cause, then "
            f"reattach it with {reattach} and record complete() or fail(...) "
            "by hand — whichever the recovery line above says happened — "
            "before wal_fence.release()."
        )
    return (
        "Putting the host back is wal_fence.release(), by hand, once you know "
        "why this refused."
    )


def _explain(failure: _StageFailed, inputs: _Inputs, err) -> None:
    cause = failure.cause
    code = getattr(cause, "code", None)
    label = type(cause).__name__ + (f"/{code}" if code else "")
    _say(
        err,
        f"FAILED at stage {failure.stage} (exit {STAGE_CODES[failure.stage]}): "
        f"{label}: {str(cause) or '(no message)'}",
    )
    where = getattr(cause, "stage", None)
    if where:
        _say(err, f"  refused at: {where}")
    if failure.completed:
        _say(
            err,
            "  note: this stage's work had finished and its records are on "
            "disk; only reporting or checking it failed.",
        )
    report = failure.report
    if report is not None:
        _say(
            err,
            f"  recovery: source_touched={report.checkpoint_started} "
            f"claim_outcome={report.claim_outcome} "
            f"claim_spend_error={report.claim_spend_error!r} "
            f"run_dir={report.run_dir} "
            f"stages={','.join(report.stages) or '-'}",
        )
    preserved = _records_beside(inputs.manifest_path)
    _say(err, "  preserved: " + (" ".join(preserved) if preserved else "(no record written)"))
    if failure.stage == "release":
        _explain_release(failure, err)
        return
    _say(
        err,
        "  nothing was deleted, put back, or retried; the host is exactly as "
        f"this stage left it. {_next_step(failure, inputs)}",
    )


def _explain_release(failure: _StageFailed, err) -> None:
    if failure.released is not None:
        _say(
            err,
            f"  release completed: record={failure.released.path} "
            f"restored={', '.join(failure.released.restored) or 'none'}; "
            "the restart policies are back and the containers were left "
            "stopped. There is nothing to put back.",
        )
        return
    # Release restores one container at a time, so by the time it refuses
    # some policies may already be back. `release()` names the ones it
    # finished on the refusal itself; a host error or an interrupt mid-command
    # does not, and "unknown" is the honest word for that.
    detail = getattr(failure.cause, "detail", None)
    restored = detail.get("restored") if isinstance(detail, dict) else None
    _say(
        err,
        "  restart policies already restored: "
        + (
            "unknown (release was cut off mid-way; inspect the host before acting)"
            if restored is None
            else ", ".join(restored) or "none"
        ),
    )
    _say(
        err,
        "  nothing was deleted or retried and no container was started; the "
        "remaining policies are as release left them. Finishing the release "
        "is wal_fence.release(), by hand, once you know why this stopped.",
    )


def main(
    argv: Sequence[str] | None = None,
    *,
    runner: wal_fence.CommandRunner = wal_fence.subprocess_runner,
    clock: Callable[[], float] = time.time,
    visibility_witness=None,
    stdout=None,
    stderr=None,
) -> int:
    out = stdout if stdout is not None else sys.stdout
    err = stderr if stderr is not None else sys.stderr
    try:
        inputs = _load_inputs(_build_parser().parse_args(argv))
    except _UsageError as exc:
        _say(err, str(exc))
        return EXIT_USAGE

    try:
        _run(inputs, runner, clock, visibility_witness, out)
    except _StageFailed as failure:
        _explain(failure, inputs, err)
        return STAGE_CODES[failure.stage]
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
