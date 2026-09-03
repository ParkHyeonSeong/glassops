"""Fold a crashed database's WAL-only commits back into the database file.

When a process holding a SQLite connection is killed, every commit that had not
yet been checkpointed lives on ONLY in the `-wal` sidecar. The main database
file is intact but stale, and copying it on its own — the shape most backup
scripts take — silently discards those commits. This module carries the WAL
along, proves the copy is byte-identical to what it found, and only then folds
the WAL into the database.

The whole procedure is built around one rule: **nothing destructive happens
until a verified backup exists, and nothing is cleaned up when a step fails.**
Concretely it never removes or truncates a WAL by hand, never renames or
replaces the source, never rebuilds the file in place, never changes how the
database journals, and never restores a backup over an original by itself.
A failure leaves the source where it is and leaves every file the run had
already written where it is, for a human to look at.

It deliberately depends on nothing but the standard library and takes no part
in the application's runtime: recovery runs against a database file that no
process owns, usually with the service stopped, so reaching for the
application's own pooled connection would be both unavailable and unsafe.

Order of operations, and why it is this order:

    1.  validate the paths, refusing anything that is not an existing, real,
        absolute file, and refusing to write into an existing run directory
    2.  measure the space this run actually needs, item by item, and stop
        before the first byte is copied if it does not fit
    3.  record the source's identity (inode, size, SHA-256)
    4.  copy database AND WAL together into a fresh run directory, fsync both
        the files and the directory, and re-hash the copy against the source
    5.  copy that verified snapshot to a second, disposable location and read
        the pre-checkpoint baseline THERE — never from the source, because
        merely opening a WAL database can checkpoint it when the connection
        closes, which would mutate the very thing step 4 just certified
    6.  prove the backup is recoverable by checkpointing and verifying that
        disposable copy first
    7.  only then checkpoint the source, and verify it against the same
        baseline

Steps 5 and 6 come before the source is touched on purpose: by the time the
original is modified, an identical copy has already been carried all the way
through the same procedure and checked.
"""

from __future__ import annotations

import abc
import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

__all__ = [
    "WalRecoveryError",
    "SourceRejected",
    "BackupRejected",
    "ProbeRejected",
    "OracleInvalid",
    "InsufficientSpace",
    "SnapshotFailed",
    "CheckpointFailed",
    "VerificationFailed",
    "FenceRequired",
    "FenceRefused",
    "SourceApplyFailed",
    "SourceApplyAuthority",
    "SourceApplyGrant",
    "ApplyRecordRejected",
    "APPLY_SCHEMA",
    "CLAIMED",
    "APPLYING",
    "COMPLETED",
    "FAILED",
    "seal_apply_record",
    "read_apply_record",
    "Sentinel",
    "Probe",
    "FileFacts",
    "TableFacts",
    "Readback",
    "SpacePlan",
    "CheckpointResult",
    "WalOnlyEvidence",
    "RecoveryReport",
    "wal_path",
    "file_facts",
    "plan_space",
    "assert_wal_only_commit",
    "rehearse_wal_recovery",
]

_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_CHUNK = 1 << 20
_DIR_MODE = 0o700


# ── typed failures ───────────────────────────────────
#
# Every one of these carries the partial report, so a caller can see exactly
# how far the run got — in particular whether the source was still untouched.


class WalRecoveryError(Exception):
    """Base for every refusal and failure in this module."""

    code = "WAL_RECOVERY_ERROR"

    def __init__(self, message: str, *, stage: str | None = None, report=None):
        super().__init__(message)
        self.stage = stage
        self.report = report


class SourceRejected(WalRecoveryError):
    """The database path is not something this procedure will open."""

    code = "SOURCE_REJECTED"


class BackupRejected(WalRecoveryError):
    """The backup destination is unusable, or already holds a run."""

    code = "BACKUP_REJECTED"


class ProbeRejected(WalRecoveryError):
    """A table or column name the caller supplied is not a plain identifier."""

    code = "PROBE_REJECTED"


class InsufficientSpace(WalRecoveryError):
    """The measured free space cannot hold this run."""

    code = "INSUFFICIENT_SPACE"


class SnapshotFailed(WalRecoveryError):
    """The backup could not be written, flushed, or proved identical."""

    code = "SNAPSHOT_FAILED"


class CheckpointFailed(WalRecoveryError):
    """The checkpoint did not complete cleanly."""

    code = "CHECKPOINT_FAILED"


class VerificationFailed(WalRecoveryError):
    """A recovered database did not read back as the baseline did."""

    code = "VERIFICATION_FAILED"


class FenceRequired(WalRecoveryError):
    """No usable host fence was supplied, so the source may not be touched.

    Copying and verifying a backup is safe on a running system; folding the
    WAL into the live database is not. Something has to have stopped the
    services and shown that nothing holds the file open, and this procedure
    will not take that on trust.
    """

    code = "FENCE_REQUIRED"


class FenceRefused(WalRecoveryError):
    """The host fence was consulted and would not authorise the checkpoint.

    The reason lives on `__cause__` — the fence raises its own typed refusals
    (an expired lease, a container that came back up, a descriptor that
    reappeared, bytes that no longer match) and those are worth keeping.
    """

    code = "FENCE_REFUSED"


class ApplyRecordRejected(FenceRefused):
    """The authority has no claim record on disk, or it does not bind here.

    A `SourceApplyAuthority` subclass can copy the shape of a real capability
    perfectly — the shape is public. What it cannot copy is the record a
    quiesce leaves behind, so that record is what this module goes and reads
    for itself rather than taking the object's word.
    """

    code = "APPLY_RECORD_REJECTED"


class SourceApplyFailed(WalRecoveryError):
    """Something went wrong after the source had already been written to.

    Whatever it was, it comes back as this rather than as a bare exception:
    once the original database has been modified, the report of what happened
    to it is the most important thing the caller can be handed.
    """

    code = "SOURCE_APPLY_FAILED"


class OracleInvalid(WalRecoveryError):
    """This database does not hold a commit that exists only in its WAL.

    Raised by `assert_wal_only_commit`. It means the fixture cannot be used to
    demonstrate recovery — not that recovery failed. A database whose
    connection was closed normally lands here, because the close checkpointed
    the commit into the main file and there is nothing left to rescue.
    """

    code = "ORACLE_INVALID"

    def __init__(self, message: str, *, reason: str, evidence=None, report=None):
        super().__init__(message, stage="oracle", report=report)
        #: Which precondition was missing, as a stable token. Several of them
        #: are true at once for a normally closed database, so the order the
        #: conditions are tested in is part of the contract, not an accident.
        self.reason = reason
        self.evidence = evidence


# ── the apply record: the provenance a grant has to have ───

APPLY_SCHEMA = "glassops.source-apply/1"
CLAIMED = "CLAIMED"
APPLYING = "APPLYING"
COMPLETED = "COMPLETED"
FAILED = "FAILED"


def _record_digest(body: "dict") -> str:
    payload = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(payload).hexdigest()


def seal_apply_record(body: "dict") -> "dict":
    """Stamp a record with a digest of itself, so a torn one is not a small one."""
    without = {k: v for k, v in body.items() if k != "content_sha256"}
    return {**without, "content_sha256": _record_digest(without)}


def read_apply_record(path, *, expect_states=None) -> "dict":
    """Read a claim record, refusing anything that is not exactly one.

    Deliberately small and deliberately here: this module has to be able to
    check an authority's provenance without knowing the first thing about
    Docker, so the record format is defined by the side that demands it.
    """
    target = os.fspath(path)
    try:
        with open(target, encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError:
        raise ApplyRecordRejected(
            f"there is no claim record at {target!r}", stage="provenance"
        ) from None
    except ValueError as exc:
        raise ApplyRecordRejected(
            f"the claim record at {target!r} is not readable JSON — a torn "
            f"write is not a smaller claim: {exc}",
            stage="provenance",
        ) from exc
    if data.get("schema") != APPLY_SCHEMA:
        raise ApplyRecordRejected(
            f"{target!r} is schema {data.get('schema')!r}, not {APPLY_SCHEMA!r}",
            stage="provenance",
        )
    body = {k: v for k, v in data.items() if k != "content_sha256"}
    if data.get("content_sha256") != _record_digest(body):
        raise ApplyRecordRejected(
            f"{target!r} does not match its own digest; it was truncated or "
            "edited after it was written",
            stage="provenance",
        )
    if expect_states is not None and data.get("state") not in expect_states:
        raise ApplyRecordRejected(
            f"{target!r} is in state {data.get('state')!r}, and this step needs "
            f"one of {sorted(expect_states)}",
            stage="provenance",
        )
    return data


# ── the authority a source checkpoint requires ───────


@dataclass(frozen=True)
class SourceApplyGrant:
    """A specific, checked permission to fold one database's WAL in.

    Not a yes. Every field is something the recovery re-checks for itself
    against the file it is about to open, so a grant that was true when it was
    issued and false a moment later cannot get past the comparison.
    """

    claim_id: str
    lease_id: str
    ok: bool
    db_path: str
    wal_path: str
    db_inode: int | None
    db_device: int | None
    db_sha256: str | None
    wal_inode: int | None
    wal_device: int | None
    wal_sha256: str | None
    #: Where the issuing fence wrote the claim this grant belongs to, and the
    #: manifest that claim was taken against. The recovery reads both.
    claim_record_path: str = ""
    manifest_path: str = ""


class SourceApplyAuthority(abc.ABC):
    """Whatever quiesced the host, expressed as the only thing we will accept.

    The abstract type lives HERE, in the module that demands it, so that
    recovery keeps depending on nothing but the standard library while the
    thing that stops containers lives somewhere else entirely. It is an
    abstract base class rather than a shape check because "has a method called
    `check_before_source_open`" is a bar any object can clear by accident, and
    the thing on the other side of this call is a live database.
    """

    @abc.abstractmethod
    def check_before_source_open(self, *, db_path: str) -> SourceApplyGrant:
        """Re-prove the fence right now, or raise. Never a qualified yes."""

    @abc.abstractmethod
    def begin_apply(self) -> str:
        """Move the claim from CLAIMED to APPLYING, atomically and once.

        Called after the grant has been checked and before anything opens the
        source. Exactly one caller may succeed; every other attempt — a second
        pass, a concurrent one, a release trying to cut in — has to be refused
        from here on, because from here on the original is being written to.
        """

    @abc.abstractmethod
    def complete(self) -> None:
        """The recovery finished; this authority must never authorise again."""

    @abc.abstractmethod
    def fail(self, reason: str) -> None:
        """The recovery did not finish; spend the authority anyway."""


# ── what the caller asks us to look at ───────────────


@dataclass(frozen=True)
class Sentinel:
    """One row that must survive the recovery, addressed by an exact value."""

    table: str
    column: str
    value: object


@dataclass(frozen=True)
class Probe:
    """A representative table whose shape is compared before and after."""

    table: str
    id_column: str = "id"
    timestamp_column: str | None = None


# ── what we measure ──────────────────────────────────


@dataclass(frozen=True)
class FileFacts:
    path: str
    exists: bool
    size_bytes: int
    inode: int | None
    device: int | None
    sha256: str | None


@dataclass(frozen=True)
class TableFacts:
    row_count: int
    max_id: object | None
    max_timestamp: object | None


@dataclass(frozen=True)
class Readback:
    integrity: str
    sentinel_rows: int
    tables: Mapping[str, TableFacts]

    def matches(self, other: "Readback") -> bool:
        """True when the same rows are visible, integrity aside.

        Integrity is checked on its own: a database can be structurally sound
        and still be missing the commit we came to rescue, and the two failures
        need to be told apart.
        """
        return (self.sentinel_rows, dict(self.tables)) == (
            other.sentinel_rows,
            dict(other.tables),
        )


@dataclass(frozen=True)
class SpacePlan:
    """Every byte this run needs, named and derived from a measured file size.

    There are no multipliers and no percentages here. `wal_bytes` is used as
    the bound on how far the database file can grow during a checkpoint
    because that is arithmetic, not a guess: a checkpoint copies WAL frames
    into the database, and the frames — page payload plus their headers — are
    exactly what the WAL file already occupies.
    """

    db_bytes: int
    wal_bytes: int
    source_device: int
    backup_device: int
    same_filesystem: bool
    source_free_bytes: int
    backup_free_bytes: int
    items: Mapping[str, int]
    source_required_bytes: int
    backup_required_bytes: int
    pooled_required_bytes: int
    sufficient: bool
    shortfall_bytes: int


@dataclass(frozen=True)
class CheckpointResult:
    """The full `PRAGMA wal_checkpoint` row plus the file sizes around it.

    All three returned columns are kept. `busy` alone is not the verdict, and
    neither is the WAL disappearing: a TRUNCATE that completes reports
    `(0, 0, 0)` because it reports the state of the WAL it just reset, so the
    sizes recorded here — not the tuple — are what show a WAL was present and
    what became of it.
    """

    busy: int
    log: int
    checkpointed: int
    db_bytes_before: int
    wal_bytes_before: int
    wal_present_before: bool
    db_bytes_after: int
    wal_bytes_after: int
    wal_present_after: bool

    @property
    def ok(self) -> bool:
        return self.busy == 0 and self.checkpointed == self.log


@dataclass(frozen=True)
class WalOnlyEvidence:
    """The measurement that decides whether a WAL-only commit exists."""

    db: FileFacts
    wal: FileFacts
    db_only_sentinel_rows: int
    db_only_row_count: int
    db_plus_wal_sentinel_rows: int
    db_plus_wal_row_count: int


@dataclass
class RecoveryReport:
    """Everything the run observed, filled in stage by stage.

    Handed back on success and attached to every failure, because "how far did
    it get" is the first question anyone asks after a recovery stops halfway.
    """

    db_path: str
    backup_root: str
    run_id: str
    run_dir: str | None = None
    stages: list[str] = field(default_factory=list)
    space: SpacePlan | None = None
    source_before_db: FileFacts | None = None
    source_before_wal: FileFacts | None = None
    wal_only_evidence: WalOnlyEvidence | None = None
    snapshot_db: FileFacts | None = None
    snapshot_wal: FileFacts | None = None
    restore_db: FileFacts | None = None
    restore_wal: FileFacts | None = None
    baseline: Readback | None = None
    restore_checkpoint: CheckpointResult | None = None
    restore_readback: Readback | None = None
    source_checkpoint: CheckpointResult | None = None
    source_readback: Readback | None = None
    fence_check: object | None = None
    apply_id: str | None = None
    claim_record: dict | None = None
    opened_identity: dict | None = None
    claim_outcome: str | None = None
    claim_spend_error: str | None = None
    source_after_db: FileFacts | None = None
    source_after_wal: FileFacts | None = None
    source_revalidated_db: FileFacts | None = None
    source_revalidated_wal: FileFacts | None = None
    #: True once a checkpoint has been attempted against the SOURCE database.
    #: Set before the connection is opened, not after the pragma returns: an
    #: open can itself checkpoint on close, so from that point on "the source
    #: is untouched" can no longer be claimed.
    checkpoint_started: bool = False


# ── seams: the few places the outside world can fail ───


def _copy_file(src: str, dst: str) -> None:
    """Copy bytes into a file that must not already exist.

    `xb` rather than `wb`: refusing to open an existing destination is what
    keeps a rerun from writing over a backup that is already there.
    """
    with open(src, "rb") as reader, open(dst, "xb") as writer:
        shutil.copyfileobj(reader, writer, _CHUNK)


def _fsync_file(path: str) -> None:
    handle = os.open(path, os.O_RDONLY)
    try:
        os.fsync(handle)
    finally:
        os.close(handle)


def _fsync_dir(path: str) -> None:
    """Flush the directory entry itself, so the copy survives a power loss.

    A file's contents reaching the platter does not mean its NAME has; without
    this, a backup can be durable and unreachable at the same time.
    """
    handle = os.open(path, os.O_RDONLY)
    try:
        os.fsync(handle)
    finally:
        os.close(handle)


def _free_bytes(path: str) -> int:
    stats = os.statvfs(path)
    return stats.f_bavail * stats.f_frsize


def _checkpoint_pragma(conn: sqlite3.Connection) -> tuple:
    return conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()


# ── file facts ───────────────────────────────────────


def wal_path(db_path: str | os.PathLike[str]) -> str:
    """The write-ahead log SQLite pairs with this database file."""
    return f"{os.fspath(db_path)}-wal"


def _sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def file_facts(path: str | os.PathLike[str], *, digest: bool = True) -> FileFacts:
    """Identity and content of one file, or a recorded absence."""
    target = os.fspath(path)
    try:
        info = os.lstat(target)
    except FileNotFoundError:
        return FileFacts(target, False, 0, None, None, None)
    return FileFacts(
        path=target,
        exists=True,
        size_bytes=info.st_size,
        inode=info.st_ino,
        device=info.st_dev,
        sha256=_sha256(target) if digest else None,
    )


# ── validation ───────────────────────────────────────


def _validate_source(db_path: str | os.PathLike[str]) -> str:
    """Return the database path, or refuse to go anywhere near it.

    Every rejection here happens before SQLite is handed anything, which is the
    point: `sqlite3.connect` on a path that does not exist CREATES an empty
    database, and a symlink would send a checkpoint into a file the operator
    never named.
    """
    target = os.fspath(db_path)
    if not os.path.isabs(target):
        raise SourceRejected(
            f"database path must be absolute, got {target!r}", stage="validate"
        )
    try:
        info = os.lstat(target)
    except FileNotFoundError:
        raise SourceRejected(
            f"database does not exist: {target!r} — refusing to let SQLite "
            "create one",
            stage="validate",
        ) from None
    except OSError as exc:
        raise SourceRejected(
            f"database path is unreadable: {target!r} ({exc})", stage="validate"
        ) from exc
    if stat.S_ISLNK(info.st_mode):
        raise SourceRejected(
            f"database path is a symlink: {target!r}", stage="validate"
        )
    if not stat.S_ISREG(info.st_mode):
        raise SourceRejected(
            f"database path is not a regular file: {target!r}", stage="validate"
        )

    wal = wal_path(target)
    if os.path.islink(wal):
        raise SourceRejected(f"WAL path is a symlink: {wal!r}", stage="validate")
    if os.path.exists(wal) and not os.path.isfile(wal):
        raise SourceRejected(
            f"WAL path is not a regular file: {wal!r}", stage="validate"
        )
    return target


def _validate_backup_root(backup_root: str | os.PathLike[str]) -> str:
    target = os.fspath(backup_root)
    if not os.path.isabs(target):
        raise BackupRejected(
            f"backup root must be absolute, got {target!r}", stage="validate"
        )
    if os.path.islink(target):
        raise BackupRejected(
            f"backup root is a symlink: {target!r}", stage="validate"
        )
    if not os.path.isdir(target):
        raise BackupRejected(
            f"backup root is not an existing directory: {target!r}", stage="validate"
        )
    return target


def _validate_run_id(run_id: str) -> str:
    if not _RUN_ID.match(run_id or ""):
        raise BackupRejected(
            f"run id must be a plain name, got {run_id!r}", stage="validate"
        )
    return run_id


def _identifier(name: str, *, what: str) -> str:
    """Accept only names that can be quoted into SQL without ambiguity.

    Table and column names cannot be bound as parameters, so they end up in the
    statement text. Restricting them to bare identifiers is what keeps that
    from being an injection point.
    """
    if not isinstance(name, str) or not _IDENTIFIER.match(name):
        raise ProbeRejected(f"{what} is not a plain identifier: {name!r}")
    return name


def _validate_query_spec(sentinel: Sentinel, probes: Sequence[Probe]) -> None:
    _identifier(sentinel.table, what="sentinel table")
    _identifier(sentinel.column, what="sentinel column")
    if not probes:
        raise ProbeRejected("at least one probe table is required")
    for probe in probes:
        _identifier(probe.table, what="probe table")
        _identifier(probe.id_column, what="probe id column")
        if probe.timestamp_column is not None:
            _identifier(probe.timestamp_column, what="probe timestamp column")


# ── space ────────────────────────────────────────────


def plan_space(
    *, db_path: str | os.PathLike[str], backup_root: str | os.PathLike[str]
) -> SpacePlan:
    """Measure what this run needs and what is actually free.

    The itemisation is the point. Each entry is one measured file size, so a
    reader can check the arithmetic against `ls -l` rather than trust a
    headroom factor someone once picked.
    """
    db = _validate_source(db_path)
    root = _validate_backup_root(backup_root)

    db_bytes = os.path.getsize(db)
    wal = wal_path(db)
    wal_bytes = os.path.getsize(wal) if os.path.exists(wal) else 0

    items = {
        # the backup itself: database and WAL travel together or not at all
        "snapshot_db": db_bytes,
        "snapshot_wal": wal_bytes,
        # the disposable copy the backup is proved on
        "restore_db": db_bytes,
        "restore_wal": wal_bytes,
        # checkpointing that copy moves every WAL frame into its database file
        "restore_checkpoint_db_growth": wal_bytes,
        # and the same again when the source is finally checkpointed
        "source_checkpoint_db_growth": wal_bytes,
    }
    source_required = items["source_checkpoint_db_growth"]
    backup_required = sum(
        value for key, value in items.items() if key != "source_checkpoint_db_growth"
    )
    pooled_required = source_required + backup_required

    source_dir = os.path.dirname(db) or "."
    source_device = os.stat(source_dir).st_dev
    backup_device = os.stat(root).st_dev
    same_filesystem = source_device == backup_device
    source_free = _free_bytes(source_dir)
    backup_free = _free_bytes(root)

    if same_filesystem:
        # One pool of free space cannot be spent twice; counting the two
        # requirements separately against the same filesystem would approve a
        # run that runs out halfway through.
        shortfall = max(0, pooled_required - source_free)
    else:
        shortfall = max(0, source_required - source_free) + max(
            0, backup_required - backup_free
        )

    return SpacePlan(
        db_bytes=db_bytes,
        wal_bytes=wal_bytes,
        source_device=source_device,
        backup_device=backup_device,
        same_filesystem=same_filesystem,
        source_free_bytes=source_free,
        backup_free_bytes=backup_free,
        items=items,
        source_required_bytes=source_required,
        backup_required_bytes=backup_required,
        pooled_required_bytes=pooled_required,
        sufficient=shortfall == 0,
        shortfall_bytes=shortfall,
    )


# ── reading a database ───────────────────────────────


def _connect_rw(path: str, busy_timeout_ms: int) -> sqlite3.Connection:
    """Open an existing database, and never bring one into being.

    `mode=rw` is the whole reason for the URI: the default open flags create a
    missing file, and a recovery run that quietly invents an empty database is
    the worst possible outcome — it looks like a success.
    """
    uri = f"{Path(path).as_uri()}?mode=rw"
    conn = sqlite3.connect(
        uri, uri=True, isolation_level=None, timeout=busy_timeout_ms / 1000
    )
    conn.execute(f"PRAGMA busy_timeout={int(busy_timeout_ms)}")
    # Connection-local, stored nowhere: it only stops SQLite from choosing its
    # own moment to checkpoint while we are measuring.
    conn.execute("PRAGMA wal_autocheckpoint=0")
    return conn


def _connect_immutable_db_only(path: str) -> sqlite3.Connection:
    """Read only the main database bytes, ignoring adjacent WAL state.

    The file is a verified snapshot, not the live source. `immutable=1` keeps
    this proof from creating a WAL/SHM or checkpointing the snapshot while the
    oracle asks whether the sentinel was already present in the main file.
    """
    uri = f"{Path(path).as_uri()}?mode=ro&immutable=1"
    return sqlite3.connect(uri, uri=True, isolation_level=None)


def _require_table(conn: sqlite3.Connection, table: str) -> None:
    found = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
    ).fetchone()
    if found is None:
        raise VerificationFailed(f"table {table!r} does not exist in this database")


def _require_column(conn: sqlite3.Connection, table: str, column: str) -> None:
    found = conn.execute(
        "SELECT name FROM pragma_table_info(?) WHERE name = ?", (table, column)
    ).fetchone()
    if found is None:
        raise VerificationFailed(f"table {table!r} has no column {column!r}")


def _read_back(
    conn: sqlite3.Connection, sentinel: Sentinel, probes: Sequence[Probe]
) -> Readback:
    integrity = "; ".join(
        str(row[0]) for row in conn.execute("PRAGMA integrity_check").fetchall()
    )

    _require_table(conn, sentinel.table)
    _require_column(conn, sentinel.table, sentinel.column)
    sentinel_rows = conn.execute(
        f'SELECT count(*) FROM "{sentinel.table}" WHERE "{sentinel.column}" = ?',
        (sentinel.value,),
    ).fetchone()[0]

    tables: dict[str, TableFacts] = {}
    for probe in probes:
        _require_table(conn, probe.table)
        _require_column(conn, probe.table, probe.id_column)
        row_count = conn.execute(f'SELECT count(*) FROM "{probe.table}"').fetchone()[0]
        max_id = conn.execute(
            f'SELECT max("{probe.id_column}") FROM "{probe.table}"'
        ).fetchone()[0]
        max_timestamp = None
        if probe.timestamp_column is not None:
            _require_column(conn, probe.table, probe.timestamp_column)
            max_timestamp = conn.execute(
                f'SELECT max("{probe.timestamp_column}") FROM "{probe.table}"'
            ).fetchone()[0]
        tables[probe.table] = TableFacts(row_count, max_id, max_timestamp)

    return Readback(integrity=integrity, sentinel_rows=sentinel_rows, tables=tables)


def _checkpoint(conn: sqlite3.Connection, path: str) -> CheckpointResult:
    wal = wal_path(path)
    db_before = os.path.getsize(path)
    wal_before_present = os.path.exists(wal)
    wal_before = os.path.getsize(wal) if wal_before_present else 0

    row = _checkpoint_pragma(conn)
    if row is None or len(row) != 3:
        raise CheckpointFailed(
            f"PRAGMA wal_checkpoint returned {row!r}, expected three columns",
            stage="checkpoint",
        )
    busy, log, checkpointed = (int(value) for value in row)

    wal_after_present = os.path.exists(wal)
    return CheckpointResult(
        busy=busy,
        log=log,
        checkpointed=checkpointed,
        db_bytes_before=db_before,
        wal_bytes_before=wal_before,
        wal_present_before=wal_before_present,
        db_bytes_after=os.path.getsize(path),
        wal_bytes_after=os.path.getsize(wal) if wal_after_present else 0,
        wal_present_after=wal_after_present,
    )


# ── the oracle ───────────────────────────────────────


def assert_wal_only_commit(
    *,
    db_path: str | os.PathLike[str],
    sentinel: Sentinel,
    workspace: str | os.PathLike[str],
) -> WalOnlyEvidence:
    """Prove this database holds a commit that exists only in its WAL.

    Two copies are made into `workspace` — one of the database alone, one of
    the database with its WAL — and the sentinel is counted in each. A genuine
    post-crash database shows the commit in the second and not the first; that
    gap is the only thing that makes a later "recovery succeeded" mean
    anything.

    The source is opened by nothing here: both counts come off copies, so the
    measurement cannot itself checkpoint away the state it is measuring.

    Raises `OracleInvalid` — with the measurement attached — when the gap is
    not there.
    """
    db = _validate_source(db_path)
    _identifier(sentinel.table, what="sentinel table")
    _identifier(sentinel.column, what="sentinel column")

    root = os.fspath(workspace)
    if not os.path.isabs(root):
        raise BackupRejected(f"oracle workspace must be absolute, got {root!r}")
    try:
        os.makedirs(root, mode=_DIR_MODE)
    except FileExistsError:
        raise BackupRejected(
            f"oracle workspace already exists: {root!r}", stage="oracle"
        ) from None

    db_facts = file_facts(db)
    wal_facts = file_facts(wal_path(db))
    name = os.path.basename(db)

    db_only_dir = os.path.join(root, "db-only")
    both_dir = os.path.join(root, "db-and-wal")
    os.mkdir(db_only_dir, _DIR_MODE)
    os.mkdir(both_dir, _DIR_MODE)

    db_only = os.path.join(db_only_dir, name)
    both = os.path.join(both_dir, name)
    _copy_file(db, db_only)
    _copy_file(db, both)
    if wal_facts.exists:
        _copy_file(wal_path(db), wal_path(both))

    db_only_sentinel, db_only_rows = _count(db_only, sentinel)
    both_sentinel, both_rows = _count(both, sentinel)

    evidence = WalOnlyEvidence(
        db=db_facts,
        wal=wal_facts,
        db_only_sentinel_rows=db_only_sentinel,
        db_only_row_count=db_only_rows,
        db_plus_wal_sentinel_rows=both_sentinel,
        db_plus_wal_row_count=both_rows,
    )

    return _require_wal_only(evidence)


def _require_wal_only(
    evidence: WalOnlyEvidence, *, report: RecoveryReport | None = None
) -> WalOnlyEvidence:
    """Accept only evidence that the requested commit lives in the WAL alone."""

    # Counted first, judged second, so the evidence is complete no matter which
    # condition is the one that fails.
    if not evidence.wal.exists or evidence.wal.size_bytes == 0:
        raise OracleInvalid(
            f"there is no WAL to recover from: {evidence.wal.path!r} is "
            f"{'absent' if not evidence.wal.exists else 'empty'}",
            reason="no-wal",
            evidence=evidence,
            report=report,
        )
    if evidence.db_only_sentinel_rows != 0:
        raise OracleInvalid(
            f"the sentinel is already in the database file "
            f"({evidence.db_only_sentinel_rows} row(s)) — this commit was "
            "checkpointed, so "
            "nothing here is WAL-only",
            reason="already-checkpointed",
            evidence=evidence,
            report=report,
        )
    if evidence.db_plus_wal_sentinel_rows < 1:
        raise OracleInvalid(
            "the sentinel is in neither the database nor the WAL — this "
            "commit was never durable",
            reason="never-durable",
            evidence=evidence,
            report=report,
        )
    if evidence.db_plus_wal_row_count <= evidence.db_only_row_count:
        raise OracleInvalid(
            f"the WAL adds no rows ({evidence.db_only_row_count} -> "
            f"{evidence.db_plus_wal_row_count})",
            reason="wal-adds-nothing",
            evidence=evidence,
            report=report,
        )
    return evidence


def _count(path: str, sentinel: Sentinel) -> tuple[int, int]:
    conn = _connect_rw(path, busy_timeout_ms=1000)
    try:
        matched, total = _count_on_connection(conn, sentinel)
    finally:
        conn.close()
    return matched, total


def _count_on_connection(
    conn: sqlite3.Connection, sentinel: Sentinel
) -> tuple[int, int]:
    _require_table(conn, sentinel.table)
    _require_column(conn, sentinel.table, sentinel.column)
    matched = conn.execute(
        f'SELECT count(*) FROM "{sentinel.table}" WHERE "{sentinel.column}" = ?',
        (sentinel.value,),
    ).fetchone()[0]
    total = conn.execute(f'SELECT count(*) FROM "{sentinel.table}"').fetchone()[0]
    return matched, total


# ── the procedure ────────────────────────────────────


def rehearse_wal_recovery(
    *,
    db_path: str | os.PathLike[str],
    backup_root: str | os.PathLike[str],
    run_id: str,
    sentinel: Sentinel,
    probes: Sequence[Probe],
    fence: object,
    busy_timeout_ms: int = 5000,
) -> RecoveryReport:
    """Back a crashed database up, prove the backup, then fold in its WAL.

    `fence` is the host-side quiesce this procedure will not act without. It
    is duck-typed on purpose — one method, `check_before_source_open(db_path=)`
    — so that recovery keeps depending on nothing but the standard library
    while the thing that stops containers stays somewhere else entirely.

    Returns the full `RecoveryReport` on success. On any failure it raises a
    `WalRecoveryError` carrying that same report, and leaves both the source
    and everything already written under the run directory exactly as they
    are — this never tidies up after itself, and never restores a backup on
    its own initiative.
    """
    # The fence FIRST, and on its own. Everything below this line ends the
    # claim on the way out, so the thing being ended has to be a capability
    # something actually issued before anything else is allowed to fail:
    # calling `fail()` on an object this procedure has already refused is not
    # bookkeeping, it is talking to a stranger. Shape-checked here and
    # consulted much later — a run with no fence should cost nothing, not a
    # full backup followed by a refusal.
    _validate_fence(fence)

    # Built from the values as they were HANDED IN, before any of them have
    # been checked, so that a refusal over one of them still travels with an
    # account of the run it refused.
    report = RecoveryReport(
        db_path=_as_text(db_path),
        backup_root=_as_text(backup_root),
        run_id=_as_text(run_id),
    )
    try:
        # Inside the handler, not in front of it. A run refused for its run id
        # has already been handed a live, one-shot capability over a host
        # whose containers are stopped; walking away without closing it out
        # leaves a fence nobody can release.
        db = _validate_source(db_path)
        root = _validate_backup_root(backup_root)
        _validate_run_id(run_id)
        _validate_query_spec(sentinel, probes)
        report.db_path = db
        report.backup_root = root
        return _run_rehearsal(
            report, db, root, run_id, sentinel, probes, fence, busy_timeout_ms
        )
    except BaseException as exc:
        # ANY failure ends the claim, not only one that got as far as the
        # source. A claim left live after a run that will never resume is a
        # fence nobody can release: the containers stay down and the restart
        # policies stay pinned to `no` until somebody works out by hand what
        # happened. Ending it here is what makes the ordinary release work.
        _spend_claim(report, fence, reason=_failure_reason(exc))
        # The original exception is re-raised as it is — same object, same
        # type, same stage, same traceback — carrying the account of how far
        # the run got.
        _attach_report(exc, report)
        raise


def _attach_report(exc: BaseException, report: "RecoveryReport") -> None:
    """Write the account of the run onto the exception carrying it out.

    A `WalRecoveryError` keeps its own `.report` — and one raised from deeper
    inside already holds the more specific one, so only a missing report is
    filled in. Everything else, from a `sqlite3.ProgrammingError` to a
    cancellation, gets it under `.recovery_report`, so that nothing reading
    `.report` mistakes a driver error for a refusal this module raised.

    All of it is bookkeeping, and bookkeeping is never the finding: an
    exception object can refuse an attribute outright, and a ^C can land in
    here as easily as anywhere else. Either of those replacing the failure
    that actually happened would lose the only thing the operator needs, so
    nothing raised in here escapes.
    """
    try:
        if isinstance(exc, WalRecoveryError):
            if exc.report is None:
                exc.report = report
        else:
            exc.recovery_report = report
    except BaseException:  # noqa: BLE001 - never the finding
        pass


def _as_text(value) -> str:
    """Whatever was handed in, as something a report can carry."""
    try:
        return os.fspath(value)
    except TypeError:
        return str(value)


def _failure_reason(exc: BaseException) -> str:
    if isinstance(exc, WalRecoveryError):
        return str(exc)
    return f"{type(exc).__name__}: {exc}"


def _run_rehearsal(
    report: RecoveryReport,
    db: str,
    root: str,
    run_id: str,
    sentinel: Sentinel,
    probes: Sequence[Probe],
    fence: SourceApplyAuthority,
    busy_timeout_ms: int,
) -> RecoveryReport:
    """The run itself. Its caller is what guarantees the claim is closed out."""
    run_dir = os.path.join(root, run_id)
    report.stages.append("validate")

    # A. what we are about to copy, and what it is
    report.source_before_db = file_facts(db)
    report.source_before_wal = file_facts(wal_path(db))
    report.stages.append("source-facts")

    # Space is settled before a single byte moves: a copy that dies on a full
    # filesystem leaves a backup that looks like one and is not.
    report.space = plan_space(db_path=db, backup_root=root)
    if not report.space.sufficient:
        raise InsufficientSpace(
            f"{report.space.shortfall_bytes} byte(s) short: needs "
            f"{report.space.backup_required_bytes} for the backup and "
            f"{report.space.source_required_bytes} beside the source, with "
            f"{report.space.backup_free_bytes} and "
            f"{report.space.source_free_bytes} free",
            stage="space-preflight",
            report=report,
        )
    report.stages.append("space-preflight")

    try:
        # The single point where an existing backup is refused, and it is a
        # bare mkdir on purpose: asking whether the directory is free and then
        # creating it are two steps, and a second run can arrive in between.
        # Failing to create it is the only answer that cannot be raced.
        os.mkdir(run_dir, _DIR_MODE)
    except FileExistsError:
        raise BackupRejected(
            f"run directory already exists: {run_dir!r} — pick a new run id "
            "rather than writing over an earlier backup",
            stage="run-dir",
            report=report,
        ) from None
    except OSError as exc:
        raise BackupRejected(
            f"could not create run directory {run_dir!r}: {exc}",
            stage="run-dir",
            report=report,
        ) from exc
    report.run_dir = run_dir
    report.stages.append("run-dir")
    try:
        # `run_dir` itself is only durable once its entry in `backup_root` is.
        # Source mutation must never begin while a power loss can still make
        # the verified backup disappear by name.
        _fsync_dir(root)
    except OSError as exc:
        raise SnapshotFailed(
            f"could not make run directory {run_dir!r} durable: {exc}",
            stage="run-dir-fsync",
            report=report,
        ) from exc
    report.stages.append("run-dir-fsync")

    name = os.path.basename(db)
    snapshot_dir = os.path.join(run_dir, "snapshot")
    snapshot_db = os.path.join(snapshot_dir, name)
    _snapshot(report, db, snapshot_dir, snapshot_db, stage="snapshot-copy")
    report.snapshot_db = file_facts(snapshot_db)
    report.snapshot_wal = file_facts(wal_path(snapshot_db))

    # D. the copy is the source, byte for byte, or it is not a backup
    if report.snapshot_db.sha256 != report.source_before_db.sha256:
        raise SnapshotFailed(
            f"snapshot of {name!r} does not match the source: "
            f"{report.snapshot_db.sha256} != {report.source_before_db.sha256}",
            stage="snapshot-verify",
            report=report,
        )
    if report.snapshot_wal.sha256 != report.source_before_wal.sha256:
        raise SnapshotFailed(
            "snapshot of the WAL does not match the source: "
            f"{report.snapshot_wal.sha256} != {report.source_before_wal.sha256}",
            stage="snapshot-verify",
            report=report,
        )
    # E. and the exact source identity is still what it was when we hashed it
    _require_source_unchanged(report, stage="snapshot-verify")
    report.stages.append("snapshot-verify")

    # The snapshot is never opened read-write. The oracle reads only its main
    # database bytes through an immutable connection; all WAL-inclusive work
    # happens on the second, disposable copy below.
    restore_dir = os.path.join(run_dir, "restore")
    restore_db = os.path.join(restore_dir, name)
    _snapshot(report, snapshot_db, restore_dir, restore_db, stage="restore-copy")
    report.restore_db = file_facts(restore_db)
    report.restore_wal = file_facts(wal_path(restore_db))
    if not _same_file_contents(report.restore_db, report.snapshot_db):
        raise SnapshotFailed(
            "the disposable restore database is not byte-identical to the "
            "verified snapshot",
            stage="restore-copy-verify",
            report=report,
        )
    if not _same_file_contents(report.restore_wal, report.snapshot_wal):
        raise SnapshotFailed(
            "the disposable restore WAL is not byte-identical to the verified "
            "snapshot WAL",
            stage="restore-copy-verify",
            report=report,
        )
    report.stages.append("restore-copy-verify")

    immutable = _connect_immutable_db_only(snapshot_db)
    try:
        db_only_sentinel, db_only_rows = _count_on_connection(immutable, sentinel)
    finally:
        immutable.close()

    # The pre-checkpoint baseline is read HERE, off the copy, and never off the
    # source: closing a connection to a WAL database can checkpoint it, so
    # reading the original first would modify the file the snapshot certifies.
    conn = _connect_rw(restore_db, busy_timeout_ms)
    try:
        both_sentinel, both_rows = _count_on_connection(conn, sentinel)
        report.wal_only_evidence = WalOnlyEvidence(
            db=report.source_before_db,
            wal=report.source_before_wal,
            db_only_sentinel_rows=db_only_sentinel,
            db_only_row_count=db_only_rows,
            db_plus_wal_sentinel_rows=both_sentinel,
            db_plus_wal_row_count=both_rows,
        )
        _require_wal_only(report.wal_only_evidence, report=report)
        report.stages.append("wal-only-oracle")
        report.baseline = _read_back(conn, sentinel, probes)
        report.stages.append("baseline")
        report.restore_checkpoint = _checkpoint(conn, restore_db)
    finally:
        conn.close()
    if not report.restore_checkpoint.ok:
        raise CheckpointFailed(
            "checkpointing the restored copy did not complete: "
            f"busy={report.restore_checkpoint.busy} "
            f"log={report.restore_checkpoint.log} "
            f"checkpointed={report.restore_checkpoint.checkpointed}",
            stage="restore-checkpoint",
            report=report,
        )
    report.stages.append("restore-checkpoint")

    conn = _connect_rw(restore_db, busy_timeout_ms)
    try:
        report.restore_readback = _read_back(conn, sentinel, probes)
    finally:
        conn.close()
    _require_verified(report, report.restore_readback, "restore")
    report.stages.append("restore-verify")

    # Only now, with a verified backup and a copy that has already been through
    # the whole procedure, is the original touched.
    _require_source_unchanged(report, stage="source-revalidate")
    grant = _require_fence(report, fence)
    report.fence_check = grant
    # An authority is only as good as the record behind it, so the record is
    # read here rather than believed. Everything above this line is still
    # reversible; nothing below it is.
    report.claim_record = _require_claim_provenance(report, grant)
    report.stages.append("source-revalidate")

    # CLAIMED -> APPLYING, atomically and exactly once. From here the claim is
    # held, and it is spent exactly once on the way out whichever way this
    # goes: a failed recovery must not leave a live authorisation behind, and
    # a release must not be able to cut in.
    report.apply_id = _begin_apply(report, fence, grant)
    report.stages.append("apply-begin")
    try:
        _apply_to_source(report, db, grant, sentinel, probes, busy_timeout_ms)
    except WalRecoveryError as exc:
        _spend_claim(report, fence, reason=str(exc))
        raise
    except Exception as exc:
        _spend_claim(report, fence, reason=f"{type(exc).__name__}: {exc}")
        raise SourceApplyFailed(
            f"the source apply did not complete: {type(exc).__name__}: {exc}",
            stage="source-apply",
            report=report,
        ) from exc
    except BaseException as exc:  # cancellation, interrupt: spend, keep the report
        _spend_claim(report, fence, reason=f"{type(exc).__name__}")
        # The original database has already been written to. Whatever is
        # unwinding the stack, the account of what happened to it travels with
        # it rather than being dropped on the way past — and never at the cost
        # of the thing that is unwinding.
        _attach_report(exc, report)
        raise
    _spend_claim(report, fence, reason=None)
    return report


def _require_claim_provenance(report: RecoveryReport, grant: SourceApplyGrant) -> dict:
    """Read the claim this grant says it came from, and check it says the same.

    This is the difference between an authority and an object with the right
    method names. The record was written by whatever quiesced the host, before
    this process was involved; a subclass that fabricates a grant has nothing
    to point at here.
    """
    if not (isinstance(grant.claim_record_path, str) and grant.claim_record_path.strip()):
        raise ApplyRecordRejected(
            "the grant names no claim record, so there is nothing to check it "
            "against. A grant is only worth where it came from.",
            stage="provenance",
            report=report,
        )
    record = _read_claim_record(report, grant, expect_states={CLAIMED})
    for field_name, expected in (
        ("claim_id", grant.claim_id),
        ("lease_id", grant.lease_id),
        ("manifest_path", grant.manifest_path),
        ("db_path", report.db_path),
        ("wal_path", wal_path(report.db_path)),
    ):
        if record.get(field_name) != expected:
            raise ApplyRecordRejected(
                f"the claim record at {grant.claim_record_path!r} says "
                f"{field_name}={record.get(field_name)!r}, and this apply is "
                f"for {expected!r}",
                stage="provenance",
                report=report,
            )
    if not os.path.isfile(str(grant.manifest_path)):
        raise ApplyRecordRejected(
            f"the claim names a manifest at {grant.manifest_path!r} that is not "
            "there; a claim with no quiesce behind it authorises nothing",
            stage="provenance",
            report=report,
        )
    return record


def _read_claim_record(
    report: RecoveryReport, grant: SourceApplyGrant, *, expect_states
) -> dict:
    """Read the claim record, and keep the report with any refusal."""
    try:
        return read_apply_record(grant.claim_record_path, expect_states=expect_states)
    except ApplyRecordRejected as exc:
        raise ApplyRecordRejected(
            str(exc), stage=exc.stage or "provenance", report=report
        ) from exc


def _begin_apply(
    report: RecoveryReport, fence: SourceApplyAuthority, grant: SourceApplyGrant
) -> str:
    """Take the one-shot transition, then check it actually happened on disk."""
    try:
        apply_id = fence.begin_apply()
    except WalRecoveryError:
        raise
    except Exception as exc:
        raise FenceRefused(
            f"the host fence would not begin the apply: {exc}",
            stage="apply-begin",
            report=report,
        ) from exc
    if not (isinstance(apply_id, str) and apply_id.strip()):
        raise FenceRefused(
            f"the host fence returned {apply_id!r} instead of an apply id",
            stage="apply-begin",
            report=report,
        )
    # Said yes is not the same as wrote it down. If the transition is not
    # durable, a second process can still believe the claim is free.
    record = _read_claim_record(report, grant, expect_states={APPLYING})
    if record.get("apply_id") != apply_id:
        raise ApplyRecordRejected(
            f"the claim record records apply {record.get('apply_id')!r}, not "
            f"the {apply_id!r} just issued",
            stage="apply-begin",
            report=report,
        )
    return apply_id


def _apply_to_source(
    report: RecoveryReport,
    db: str,
    grant: SourceApplyGrant,
    sentinel: Sentinel,
    probes: Sequence[Probe],
    busy_timeout_ms: int,
) -> None:
    """Open the source and fold the WAL in, with the file pinned throughout.

    The descriptor opened here is the join between "the fence checked it" and
    "SQLite opened it". Without it those are two statements about a PATH, and a
    path can be pointed at a different file in between — an atomic rename is
    one syscall, and it leaves the bytes looking identical. Holding a
    descriptor means the inode we verified is the inode we are still talking
    about when the checkpoint runs.
    """
    guard = os.open(db, os.O_RDONLY)
    try:
        pinned = os.fstat(guard)
        if (pinned.st_ino, pinned.st_dev) != (grant.db_inode, grant.db_device):
            raise FenceRefused(
                f"the database at {db!r} is inode {pinned.st_ino} on device "
                f"{pinned.st_dev}, and the grant was issued for inode "
                f"{grant.db_inode} on device {grant.db_device}. It was replaced "
                "between the check and the open; the bytes may well match, and "
                "that is not the same file.",
                stage="source-open",
                report=report,
            )
        wal_now = file_facts(wal_path(db), digest=False)
        if (wal_now.inode, wal_now.device) != (grant.wal_inode, grant.wal_device):
            raise FenceRefused(
                f"the WAL at {wal_path(db)!r} is inode {wal_now.inode} on device "
                f"{wal_now.device}, and the grant was issued for inode "
                f"{grant.wal_inode} on device {grant.wal_device}",
                stage="source-open",
                report=report,
            )

        report.checkpoint_started = True
        conn = _connect_rw(db, busy_timeout_ms)
        try:
            reopened = os.stat(db)
            if (reopened.st_ino, reopened.st_dev) != (pinned.st_ino, pinned.st_dev):
                raise FenceRefused(
                    f"{db!r} was replaced while it was being opened: the path "
                    f"now resolves to inode {reopened.st_ino} on device "
                    f"{reopened.st_dev}, not {pinned.st_ino} on {pinned.st_dev}",
                    stage="source-open",
                    report=report,
                )
            report.opened_identity = _opened_identity(report, conn, db, pinned)
            report.stages.append("source-open")
            report.source_checkpoint = _checkpoint(conn, db)
        finally:
            conn.close()
    finally:
        os.close(guard)

    report.source_after_db = file_facts(db)
    report.source_after_wal = file_facts(wal_path(db))
    if not report.source_checkpoint.ok:
        raise CheckpointFailed(
            "checkpointing the source did not complete: "
            f"busy={report.source_checkpoint.busy} "
            f"log={report.source_checkpoint.log} "
            f"checkpointed={report.source_checkpoint.checkpointed}. The source "
            "has already been written to; its recorded sizes and the verified "
            "snapshot are both in this report.",
            stage="source-checkpoint",
            report=report,
        )
    report.stages.append("source-checkpoint")

    conn = _connect_rw(db, busy_timeout_ms)
    try:
        report.source_readback = _read_back(conn, sentinel, probes)
    finally:
        conn.close()
    _require_verified(report, report.source_readback, "source")
    report.stages.append("source-verify")


def _opened_identity(
    report: RecoveryReport, conn: sqlite3.Connection, db: str, pinned
) -> dict:
    """Get SQLite to say which file it opened, and refuse if it cannot.

    SQLite does not expose the descriptor it holds, so the strongest available
    statement is that the connection's own idea of its main database is the
    path we pinned, and that the path still resolves to the inode we pinned.
    That detects a swap across this window; it does not make one impossible.
    Where even that cannot be established, this refuses rather than guessing —
    an unanswerable question about which file is about to be rewritten is not
    one to round off.
    """
    try:
        rows = conn.execute("PRAGMA database_list").fetchall()
    except sqlite3.Error as exc:
        raise FenceRefused(
            f"the connection to {db!r} could not say which file it opened: {exc}",
            stage="source-open",
            report=report,
        ) from exc
    main = next((row for row in rows if row[1] == "main"), None)
    if main is None or not main[2]:
        raise FenceRefused(
            f"the connection to {db!r} reports no main database file, so there "
            "is no way to show it opened the one that was checked",
            stage="source-open",
            report=report,
        )
    main_file = str(main[2])
    if os.path.realpath(main_file) != os.path.realpath(db):
        raise FenceRefused(
            f"the connection opened {main_file!r}, not {db!r}",
            stage="source-open",
            report=report,
        )
    try:
        now = os.stat(main_file)
    except OSError as exc:
        raise FenceRefused(
            f"cannot confirm what {main_file!r} is: {exc}",
            stage="source-open",
            report=report,
        ) from exc
    if (now.st_ino, now.st_dev) != (pinned.st_ino, pinned.st_dev):
        raise FenceRefused(
            f"{main_file!r} is inode {now.st_ino} on device {now.st_dev}, and "
            f"the pinned descriptor is {pinned.st_ino} on {pinned.st_dev}",
            stage="source-open",
            report=report,
        )
    return {
        "main_file": main_file,
        "inode": now.st_ino,
        "device": now.st_dev,
        "pinned_inode": pinned.st_ino,
        "pinned_device": pinned.st_dev,
    }


def _spend_claim(
    report: RecoveryReport, fence: SourceApplyAuthority, *, reason: str | None
) -> None:
    """Close out the authority once, on every path out of the apply.

    On the failure path a problem spending it is recorded rather than raised:
    replacing the reason the recovery failed with the reason the bookkeeping
    failed would lose the thing the operator actually needs.
    """
    if report.claim_outcome is not None or report.claim_spend_error is not None:
        # Already closed out once on this run. Trying again would either be a
        # no-op or would replace the recorded reason with a repeat of itself.
        return
    outcome = "completed" if reason is None else "failed"
    try:
        if reason is None:
            fence.complete()
        else:
            fence.fail(reason)
    except BaseException as exc:
        # BaseException, not Exception: a ^C or a cancellation landing in the
        # middle of the bookkeeping would otherwise unwind straight past the
        # failure that brought us here, and the operator would be told about
        # the interrupt instead of about the recovery.
        report.claim_spend_error = f"{type(exc).__name__}: {exc}"
        if reason is not None:
            # A failure is already on its way out. This one is recorded and
            # goes no further; replacing the reason the recovery failed with
            # the reason the bookkeeping failed loses what is actually needed.
            return
        if isinstance(exc, Exception):
            raise SourceApplyFailed(
                "the recovery finished but its authorisation could not be "
                f"closed out: {exc}",
                stage="claim",
                report=report,
            ) from exc
        # Nothing else went wrong, and this is not ours to swallow.
        _attach_report(exc, report)
        raise
    else:
        report.claim_outcome = outcome


def _validate_fence(fence: object) -> None:
    if fence is None:
        raise FenceRequired(
            "a host fence is required before a source checkpoint: pass the "
            "capability that proves the services are stopped and nothing holds "
            "the database open",
            stage="validate",
        )
    if not isinstance(fence, SourceApplyAuthority):
        raise FenceRequired(
            f"{type(fence).__name__} is not a SourceApplyAuthority. Having a "
            "method with the right name is not the same as being the "
            "capability something issued after quiescing this host.",
            stage="validate",
        )


def _require_fence(report: RecoveryReport, fence: SourceApplyAuthority):
    """Ask the authority at the last possible moment, and check its answer.

    Deliberately after the backup is verified and before anything opens the
    source: everything a fence asserts can stop being true while the backup is
    being taken, and this is the only moment where knowing that still helps.

    The answer is then checked against what THIS process can see. A grant is
    evidence, not permission — if its identity does not match the file we are
    about to open, the mismatch is the finding.
    """
    try:
        grant = fence.check_before_source_open(db_path=report.db_path)
    except WalRecoveryError:
        # Already one of ours, already specific. Wrapping it would bury the
        # reason under a more general one.
        raise
    except Exception as exc:
        raise FenceRefused(
            f"the host fence would not authorise checkpointing "
            f"{report.db_path!r}: {exc}",
            stage="source-revalidate",
            report=report,
        ) from exc
    _validate_grant(report, grant)
    return grant


def _validate_grant(report: RecoveryReport, grant: object) -> None:
    """Refuse a grant that is the wrong shape, empty, or about another file."""

    def refuse(why: str):
        raise FenceRefused(
            f"the host fence returned a grant this recovery will not act on: "
            f"{why}",
            stage="source-revalidate",
            report=report,
        )

    if not isinstance(grant, SourceApplyGrant):
        refuse(f"expected a SourceApplyGrant, got {type(grant).__name__}")
    if grant.ok is not True:
        refuse(f"ok={grant.ok!r}")
    if not (isinstance(grant.claim_id, str) and grant.claim_id.strip()):
        refuse("it carries no claim id, so no claim was ever taken")
    if not (isinstance(grant.lease_id, str) and grant.lease_id.strip()):
        refuse("it carries no lease id, so no fence ever granted it")
    if grant.db_path != report.db_path:
        refuse(f"it is for {grant.db_path!r}, not {report.db_path!r}")
    if grant.wal_path != wal_path(report.db_path):
        refuse(f"its WAL is {grant.wal_path!r}, not {wal_path(report.db_path)!r}")

    db, wal = report.source_revalidated_db, report.source_revalidated_wal
    if (grant.db_inode, grant.db_sha256) != (db.inode, db.sha256):
        refuse(
            f"it describes a database with inode {grant.db_inode} / "
            f"sha256 {grant.db_sha256}, and this one has {db.inode} / "
            f"{db.sha256}"
        )
    if grant.db_device is not None and grant.db_device != db.device:
        refuse(
            f"it describes a database on device {grant.db_device}, and this "
            f"one is on {db.device} — the mount underneath has changed"
        )
    if (grant.wal_inode, grant.wal_sha256) != (wal.inode, wal.sha256):
        refuse(
            f"it describes a WAL with inode {grant.wal_inode} / sha256 "
            f"{grant.wal_sha256}, and this one has {wal.inode} / {wal.sha256}"
        )


def _require_source_unchanged(report: RecoveryReport, *, stage: str) -> None:
    """Recheck path type, identity, size, and content before source mutation."""
    try:
        _validate_source(report.db_path)
        current_db = file_facts(report.db_path)
        current_wal = file_facts(wal_path(report.db_path))
    except (OSError, SourceRejected) as exc:
        raise SnapshotFailed(
            f"could not revalidate the source before {stage}: {exc}",
            stage=stage,
            report=report,
        ) from exc

    if stage == "source-revalidate":
        report.source_revalidated_db = current_db
        report.source_revalidated_wal = current_wal

    if (current_db, current_wal) != (
        report.source_before_db,
        report.source_before_wal,
    ):
        raise SnapshotFailed(
            "the source database or WAL changed after it was recorded; refusing "
            "to checkpoint a source that no longer matches the verified backup",
            stage=stage,
            report=report,
        )


def _same_file_contents(left: FileFacts, right: FileFacts) -> bool:
    """Compare copy contents without requiring copies to share an inode."""
    return (
        left.exists,
        left.size_bytes,
        left.sha256,
    ) == (
        right.exists,
        right.size_bytes,
        right.sha256,
    )


def _snapshot(
    report: RecoveryReport, src_db: str, dest_dir: str, dest_db: str, *, stage: str
) -> None:
    """Copy a database and its WAL together, and make both durable."""
    src_wal = wal_path(src_db)
    try:
        os.mkdir(dest_dir, _DIR_MODE)
        _copy_file(src_db, dest_db)
        _fsync_file(dest_db)
        if os.path.exists(src_wal):
            _copy_file(src_wal, wal_path(dest_db))
            _fsync_file(wal_path(dest_db))
        _fsync_dir(dest_dir)
        _fsync_dir(os.path.dirname(dest_dir))
    except OSError as exc:
        raise SnapshotFailed(
            f"could not write {stage} into {dest_dir!r}: {exc}",
            stage=stage,
            report=report,
        ) from exc
    report.stages.append(stage)


def _require_verified(report: RecoveryReport, readback: Readback, which: str) -> None:
    if readback.integrity != "ok":
        raise VerificationFailed(
            f"{which} failed its integrity check: {readback.integrity}",
            stage=f"{which}-verify",
            report=report,
        )
    if readback.sentinel_rows < 1:
        raise VerificationFailed(
            f"{which} lost the sentinel row entirely",
            stage=f"{which}-verify",
            report=report,
        )
    if not readback.matches(report.baseline):
        raise VerificationFailed(
            f"{which} does not read back as it did before the checkpoint: "
            f"{readback} != {report.baseline}",
            stage=f"{which}-verify",
            report=report,
        )
