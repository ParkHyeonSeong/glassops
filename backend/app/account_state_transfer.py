"""Carry GlassOps account state from an old database into a freshly initialised one.

Monitoring history is abandoned on purpose; the accounts, their host mappings,
the alert configuration, revoked tokens, the audit trail and the exact bytes
of `secret.key` are not. This module moves exactly that, in two steps that run
on the host with the backend stopped:

    export   old DB (+ its WAL) ──SQLite read-only snapshot──▶ bundle directory
    restore  bundle directory ──one write transaction──▶ the NEW build's fresh DB

What travels: ``users``, ``user_host_accounts``, ``alert_config``,
``token_blacklist``, ``audit_log`` (rows and ids as they are, plus the
``audit_log`` AUTOINCREMENT high-water so a pruned id is never reissued), and
``secret.key`` byte for byte. What stays behind: every metrics, downsample,
aggregation and network-audit table, the old ``glassops.db`` / ``-wal`` /
``-shm`` files, and ``.env`` (host-side configuration, never in the bundle).

Safety properties, each one measured by ``tests/backend/test_account_state_transfer.py``:

* The source is read through SQLite with ``mode=ro`` inside ONE read
  transaction, so rows that exist only in the WAL are seen and nothing is
  checkpointed, rewritten or deleted — the main file and the WAL are
  byte-identical afterwards.
* The source schema must be the one both the 817e373 build and this tree
  create; ``runtime_config`` must be empty (a populated one is a decision for
  the operator, not for this tool).
* The bundle is a canonical JSON manifest (deterministic for the same logical
  content), the key file, and a completion marker written LAST that pins both
  digests. Every file is written tmp → fsync → rename → fsync(dir), files are
  0600 and directories 0700, and an existing bundle/run path — or a symlink
  anywhere a path is resolved — is refused rather than reused.
* The restore target must be exactly what ``init_db()`` leaves behind: the
  bootstrap admin alone in ``users``, the cutover row alone in
  ``runtime_config``, everything else empty. The bootstrap admin is deleted
  explicitly before the bundle's users are applied (it may share an email
  with one of them), and its plaintext ``initial_admin_password`` file goes
  with it.
* After applying, ``PRAGMA foreign_key_check``, row counts, per-table digests
  and the emptiness of every monitoring table are checked INSIDE the write
  transaction, and ``secret.key`` is installed last, just before COMMIT.
  Any failure before COMMIT rolls the rows back and the target is re-checked
  to be fresh; the target's ``secret.key`` may by then already hold the
  bundle key, which a re-run with the same bundle and a NEW run directory
  accepts as already in place — after fsyncing that file and its directory
  entry again, because identical bytes being visible does not prove the
  earlier rename was made durable. Nothing is written to the run directory
  in the failed case, and the error says so (``RETRY_GUIDANCE``). If the rollback or
  the re-check cannot confirm the fresh state, the failure is reported as
  ``MANUAL_VERIFICATION_REQUIRED`` and no retry safety is claimed.
* The target container must not be started until the run directory holds
  ``COMPLETE``: only that marker says the database rows and the key are the
  bundle's together.

The digests guard against corruption and accidental edits. They are not a
signature: a bundle is trusted because of where it came from and how it was
kept (0700/0600), not because of anything this module can prove about it.
Nothing here prints, logs or embeds in an error message a row value or a key
byte — table names and counts only.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sqlite3
import stat
import sys
import urllib.parse
from dataclasses import asdict, dataclass
from pathlib import Path

FORMAT = "glassops-account-state/1"
MANIFEST_NAME = "manifest.json"
SECRET_KEY_NAME = "secret.key"
COMPLETE_MARKER = "COMPLETE"
REPORT_NAME = "report.json"
INITIAL_ADMIN_PASSWORD_FILE = "initial_admin_password"

#: The tables that travel, in apply order (parents before children:
#: ``user_host_accounts`` references ``users``).
PRESERVED_TABLES = ("users", "user_host_accounts", "alert_config", "token_blacklist", "audit_log")

#: AUTOINCREMENT tables among the preserved ones; their ``sqlite_sequence``
#: high-water travels with them.
AUTOINCREMENT_TABLES = ("audit_log",)

#: The single ``runtime_config`` row a fresh ``init_db()`` writes
#: (``app.database._AGG_CUTOVER_KEY``; copied rather than imported because
#: importing ``app.database`` resolves the master secret as a side effect).
CUTOVER_KEY = "metric_agg_cutover_raw_id"

# ``PRAGMA table_info`` rows — (cid, name, type, notnull, dflt_value, pk) — of
# every table this module reads or writes, exactly as ``init_db()`` creates
# them in this tree and as the 817e373 build created them on dev9. The test
# suite pins these against a real ``init_db()`` database, so schema drift fails
# a test before it can fail an operator.
EXPECTED_TABLES = {
    "users": (
        (0, "email", "TEXT", 0, None, 1),
        (1, "password_hash", "TEXT", 1, None, 0),
        (2, "totp_secret", "TEXT", 0, None, 0),
        (3, "totp_enabled", "INTEGER", 0, "0", 0),
        (4, "must_change_password", "INTEGER", 0, "0", 0),
        (5, "created_at", "REAL", 1, None, 0),
        (6, "role", "TEXT", 1, "'user'", 0),
        (7, "is_active", "INTEGER", 1, "1", 0),
        (8, "tokens_valid_after", "REAL", 1, "0", 0),
    ),
    "user_host_accounts": (
        (0, "user_email", "TEXT", 1, None, 1),
        (1, "agent_id", "TEXT", 1, None, 2),
        (2, "host_user", "TEXT", 1, None, 0),
    ),
    "alert_config": (
        (0, "id", "INTEGER", 0, "1", 1),
        (1, "config", "TEXT", 1, None, 0),
    ),
    "token_blacklist": (
        (0, "token_hash", "TEXT", 0, None, 1),
        (1, "expires_at", "REAL", 1, None, 0),
    ),
    "audit_log": (
        (0, "id", "INTEGER", 0, None, 1),
        (1, "timestamp", "REAL", 1, None, 0),
        (2, "user_email", "TEXT", 1, None, 0),
        (3, "action", "TEXT", 1, None, 0),
        (4, "agent_id", "TEXT", 1, "''", 0),
        (5, "detail", "TEXT", 1, "'{}'", 0),
    ),
    "runtime_config": (
        (0, "key", "TEXT", 0, None, 1),
        (1, "value", "TEXT", 1, None, 0),
    ),
}

# ``PRAGMA foreign_key_list`` rows — (id, seq, table, from, to, on_update,
# on_delete, match). The ``user_host_accounts → users`` key is the relation
# the restore has to keep intact.
EXPECTED_FOREIGN_KEYS = {
    "users": (),
    "user_host_accounts": ((0, 0, "users", "user_email", "email", "NO ACTION", "CASCADE", "NONE"),),
    "alert_config": (),
    "token_blacklist": (),
    "audit_log": (),
    "runtime_config": (),
}

_SQLITE_TIMEOUT = 30.0


# ── typed refusals ───────────────────────────────────


class TransferRefused(Exception):
    """Base for every refusal. Messages name tables, counts and paths — never
    a row value and never a key byte.

    `aftermath` is attached to a refusal raised inside the restore transaction
    once the target has been proven back in its fresh state: what state the
    target is in, and how to continue (RETRY_GUIDANCE)."""

    code = "TRANSFER_REFUSED"

    def __init__(self, message: str, *, aftermath: str | None = None):
        super().__init__(message)
        self.aftermath = aftermath

    def __str__(self) -> str:
        base = super().__str__()
        return f"{base} {self.aftermath}" if self.aftermath else base


class PathRejected(TransferRefused):
    """A path is missing, is a symlink, is not the kind of file expected, or
    already exists where this tool would create something."""

    code = "PATH_REJECTED"


class SchemaMismatch(TransferRefused):
    """A database does not have the columns or foreign keys this module expects."""

    code = "SCHEMA_MISMATCH"


class RuntimeConfigNotEmpty(TransferRefused):
    """The source holds runtime_config rows; carrying or dropping them is the
    operator's decision, so the export stops."""

    code = "RUNTIME_CONFIG_NOT_EMPTY"


class BundleRejected(TransferRefused):
    """The bundle is incomplete, malformed, edited, or carries tables outside
    the allowlist."""

    code = "BUNDLE_REJECTED"


class TargetNotFresh(TransferRefused):
    """The target database holds more than init_db() leaves behind."""

    code = "TARGET_NOT_FRESH"


class VerificationFailed(TransferRefused):
    """Applying or proving the restored state failed; everything was rolled back."""

    code = "VERIFICATION_FAILED"


class RestoreInterrupted(TransferRefused):
    """Something other than a refusal (an OSError, a driver error, an
    interrupt) stopped the restore before COMMIT, and the target has been
    proven back in its fresh state. Retry-safe: see RETRY_GUIDANCE."""

    code = "RESTORE_INTERRUPTED"


class ManualVerificationRequired(TransferRefused):
    """The restore failed AND the target could not be proven back in its fresh
    state (the rollback failed, or the re-check did). No retry recipe is
    offered: an operator has to look before anything else happens."""

    code = "MANUAL_VERIFICATION_REQUIRED"


#: Attached to every failure raised inside the restore transaction once the
#: target has been PROVEN back in its fresh state. The key may already be the
#: bundle's because it is installed just before COMMIT; a re-run accepts it.
RETRY_GUIDANCE = (
    "State: the target database is back in its fresh state (nothing from the "
    "bundle was committed), the target secret.key may already hold the bundle "
    "key, and no report or COMPLETE marker was written. Do NOT start the target "
    "container until a run directory holds COMPLETE. Once the cause is fixed, "
    "re-run restore with the same bundle and a NEW --run-dir; it completes from "
    "this state."
)

#: The message when that proof could not be obtained. Deliberately without a
#: retry recipe.
MANUAL_GUIDANCE = (
    "MANUAL VERIFICATION REQUIRED: the target's state could not be confirmed. Do "
    "NOT start the target container and do not run restore again until an "
    "operator has inspected the target database and its secret.key; no report "
    "or COMPLETE marker was written."
)


# ── reports ──────────────────────────────────────────


@dataclass(frozen=True)
class ExportReport:
    bundle_dir: str
    source_db: str
    row_counts: dict
    table_digests: dict
    autoincrement: dict
    manifest_sha256: str
    secret_key_sha256: str
    secret_key_size: int

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class RestoreReport:
    run_dir: str
    bundle_dir: str
    target_db: str
    row_counts: dict
    table_digests: dict
    autoincrement: dict
    monitoring_rows: dict
    foreign_key_violations: int
    bootstrap_admin_removed: str | None
    initial_admin_password_removed: bool
    secret_key_sha256: str
    secret_key_installed: bool
    manifest_sha256: str

    def as_dict(self) -> dict:
        return asdict(self)


# ── canonical encoding ───────────────────────────────


def canonical_bytes(obj) -> bytes:
    """One byte sequence per logical value: sorted keys, no whitespace, ASCII
    only, and no NaN/Infinity (which JSON cannot carry faithfully)."""
    return json.dumps(
        obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    ).encode("ascii")


def table_digest(columns, rows) -> str:
    return hashlib.sha256(canonical_bytes({"columns": list(columns), "rows": rows})).hexdigest()


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _encode_value(value, table: str, column: str):
    """SQLite storage class → JSON value. NULL, INTEGER, REAL and TEXT map to
    themselves (Python's json keeps 1 and 1.0 apart). None of the preserved
    columns is a BLOB and JSON cannot carry ±Infinity, so either is refused
    rather than approximated."""
    if value is None or isinstance(value, str):
        return value
    if isinstance(value, bool):
        raise SchemaMismatch(f"{table}.{column} holds an unsupported value type")
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise SchemaMismatch(f"{table}.{column} holds a non-finite REAL value")
        return value
    raise SchemaMismatch(f"{table}.{column} holds an unsupported value type {type(value).__name__}")


def _decode_value(value, table: str, column: str):
    if value is None or isinstance(value, str):
        return value
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise BundleRejected(f"{table}.{column} carries an unsupported JSON value")
    if isinstance(value, float) and not math.isfinite(value):
        raise BundleRejected(f"{table}.{column} carries a non-finite number")
    return value


def _reject_constant(name: str):
    raise ValueError(f"non-finite number {name}")


def _load_json(data: bytes, what: str) -> dict:
    try:
        obj = json.loads(data.decode("utf-8"), parse_constant=_reject_constant)
    except (UnicodeDecodeError, ValueError) as exc:
        raise BundleRejected(f"{what} is not valid JSON") from exc
    if not isinstance(obj, dict):
        raise BundleRejected(f"{what} is not a JSON object")
    return obj


# ── schema and table access ──────────────────────────


def _columns(table: str) -> tuple[str, ...]:
    return tuple(row[1] for row in EXPECTED_TABLES[table])


def _order_by(table: str) -> str:
    """The declared primary key, in key order — never rowid, which users'
    rows do not keep across a restore."""
    keyed = sorted((row[5], row[1]) for row in EXPECTED_TABLES[table] if row[5] > 0)
    return ", ".join(name for _, name in keyed)


def _integer_pk(table: str) -> str:
    (name,) = [row[1] for row in EXPECTED_TABLES[table] if row[5] == 1]
    return name


def verify_schema(conn: sqlite3.Connection, where: str) -> None:
    """Refuse a database whose preserved tables are not shaped as expected.
    Compared as PRAGMA output, column by column, including defaults and
    foreign keys — a superset is a mismatch too."""
    for table, expected in EXPECTED_TABLES.items():
        actual = tuple(tuple(row) for row in conn.execute(f"PRAGMA table_info({table})"))
        if actual != expected:
            raise SchemaMismatch(f"{where} table {table!r} is missing or does not have the expected columns")
        keys = tuple(tuple(row) for row in conn.execute(f"PRAGMA foreign_key_list({table})"))
        if keys != EXPECTED_FOREIGN_KEYS[table]:
            raise SchemaMismatch(f"{where} table {table!r} does not have the expected foreign keys")


def _read_table(conn: sqlite3.Connection, table: str) -> tuple[list[str], list[list]]:
    columns = _columns(table)
    sql = f"SELECT {', '.join(columns)} FROM {table} ORDER BY {_order_by(table)}"
    rows = [
        [_encode_value(value, table, column) for value, column in zip(row, columns)]
        for row in conn.execute(sql)
    ]
    return list(columns), rows


def _has_sequence_table(conn: sqlite3.Connection) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'sqlite_sequence'"
    ).fetchone() is not None


def _read_autoincrement(conn: sqlite3.Connection) -> dict:
    have = _has_sequence_table(conn)
    out = {}
    for table in AUTOINCREMENT_TABLES:
        row = conn.execute("SELECT seq FROM sqlite_sequence WHERE name = ?", (table,)).fetchone() if have else None
        out[table] = int(row[0]) if row and row[0] is not None else None
    return out


def _quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _user_tables(conn: sqlite3.Connection) -> list[str]:
    return [row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]


def _count(conn: sqlite3.Connection, table: str) -> int:
    return int(conn.execute(f"SELECT count(*) FROM {_quote_ident(table)}").fetchone()[0])


def _assert_fresh(conn: sqlite3.Connection) -> str | None:
    """The target must be exactly what init_db() leaves: at most the bootstrap
    admin in users, at most the cutover row in runtime_config, nothing else
    anywhere. Returns the bootstrap admin's email, or None if users is empty."""
    bootstrap = None
    for table in _user_tables(conn):
        if table == "runtime_config":
            for key, value in conn.execute("SELECT key, value FROM runtime_config"):
                if key != CUTOVER_KEY or value != "0":
                    raise TargetNotFresh(
                        f"target runtime_config holds key {key!r}, which a fresh database does not")
            continue
        n = _count(conn, table)
        if table == "users":
            if n == 0:
                continue
            if n > 1:
                raise TargetNotFresh(
                    f"target users holds {n} rows; a fresh database holds only the bootstrap admin")
            email, role = conn.execute("SELECT email, role FROM users").fetchone()
            if role != "admin":
                raise TargetNotFresh("target's single user is not the bootstrap admin")
            bootstrap = email
        elif n:
            raise TargetNotFresh(f"target {table} holds {n} row(s); a fresh database holds none")
    return bootstrap


def _rollback_quietly(conn: sqlite3.Connection) -> None:
    """End a transaction that only READ the target: whatever the outcome, the
    target is as it was found, so there is nothing to re-verify."""
    try:
        conn.execute("ROLLBACK")
    except Exception:  # noqa: BLE001
        pass


def _return_target_to_fresh(conn: sqlite3.Connection) -> str | None:
    """After a failure inside the restore transaction: roll the rows back, then
    PROVE the target is in its fresh state again. Returns None when both
    succeeded, otherwise a short reason. Never raises and never quotes a
    value: the verdict is "confirmed fresh" or "an operator has to look",
    nothing in between."""
    try:
        conn.execute("ROLLBACK")
    except Exception as exc:  # noqa: BLE001 — any failure here means "unknown state"
        return f"rollback failed: {type(exc).__name__}"
    try:
        _assert_fresh(conn)
    except TransferRefused as exc:
        return f"fresh re-check failed: {exc}"
    except Exception as exc:  # noqa: BLE001
        return f"fresh re-check failed: {type(exc).__name__}"
    return None


def _other_table_counts(conn: sqlite3.Connection) -> dict:
    """Row counts of every table that is NOT carried — the monitoring tables
    and anything a newer build may add — all of which must stay at zero."""
    return {
        table: _count(conn, table)
        for table in _user_tables(conn)
        if table not in PRESERVED_TABLES and table != "runtime_config"
    }


def _apply_autoincrement(conn: sqlite3.Connection, wanted: dict) -> dict:
    applied = {}
    for table in AUTOINCREMENT_TABLES:
        pk = _integer_pk(table)
        max_id = conn.execute(f"SELECT max({pk}) FROM {table}").fetchone()[0] or 0
        seq = wanted.get(table)
        if seq is None:
            row = conn.execute("SELECT seq FROM sqlite_sequence WHERE name = ?", (table,)).fetchone()
            applied[table] = int(row[0]) if row and row[0] is not None else None
            continue
        high = max(int(seq), int(max_id))
        conn.execute("DELETE FROM sqlite_sequence WHERE name = ?", (table,))
        conn.execute("INSERT INTO sqlite_sequence (name, seq) VALUES (?, ?)", (table, high))
        applied[table] = high
    return applied


# ── files ────────────────────────────────────────────


def _lstat(path: Path):
    try:
        return os.lstat(path)
    except FileNotFoundError:
        return None


def _require_regular_file(path: Path, what: str) -> None:
    st = _lstat(path)
    if st is None:
        raise PathRejected(f"{what} does not exist: {path}")
    if stat.S_ISLNK(st.st_mode):
        raise PathRejected(f"{what} is a symlink: {path}")
    if not stat.S_ISREG(st.st_mode):
        raise PathRejected(f"{what} is not a regular file: {path}")


def _require_directory(path: Path, what: str, *, follow_symlink: bool) -> None:
    st = _lstat(path)
    if st is None:
        raise PathRejected(f"{what} does not exist: {path}")
    if stat.S_ISLNK(st.st_mode):
        if not follow_symlink:
            raise PathRejected(f"{what} is a symlink: {path}")
        st = os.stat(path)
    if not stat.S_ISDIR(st.st_mode):
        raise PathRejected(f"{what} is not a directory: {path}")


def _require_absent(path: Path, what: str) -> None:
    """Nothing may exist at `path` — not a file, not a directory, not even a
    dangling symlink — and its parent must be a directory we can create in."""
    if _lstat(path) is not None:
        raise PathRejected(f"{what} already exists: {path}")
    if not path.parent.is_dir():
        raise PathRejected(f"parent of {what} is not a directory: {path.parent}")


def _read_all(fd: int) -> bytes:
    chunks = []
    while True:
        chunk = os.read(fd, 1 << 20)
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)


def _read_private(path: Path) -> bytes:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise PathRejected(f"not a regular file: {path}")
        return _read_all(fd)
    finally:
        os.close(fd)


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _make_private_dir(path: Path, what: str) -> None:
    try:
        os.mkdir(path, 0o700)
    except FileExistsError as exc:
        raise PathRejected(f"{what} already exists: {path}") from exc
    os.chmod(path, 0o700)
    _fsync_dir(path.parent)


def _write_private_file(path: Path, data: bytes, *, replace: bool = False) -> None:
    """tmp (0600, O_EXCL|O_NOFOLLOW) → write → fsync → rename → fsync(parent).
    Refuses an existing destination unless `replace` is asked for explicitly."""
    if not replace and _lstat(path) is not None:
        raise PathRejected(f"refusing to overwrite an existing file: {path}")
    tmp = path.with_name(f".{path.name}.tmp")
    if _lstat(tmp) is not None:
        raise PathRejected(f"a stale temporary file is in the way: {tmp}")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        try:
            os.fchmod(fd, 0o600)
            view = memoryview(data)
            while view:
                written = os.write(fd, view)
                view = view[written:]
            os.fsync(fd)
        finally:
            os.close(fd)
        os.rename(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    _fsync_dir(path.parent)


def _uri(path: Path, mode: str) -> str:
    return "file:" + urllib.parse.quote(os.path.abspath(path)) + f"?mode={mode}"


# ── export ───────────────────────────────────────────


def export_bundle(*, source_db, bundle_dir, data_dir=None) -> ExportReport:
    """Read the account state of `source_db` (through its WAL, without
    modifying either file) and write it, with `<data_dir>/secret.key`, into
    the new directory `bundle_dir`."""
    source_db = Path(source_db)
    bundle_dir = Path(bundle_dir)
    data_dir = Path(data_dir) if data_dir is not None else source_db.parent
    secret_path = data_dir / SECRET_KEY_NAME

    _require_regular_file(source_db, "source database")
    _require_regular_file(secret_path, "source secret.key")
    _require_absent(bundle_dir, "bundle directory")
    secret = _read_private(secret_path)
    if not secret:
        raise PathRejected(f"source secret.key is empty: {secret_path}")

    # mode=ro: reads see the WAL, but this connection can neither write nor
    # checkpoint — including on close, which a read-write connection would do
    # as the last one out. One BEGIN, so every table comes from one snapshot.
    conn = sqlite3.connect(_uri(source_db, "ro"), uri=True, isolation_level=None, timeout=_SQLITE_TIMEOUT)
    try:
        conn.execute("PRAGMA query_only=1")
        conn.execute("BEGIN")
        verify_schema(conn, "source")
        pending = _count(conn, "runtime_config")
        if pending:
            raise RuntimeConfigNotEmpty(
                f"source runtime_config holds {pending} row(s); this transfer only handles an empty one")
        tables = {table: _read_table(conn, table) for table in PRESERVED_TABLES}
        autoincrement = _read_autoincrement(conn)
        conn.execute("COMMIT")
    finally:
        conn.close()

    secret_sha = _sha256(secret)
    manifest = {
        "format": FORMAT,
        "tables": {
            table: {
                "columns": columns,
                "rows": rows,
                "row_count": len(rows),
                "digest": table_digest(columns, rows),
            }
            for table, (columns, rows) in tables.items()
        },
        "autoincrement": autoincrement,
        "secret_key": {"sha256": secret_sha, "size": len(secret)},
    }
    body = canonical_bytes(manifest)
    manifest_sha = _sha256(body)

    _make_private_dir(bundle_dir, "bundle directory")
    _write_private_file(bundle_dir / MANIFEST_NAME, body)
    _write_private_file(bundle_dir / SECRET_KEY_NAME, secret)
    marker = {
        "format": FORMAT,
        "manifest_sha256": manifest_sha,
        "secret_key_sha256": secret_sha,
        "source_db": os.path.abspath(source_db),
    }
    _write_private_file(bundle_dir / COMPLETE_MARKER, canonical_bytes(marker))

    return ExportReport(
        bundle_dir=os.path.abspath(bundle_dir),
        source_db=os.path.abspath(source_db),
        row_counts={table: len(rows) for table, (_c, rows) in tables.items()},
        table_digests={table: manifest["tables"][table]["digest"] for table in PRESERVED_TABLES},
        autoincrement=autoincrement,
        manifest_sha256=manifest_sha,
        secret_key_sha256=secret_sha,
        secret_key_size=len(secret),
    )


# ── restore ──────────────────────────────────────────


def _validate_manifest(manifest: dict) -> None:
    if manifest.get("format") != FORMAT:
        raise BundleRejected(f"manifest format is not {FORMAT}")
    tables = manifest.get("tables")
    if not isinstance(tables, dict):
        raise BundleRejected("manifest has no tables section")
    extra = sorted(set(tables) - set(PRESERVED_TABLES))
    if extra:
        raise BundleRejected(f"manifest carries tables outside the allowlist: {extra}")
    missing = sorted(set(PRESERVED_TABLES) - set(tables))
    if missing:
        raise BundleRejected(f"manifest lacks tables: {missing}")
    for table in PRESERVED_TABLES:
        entry = tables[table]
        if not isinstance(entry, dict):
            raise BundleRejected(f"manifest entry for {table} is malformed")
        columns, rows = entry.get("columns"), entry.get("rows")
        if columns != list(_columns(table)):
            raise BundleRejected(f"manifest columns for {table} are not the expected ones")
        if not isinstance(rows, list) or any(
                not isinstance(row, list) or len(row) != len(columns) for row in rows):
            raise BundleRejected(f"manifest rows for {table} are malformed")
        if entry.get("row_count") != len(rows):
            raise BundleRejected(f"manifest row_count for {table} does not match its rows")
        if entry.get("digest") != table_digest(columns, rows):
            raise BundleRejected(f"manifest digest for {table} does not match its rows")
    auto = manifest.get("autoincrement")
    if not isinstance(auto, dict) or set(auto) != set(AUTOINCREMENT_TABLES) or any(
            value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 0)
            for value in auto.values()):
        raise BundleRejected("manifest autoincrement section is malformed")
    secret = manifest.get("secret_key")
    if (not isinstance(secret, dict) or not isinstance(secret.get("sha256"), str)
            or isinstance(secret.get("size"), bool) or not isinstance(secret.get("size"), int)
            or secret["size"] <= 0):
        raise BundleRejected("manifest secret_key section is malformed")


def _reconfirm_identical_key(path: Path, data: bytes) -> bool:
    """True when `path` already holds exactly `data` AND this process has just
    made that file durable: 0600 set on the descriptor, fsync(file), close,
    fsync(parent directory). False when the bytes differ (the caller rewrites).

    "The same bytes are visible" is not evidence that the rename which put
    them there was ever made durable — an interrupted run can have failed at
    exactly that directory fsync — so the re-check repeats the durability
    steps and lets any failure propagate, which keeps the database from
    committing on top of a key that is not known to be on disk."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise PathRejected(f"target secret.key is not a regular file: {path}")
        if _read_all(fd) != data:
            return False
        os.fchmod(fd, 0o600)
        os.fsync(fd)
    finally:
        os.close(fd)
    _fsync_dir(path.parent)
    return True


def _install_secret_key(path: Path, data: bytes, expected_sha: str) -> bool:
    """Put the bundle's key at `path` byte for byte. Returns False when the
    identical bytes were already there (a re-run after an interrupted restore)
    — but only once `_reconfirm_identical_key` has fsynced that file and its
    directory entry, since the earlier run may have stopped before doing so."""
    st = _lstat(path)
    if st is not None:
        if not stat.S_ISREG(st.st_mode):
            raise PathRejected(f"target secret.key is a symlink or not a regular file: {path}")
        if _reconfirm_identical_key(path, data):
            return False
    _write_private_file(path, data, replace=True)
    if _sha256(_read_private(path)) != expected_sha:
        raise VerificationFailed("target secret.key does not read back with the exported digest")
    return True


def restore_bundle(*, bundle_dir, target_db, run_dir, data_dir=None) -> RestoreReport:
    """Apply the bundle to a database that is exactly what init_db() creates,
    prove the result inside the same transaction, install secret.key, and
    only then write the report and completion marker into the new `run_dir`.

    Operator contract: do not start the target container until `run_dir`
    holds COMPLETE. A failure before COMMIT leaves the database fresh and
    possibly the bundle key already installed — reported as RestoreInterrupted,
    or as the original refusal with RETRY_GUIDANCE attached — and a re-run
    with the same bundle and a new run directory completes from there.
    ManualVerificationRequired means that fresh state could not be confirmed:
    inspect the target before doing anything else."""
    bundle_dir = Path(bundle_dir)
    target_db = Path(target_db)
    run_dir = Path(run_dir)
    data_dir = Path(data_dir) if data_dir is not None else target_db.parent
    target_secret = data_dir / SECRET_KEY_NAME
    password_file = data_dir / INITIAL_ADMIN_PASSWORD_FILE

    _require_directory(bundle_dir, "bundle directory", follow_symlink=False)
    for name in (COMPLETE_MARKER, MANIFEST_NAME, SECRET_KEY_NAME):
        _require_regular_file(bundle_dir / name, f"bundle {name}")
    _require_regular_file(target_db, "target database")
    _require_directory(data_dir, "target data directory", follow_symlink=True)
    existing_secret = _lstat(target_secret)
    if existing_secret is not None and not stat.S_ISREG(existing_secret.st_mode):
        raise PathRejected(f"target secret.key is a symlink or not a regular file: {target_secret}")
    _require_absent(run_dir, "run directory")

    marker = _load_json(_read_private(bundle_dir / COMPLETE_MARKER), "completion marker")
    body = _read_private(bundle_dir / MANIFEST_NAME)
    manifest_sha = _sha256(body)
    if marker.get("format") != FORMAT or marker.get("manifest_sha256") != manifest_sha:
        raise BundleRejected("manifest does not match its completion marker")
    manifest = _load_json(body, "manifest")
    _validate_manifest(manifest)
    secret = _read_private(bundle_dir / SECRET_KEY_NAME)
    secret_sha = _sha256(secret)
    if (secret_sha != manifest["secret_key"]["sha256"] or len(secret) != manifest["secret_key"]["size"]
            or marker.get("secret_key_sha256") != secret_sha):
        raise BundleRejected("bundle secret.key does not match the digest recorded at export")
    decoded = {
        table: [
            [_decode_value(value, table, column)
             for value, column in zip(row, manifest["tables"][table]["columns"])]
            for row in manifest["tables"][table]["rows"]
        ]
        for table in PRESERVED_TABLES
    }

    _make_private_dir(run_dir, "run directory")

    # mode=rw (not rwc): a missing target is an error, never a new database.
    conn = sqlite3.connect(_uri(target_db, "rw"), uri=True, isolation_level=None, timeout=_SQLITE_TIMEOUT)
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        if conn.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
            raise VerificationFailed("could not enable foreign key enforcement on the target")
        conn.execute("BEGIN IMMEDIATE")
        try:
            # Read-only so far: a refusal here leaves the target exactly as it
            # was found (fresh or not), so it is raised as-is.
            verify_schema(conn, "target")
            bootstrap = _assert_fresh(conn)
        except BaseException:
            _rollback_quietly(conn)
            raise
        try:
            if bootstrap is not None:
                # Explicit, before the bundle's users arrive: one of them may
                # carry the same email, and the bootstrap row must not survive
                # under it or beside it.
                conn.execute("DELETE FROM users WHERE email = ?", (bootstrap,))
            for table in PRESERVED_TABLES:
                columns = manifest["tables"][table]["columns"]
                sql = (f"INSERT INTO {table} ({', '.join(columns)}) "
                       f"VALUES ({', '.join('?' * len(columns))})")
                try:
                    conn.executemany(sql, decoded[table])
                except sqlite3.Error as exc:
                    raise VerificationFailed(
                        f"applying {table} rows failed: {type(exc).__name__}") from exc
            autoincrement = _apply_autoincrement(conn, manifest["autoincrement"])

            violations = conn.execute("PRAGMA foreign_key_check").fetchall()
            if violations:
                raise VerificationFailed(
                    f"foreign_key_check reported {len(violations)} violation(s) after restore")
            row_counts, digests = {}, {}
            for table in PRESERVED_TABLES:
                columns, rows = _read_table(conn, table)
                row_counts[table] = len(rows)
                digests[table] = table_digest(columns, rows)
                entry = manifest["tables"][table]
                if row_counts[table] != entry["row_count"] or digests[table] != entry["digest"]:
                    raise VerificationFailed(f"restored {table} does not read back as exported")
            for table, high in autoincrement.items():
                if high is not None:
                    row = conn.execute("SELECT seq FROM sqlite_sequence WHERE name = ?", (table,)).fetchone()
                    if not row or int(row[0]) != high:
                        raise VerificationFailed(f"autoincrement high-water for {table} did not apply")
            monitoring = _other_table_counts(conn)
            populated = sorted(table for table, n in monitoring.items() if n)
            if populated:
                raise VerificationFailed(f"monitoring tables are not empty after restore: {populated}")

            # Last, once the database state is proven: a failure here still
            # rolls the rows back, and a re-run finds the key already in place.
            installed = _install_secret_key(target_secret, secret, secret_sha)
            conn.execute("COMMIT")
        except BaseException as exc:
            # From here on the target may have been modified, so nothing
            # leaves without a verdict on it: proven fresh again (retry-safe,
            # guidance attached) or not (manual verification, no retry
            # recipe). This covers the moment after the key was installed
            # and before COMMIT.
            unconfirmed = _return_target_to_fresh(conn)
            if unconfirmed is not None:
                raise ManualVerificationRequired(
                    f"restore stopped before COMMIT ({type(exc).__name__}) and the target "
                    f"could not be confirmed back in its fresh state: {unconfirmed}. "
                    f"{MANUAL_GUIDANCE}") from exc
            if isinstance(exc, TransferRefused):
                exc.aftermath = RETRY_GUIDANCE
                raise
            raise RestoreInterrupted(
                f"restore stopped before COMMIT: {type(exc).__name__}.",
                aftermath=RETRY_GUIDANCE) from exc
    finally:
        conn.close()

    # The bootstrap credential file describes an account that no longer
    # exists. Best effort, like the product's own cleanup of it.
    password_removed = False
    try:
        os.unlink(password_file)
        password_removed = True
    except FileNotFoundError:
        pass
    except OSError as exc:
        print(f"warning: could not remove {password_file}: {type(exc).__name__}", file=sys.stderr)

    report = RestoreReport(
        run_dir=os.path.abspath(run_dir),
        bundle_dir=os.path.abspath(bundle_dir),
        target_db=os.path.abspath(target_db),
        row_counts=row_counts,
        table_digests=digests,
        autoincrement=autoincrement,
        monitoring_rows=monitoring,
        foreign_key_violations=0,
        bootstrap_admin_removed=bootstrap,
        initial_admin_password_removed=password_removed,
        secret_key_sha256=secret_sha,
        secret_key_installed=installed,
        manifest_sha256=manifest_sha,
    )
    report_bytes = canonical_bytes(report.as_dict())
    _write_private_file(run_dir / REPORT_NAME, report_bytes)
    _write_private_file(run_dir / COMPLETE_MARKER, canonical_bytes({
        "format": FORMAT,
        "report_sha256": _sha256(report_bytes),
        "manifest_sha256": manifest_sha,
        "secret_key_sha256": secret_sha,
    }))
    return report


# ── command line ─────────────────────────────────────


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.account_state_transfer",
        description="Carry GlassOps accounts, host mappings, alert config, revoked tokens, "
                    "the audit log and secret.key from an old database into a freshly "
                    "initialised one. Monitoring data is left behind on purpose.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    export = commands.add_parser("export", help="read the old database into a new bundle directory")
    export.add_argument("--source-db", required=True, type=Path, help="old glassops.db (its -wal is read too)")
    export.add_argument("--bundle", required=True, type=Path, help="new directory to create (must not exist)")
    export.add_argument("--data-dir", type=Path, default=None,
                        help="directory holding secret.key (default: the source database's directory)")
    restore = commands.add_parser("restore", help="apply a bundle to a freshly initialised database")
    restore.add_argument("--bundle", required=True, type=Path)
    restore.add_argument("--target-db", required=True, type=Path, help="glassops.db created by the new build's init_db")
    restore.add_argument("--run-dir", required=True, type=Path, help="new directory for the report (must not exist)")
    restore.add_argument("--data-dir", type=Path, default=None,
                         help="directory receiving secret.key (default: the target database's directory)")
    args = parser.parse_args(argv)
    try:
        if args.command == "export":
            report = export_bundle(source_db=args.source_db, bundle_dir=args.bundle, data_dir=args.data_dir)
        else:
            report = restore_bundle(bundle_dir=args.bundle, target_db=args.target_db,
                                    run_dir=args.run_dir, data_dir=args.data_dir)
    except TransferRefused as exc:
        print(f"refused {exc.code}: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 — a host-side tool: the type name is the actionable part
        print(f"error {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(report.as_dict(), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
