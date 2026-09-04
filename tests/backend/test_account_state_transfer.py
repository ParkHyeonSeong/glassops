"""Focused tests for `app.account_state_transfer`.

dev9 keeps its accounts and settings but abandons its monitoring history: the
old database is read ONCE through SQLite, the account tables are carried over
in a small bundle together with the exact bytes of `secret.key`, and the
bundle is restored into the clean database the new build creates on first
start. Metrics, network audit rows and the old DB/WAL files stay behind.

Everything here is local and disposable. Every database is built by this file
inside pytest's `tmp_path`: the source re-creates the schema dev9's running
build (817e373) wrote, and the target is the real `init_db()` of this tree.
Nothing reaches dev9, a Docker daemon, or a real volume.

Two oracles carry the suite:

* a WAL-only commit — the source fixture commits its last account changes
  from a child process that is SIGKILLed with its connection still open, then
  proves on COPIES (never on the source itself) that those rows are absent
  from the main file and present only through the WAL. An export that copied
  the main file, or dropped the WAL, cannot pass.
* byte identity of the source — the source's main file and WAL are hashed
  before and after every export, successful or refused.
"""

import hashlib
import json
import os
import shutil
import sqlite3
import stat
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

import app.database as db
import app.account_state_transfer as transfer

BACKEND_DIR = Path(__file__).resolve().parents[2] / "backend"

# ── identities and secrets used by the fixtures ─────
#
# Every value below is a marker: distinctive enough that its presence in a
# manifest, a database or a process's output is attributable, and (for the
# secret ones) that its ABSENCE from stdout/stderr/exception text can be
# asserted rather than hoped for.

ADMIN = "admin@glassops.local"       # same address init_db uses for its bootstrap admin
ALICE = "alice@example.com"
BOB = "bob@example.com"
CAROL = "carol@example.com"          # exists only in the WAL of the source

ADMIN_HASH = "$2b$12$ADMINHASH.oldbuild.oldbuild.oldbuild.oldbuild.oldbuild.oldbu"
ALICE_HASH_V1 = "$2b$12$ALICEHASH.v1.main-file.main-file.main-file.main-file.main-f"
ALICE_HASH_V2 = "$2b$12$ALICEHASH.v2.WAL-ONLY.WAL-ONLY.WAL-ONLY.WAL-ONLY.WAL-ONLY.W"
BOB_HASH = "$2b$12$BOBHASH.disabled.disabled.disabled.disabled.disabled.disabled"
CAROL_HASH = "$2b$12$CAROLHASH.WAL-ONLY.WAL-ONLY.WAL-ONLY.WAL-ONLY.WAL-ONLY.WAL-O"
ALICE_TOTP = "ALICETOTPSECRETBASE32AAAAAAAAAAA"
CAROL_TOTP = "CAROLTOTPSECRETBASE32CCCCCCCCCCC"
ALICE_TOKENS_AFTER = 1700005000.5   # WAL-only change
ALICE_CREATED = 1700001000.25
CAROL_CREATED = 1700006000.125
SMTP_TOKEN = "gAAAAABfakefernettokenforsmtppassword0000000000000000000000000000"
TOKEN_HASH_1 = "sha256:" + "ab" * 32
TOKEN_HASH_2 = "sha256:" + "cd" * 32
SECRET_HEX = "9b1f0a2e3c4d5e6f708192a3b4c5d6e7" * 2
# A trailing newline is a state resolve_secret() accepts (it strips), and it is
# exactly what a copy that "cleans up" the value would lose.
SECRET_BYTES = (SECRET_HEX + "\n").encode()
FRESH_SECRET_HEX = "0f1e2d3c4b5a69788796a5b4c3d2e1f0" * 2
METRIC_PAYLOAD = '{"cpu": 99.5, "marker": "OLD-METRIC-PAYLOAD-MUST-NOT-TRAVEL"}'

SENSITIVE = (
    SECRET_HEX, FRESH_SECRET_HEX, ADMIN_HASH, ALICE_HASH_V1, ALICE_HASH_V2,
    BOB_HASH, CAROL_HASH, ALICE_TOTP, CAROL_TOTP, SMTP_TOKEN, TOKEN_HASH_1,
    TOKEN_HASH_2,
)

MONITORING_TABLES = (
    "metrics", "metrics_downsampled", "metric_agg_coverage", "metric_agg_bucket",
    "metric_agg_partial", "metric_agg_progress", "net_conn_events", "net_flow_rollup",
)

# The schema dev9's running build (commit 817e373) creates — copied verbatim so
# the source looks like the database the export will actually meet: no
# metric_agg_* tables, and an EMPTY runtime_config.
OLD_BUILD_DDL = (
    """CREATE TABLE IF NOT EXISTS metrics (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        agent_id TEXT NOT NULL,
        timestamp REAL NOT NULL,
        data TEXT NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS idx_metrics_agent_ts ON metrics (agent_id, timestamp)",
    """CREATE TABLE IF NOT EXISTS metrics_downsampled (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        agent_id TEXT NOT NULL,
        timestamp REAL NOT NULL,
        resolution TEXT NOT NULL,
        data TEXT NOT NULL
    )""",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_metrics_ds_unique ON metrics_downsampled (agent_id, resolution, timestamp)",
    """CREATE TABLE IF NOT EXISTS runtime_config (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS token_blacklist (
        token_hash TEXT PRIMARY KEY,
        expires_at REAL NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS alert_config (
        id INTEGER PRIMARY KEY DEFAULT 1,
        config TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS users (
        email TEXT PRIMARY KEY,
        password_hash TEXT NOT NULL,
        totp_secret TEXT,
        totp_enabled INTEGER DEFAULT 0,
        must_change_password INTEGER DEFAULT 0,
        created_at REAL NOT NULL,
        role TEXT NOT NULL DEFAULT 'user',
        is_active INTEGER NOT NULL DEFAULT 1,
        tokens_valid_after REAL NOT NULL DEFAULT 0
    )""",
    """CREATE TABLE IF NOT EXISTS user_host_accounts (
        user_email TEXT NOT NULL,
        agent_id TEXT NOT NULL,
        host_user TEXT NOT NULL,
        PRIMARY KEY (user_email, agent_id),
        FOREIGN KEY (user_email) REFERENCES users(email) ON DELETE CASCADE
    )""",
    """CREATE TABLE IF NOT EXISTS audit_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp REAL NOT NULL,
        user_email TEXT NOT NULL,
        action TEXT NOT NULL,
        agent_id TEXT NOT NULL DEFAULT '',
        detail TEXT NOT NULL DEFAULT '{}'
    )""",
    "CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_log (timestamp)",
    """CREATE TABLE IF NOT EXISTS net_conn_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        agent_id TEXT NOT NULL,
        ts REAL NOT NULL,
        event TEXT NOT NULL,
        proto TEXT NOT NULL,
        laddr TEXT, lport INTEGER,
        raddr TEXT, rport INTEGER,
        status TEXT,
        pid INTEGER, pname TEXT,
        duration REAL
    )""",
    "CREATE INDEX IF NOT EXISTS idx_nce_agent_ts ON net_conn_events (agent_id, ts)",
    "CREATE INDEX IF NOT EXISTS idx_nce_raddr ON net_conn_events (agent_id, raddr)",
    """CREATE TABLE IF NOT EXISTS net_flow_rollup (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        agent_id TEXT NOT NULL,
        ts REAL NOT NULL,
        data TEXT NOT NULL
    )""",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_nfr_unique ON net_flow_rollup (agent_id, ts)",
)

# The last account changes are committed by THIS child and never checkpointed:
# wal_autocheckpoint=0 keeps them out of the main file, synchronous=FULL puts
# them durably in the WAL, and the parent SIGKILLs the child with the
# connection still open, so no close() ever folds them back.
_CHILD_SOURCE = r'''
import sqlite3, sys, time
db, alice_hash, alice_after, carol_hash, carol_totp, carol_created = sys.argv[1:7]
conn = sqlite3.connect(db, isolation_level=None)
conn.execute("PRAGMA wal_autocheckpoint=0")
conn.execute("PRAGMA synchronous=FULL")
conn.execute("BEGIN IMMEDIATE")
conn.execute("UPDATE users SET password_hash = ?, tokens_valid_after = ? WHERE email = ?",
             (alice_hash, float(alice_after), "alice@example.com"))
conn.execute("INSERT INTO users (email, password_hash, totp_secret, totp_enabled, "
             "must_change_password, created_at, role, is_active, tokens_valid_after) "
             "VALUES (?, ?, ?, 1, 0, ?, 'user', 1, 0)",
             ("carol@example.com", carol_hash, carol_totp, float(carol_created)))
conn.execute("INSERT INTO user_host_accounts VALUES ('carol@example.com', 'local', 'carol')")
conn.execute("COMMIT")
sys.stdout.write("COMMITTED\n")
sys.stdout.flush()
time.sleep(600)
'''


@dataclass(frozen=True)
class Source:
    data_dir: Path
    db: Path
    wal: Path
    secret: Path
    #: what the account tables hold once the WAL is applied — measured on a
    #: db+wal COPY, so the source itself is never opened by the test.
    expected: dict
    #: the AUTOINCREMENT high-water of audit_log; above max(id) because the
    #: fixture pruned the newest row, as cleanup_audit_log does in production.
    audit_sequence: int


@dataclass(frozen=True)
class Target:
    data_dir: Path
    db: Path
    secret: Path
    password_file: Path


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _source_hashes(source: Source) -> tuple[str, str]:
    return _sha256(source.db), _sha256(source.wal)


def _mode(path: Path) -> int:
    return stat.S_IMODE(os.lstat(path).st_mode)


def _typed_rows(conn: sqlite3.Connection, table: str) -> list:
    """Every row of `table`, each value paired with its Python type, in an
    order that does not depend on rowid (users' rowids are not preserved)."""
    rows = conn.execute(f"SELECT * FROM {table}").fetchall()
    return sorted(
        (tuple((type(v).__name__, v) for v in row) for row in rows), key=repr
    )


def _dump(path: Path, tables) -> dict:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return {t: _typed_rows(conn, t) for t in tables}
    finally:
        conn.close()


def _all_tables(path: Path) -> list[str]:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND (name NOT LIKE 'sqlite_%' OR name = 'sqlite_sequence') ORDER BY name")]
    finally:
        conn.close()


def _dump_all(path: Path) -> dict:
    return _dump(path, _all_tables(path))


def _count(path: Path, table: str) -> int:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
    finally:
        conn.close()


def _rows_by(manifest: dict, table: str, key: str) -> dict:
    entry = manifest["tables"][table]
    return {
        dict(zip(entry["columns"], row))[key]: dict(zip(entry["columns"], row))
        for row in entry["rows"]
    }


def _read_manifest(bundle: Path) -> dict:
    return json.loads((bundle / transfer.MANIFEST_NAME).read_bytes())


def _rewrite_bundle(bundle: Path, manifest: dict) -> None:
    """Write an edited manifest back with CONSISTENT table digests and a
    matching completion marker — a bundle that passes every integrity check
    and is wrong anyway, which is what the semantic refusals must catch."""
    for entry in manifest["tables"].values():
        entry["row_count"] = len(entry["rows"])
        entry["digest"] = transfer.table_digest(entry["columns"], entry["rows"])
    body = transfer.canonical_bytes(manifest)
    (bundle / transfer.MANIFEST_NAME).write_bytes(body)
    marker = json.loads((bundle / transfer.COMPLETE_MARKER).read_bytes())
    marker["manifest_sha256"] = hashlib.sha256(body).hexdigest()
    (bundle / transfer.COMPLETE_MARKER).write_bytes(transfer.canonical_bytes(marker))


def _build_source(root: Path, *, runtime_config=(), extra_sql=()) -> Source:
    data = root / "old"
    data.mkdir()
    dbp = data / "glassops.db"
    conn = sqlite3.connect(dbp, isolation_level=None)
    assert conn.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
    for stmt in OLD_BUILD_DDL:
        conn.execute(stmt)
    conn.execute("BEGIN")
    conn.executemany(
        "INSERT INTO users VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [   # deliberately NOT in primary-key order: rowid order must not leak into the manifest
            (BOB, BOB_HASH, None, 0, 1, 1700003000.0, "user", 0, 1700004000.75),
            (ADMIN, ADMIN_HASH, None, 0, 0, 1700000000.5, "admin", 1, 0.0),
            (ALICE, ALICE_HASH_V1, ALICE_TOTP, 1, 0, ALICE_CREATED, "user", 1, 0.0),
        ],
    )
    conn.executemany(
        "INSERT INTO user_host_accounts VALUES (?, ?, ?)",
        [(ADMIN, "local", "root"), (ALICE, "local", "alice"), (ALICE, "gpu-01", "svc_alice")],
    )
    conn.execute(
        "INSERT INTO alert_config (id, config) VALUES (1, ?)",
        (json.dumps({"smtp_host": "mail.example", "smtp_password_enc": SMTP_TOKEN,
                     "thresholds": {"cpu": 90}}),),
    )
    conn.executemany(
        "INSERT INTO token_blacklist VALUES (?, ?)",
        [(TOKEN_HASH_1, 1700009999.0), (TOKEN_HASH_2, 1700008888.5)],
    )
    conn.executemany(
        "INSERT INTO audit_log (timestamp, user_email, action, agent_id, detail) VALUES (?, ?, ?, ?, ?)",
        [
            (1700000100.0, ADMIN, "user.create", "", json.dumps({"target": ALICE, "role": "user"})),
            (1700000200.5, ALICE, "auth.login", "", json.dumps({"ip": "10.0.0.7", "totp": True})),
            (1700000300.0, ALICE, "process.kill", "gpu-01", json.dumps({"pid": 4242})),
            (1700000400.0, ADMIN, "user.update", "", json.dumps({"target": BOB, "is_active": False})),
            (1700000500.0, ADMIN, "service.restart", "local", json.dumps({"service": "nginx"})),
        ],
    )
    # Pruned newest row: sqlite_sequence stays at 5 while max(id) is 4.
    conn.execute("DELETE FROM audit_log WHERE id = 5")
    conn.executemany(
        "INSERT INTO metrics (agent_id, timestamp, data) VALUES (?, ?, ?)",
        [("local", 1700000000.0 + i, METRIC_PAYLOAD) for i in range(3)],
    )
    conn.execute(
        "INSERT INTO metrics_downsampled (agent_id, timestamp, resolution, data) VALUES ('local', 1700000000.0, '1m', ?)",
        (METRIC_PAYLOAD,),
    )
    conn.executemany(
        "INSERT INTO net_conn_events (agent_id, ts, event, proto, laddr, lport, raddr, rport, status, pid, pname, duration) "
        "VALUES ('local', ?, 'open', 'tcp', '10.0.0.9', 5500, '8.8.8.8', 443, 'ESTABLISHED', 42, 'curl', NULL)",
        [(1700000000.0,), (1700000060.0,)],
    )
    conn.execute(
        "INSERT INTO net_flow_rollup (agent_id, ts, data) VALUES ('local', 1700000000.0, ?)",
        (METRIC_PAYLOAD,),
    )
    conn.executemany("INSERT INTO runtime_config (key, value) VALUES (?, ?)", list(runtime_config))
    for statement in extra_sql:
        conn.execute(statement)
    conn.execute("COMMIT")
    # Fold the baseline into the main file so the ONLY WAL-only content is the
    # child's commit below; then the last connection closes normally.
    assert conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0] == 0
    conn.close()

    secret = data / "secret.key"
    secret.write_bytes(SECRET_BYTES)
    os.chmod(secret, 0o600)

    proc = subprocess.Popen(
        [sys.executable, "-c", _CHILD_SOURCE, str(dbp), ALICE_HASH_V2,
         repr(ALICE_TOKENS_AFTER), CAROL_HASH, CAROL_TOTP, repr(CAROL_CREATED)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        line = proc.stdout.readline()
        assert line.strip() == "COMMITTED", proc.stderr.read()
    finally:
        proc.kill()
        proc.wait(timeout=30)
    wal = Path(str(dbp) + "-wal")
    assert wal.exists() and wal.stat().st_size > 0, "the child left no WAL behind"

    # The oracle, measured on copies: main file alone lacks the child's
    # commit, main file + WAL has it.
    oracle = root / "oracle"
    oracle.mkdir()
    shutil.copyfile(dbp, oracle / "db-only.db")
    db_only = sqlite3.connect(oracle / "db-only.db")
    try:
        assert db_only.execute("SELECT count(*) FROM users WHERE email = ?", (CAROL,)).fetchone()[0] == 0
        assert db_only.execute("SELECT password_hash FROM users WHERE email = ?", (ALICE,)).fetchone()[0] == ALICE_HASH_V1
    finally:
        db_only.close()
    shutil.copyfile(dbp, oracle / "both.db")
    shutil.copyfile(wal, oracle / "both.db-wal")
    expected = _dump(oracle / "both.db", transfer.PRESERVED_TABLES)
    both = sqlite3.connect(oracle / "both.db")
    try:
        assert both.execute("SELECT count(*) FROM users WHERE email = ?", (CAROL,)).fetchone()[0] == 1
        assert both.execute("SELECT password_hash FROM users WHERE email = ?", (ALICE,)).fetchone()[0] == ALICE_HASH_V2
        seq = both.execute("SELECT seq FROM sqlite_sequence WHERE name = 'audit_log'").fetchone()[0]
    finally:
        both.close()
    assert seq == 5
    return Source(data_dir=data, db=dbp, wal=wal, secret=secret, expected=expected, audit_sequence=seq)


@pytest.fixture
def source(tmp_path) -> Source:
    return _build_source(tmp_path)


@pytest.fixture
async def fresh_target(tmp_path, monkeypatch) -> Target:
    """The database the NEW build creates on first start: real init_db(), so
    the bootstrap admin, the cutover row and the initial-password file are
    exactly what the product writes, not what this test assumes it writes."""
    data = tmp_path / "new"
    data.mkdir()
    dbp = data / "glassops.db"
    monkeypatch.delenv("GLASSOPS_ADMIN_EMAIL", raising=False)
    monkeypatch.delenv("GLASSOPS_ADMIN_PASSWORD", raising=False)
    monkeypatch.setattr(db, "_db_path", str(dbp))
    monkeypatch.setattr(db, "_conn", None)
    await db.init_db()
    await db.close_db()
    db._closed = False
    secret = data / "secret.key"
    secret.write_bytes(FRESH_SECRET_HEX.encode())   # what secret_bootstrap auto-generates
    os.chmod(secret, 0o600)
    password_file = data / "initial_admin_password"
    assert password_file.exists(), "init_db did not leave its bootstrap credential file"
    assert _count(dbp, "users") == 1
    return Target(data_dir=data, db=dbp, secret=secret, password_file=password_file)


def _export(source: Source, bundle: Path):
    return transfer.export_bundle(source_db=source.db, bundle_dir=bundle)


def _restore(bundle: Path, target: Target, run: Path):
    return transfer.restore_bundle(bundle_dir=bundle, target_db=target.db, run_dir=run)


# ── export ───────────────────────────────────────────


def test_export_reads_account_changes_that_exist_only_in_the_wal(source, tmp_path):
    before = _source_hashes(source)

    report = _export(source, tmp_path / "bundle")

    manifest = _read_manifest(tmp_path / "bundle")
    users = _rows_by(manifest, "users", "email")
    assert CAROL in users, "a user that exists only in the WAL was not exported"
    assert users[ALICE]["password_hash"] == ALICE_HASH_V2
    assert users[ALICE]["tokens_valid_after"] == ALICE_TOKENS_AFTER
    assert _rows_by(manifest, "user_host_accounts", "host_user")["carol"]["user_email"] == CAROL
    assert report.row_counts["users"] == 4
    # Read, not consumed: the source's main file and WAL are byte-identical
    # and the WAL is still there — a read-only snapshot, not a checkpoint.
    assert _source_hashes(source) == before
    assert source.wal.exists()


def test_export_reads_every_table_from_one_snapshot(source, tmp_path, monkeypatch):
    """A write that commits while the export is between two tables must not
    be visible to the later table: all five come from one read transaction.
    (On dev9 the old backend may still be running while the export reads.)"""
    real_read = transfer._read_table
    intruded = []

    def read_then_intrude(conn, table):
        columns, rows = real_read(conn, table)
        if table == "users" and not intruded:
            writer = sqlite3.connect(source.db, isolation_level=None, timeout=5)
            writer.execute("INSERT INTO token_blacklist VALUES ('sha256:late-arrival', 1700010000.0)")
            writer.close()
            intruded.append(True)
        return columns, rows

    monkeypatch.setattr(transfer, "_read_table", read_then_intrude)
    report = _export(source, tmp_path / "bundle")

    assert intruded, "fixture: the concurrent write did not happen"
    assert report.row_counts["token_blacklist"] == 2, "a commit made mid-export leaked into the snapshot"
    assert _count(source.db, "token_blacklist") == 3   # it did land in the source, just after the snapshot


def test_export_refuses_a_source_whose_runtime_config_is_not_empty(tmp_path):
    source = _build_source(tmp_path, runtime_config=(("smtp_host", "mail.example.internal"),))
    before = _source_hashes(source)

    with pytest.raises(transfer.RuntimeConfigNotEmpty) as excinfo:
        _export(source, tmp_path / "bundle")

    assert "mail.example.internal" not in str(excinfo.value)
    assert not (tmp_path / "bundle").exists(), "a refused export left a bundle behind"
    assert _source_hashes(source) == before


def test_export_refuses_symlinks_and_existing_bundle_paths(source, tmp_path):
    before = _source_hashes(source)

    link = tmp_path / "link.db"
    link.symlink_to(source.db)
    with pytest.raises(transfer.PathRejected):
        transfer.export_bundle(source_db=link, bundle_dir=tmp_path / "b1")
    assert not (tmp_path / "b1").exists()

    existing = tmp_path / "b2"
    existing.mkdir()
    (existing / "keep").write_text("operator data")
    with pytest.raises(transfer.PathRejected):
        _export(source, existing)
    assert (existing / "keep").read_text() == "operator data"
    assert sorted(p.name for p in existing.iterdir()) == ["keep"]

    dangling = tmp_path / "b3"
    dangling.symlink_to(tmp_path / "nowhere")
    with pytest.raises(transfer.PathRejected):
        _export(source, dangling)

    elsewhere = tmp_path / "elsewhere.key"
    elsewhere.write_bytes(SECRET_BYTES)
    source.secret.unlink()
    source.secret.symlink_to(elsewhere)
    with pytest.raises(transfer.PathRejected):
        _export(source, tmp_path / "b4")
    assert not (tmp_path / "b4").exists()

    source.secret.unlink()
    with pytest.raises(transfer.PathRejected):
        _export(source, tmp_path / "b5")
    assert not (tmp_path / "b5").exists()

    assert _source_hashes(source) == before


def test_manifest_and_digests_are_deterministic(source, tmp_path):
    first = _export(source, tmp_path / "b1")
    second = _export(source, tmp_path / "b2")

    assert (tmp_path / "b1" / transfer.MANIFEST_NAME).read_bytes() == \
        (tmp_path / "b2" / transfer.MANIFEST_NAME).read_bytes()
    assert first.table_digests == second.table_digests
    assert first.manifest_sha256 == second.manifest_sha256
    assert first.secret_key_sha256 == second.secret_key_sha256 == hashlib.sha256(SECRET_BYTES).hexdigest()
    assert (tmp_path / "b1" / transfer.SECRET_KEY_NAME).read_bytes() == SECRET_BYTES


def test_manifest_rows_are_in_primary_key_order_not_insertion_order(source, tmp_path):
    _export(source, tmp_path / "bundle")

    manifest = _read_manifest(tmp_path / "bundle")
    users = manifest["tables"]["users"]
    emails = [row[users["columns"].index("email")] for row in users["rows"]]
    assert emails == [ADMIN, ALICE, BOB, CAROL], "the fixture inserted BOB first; the manifest must not care"
    hosts = manifest["tables"]["user_host_accounts"]
    keys = [(row[0], row[1]) for row in hosts["rows"]]
    assert keys == sorted(keys)


@pytest.mark.parametrize("statement", [
    "UPDATE audit_log SET detail = X'00ff10' WHERE id = 1",        # a BLOB in a TEXT column
    "UPDATE users SET tokens_valid_after = 1e999 WHERE email = 'bob@example.com'",   # +Infinity in a REAL column
])
def test_export_refuses_values_json_cannot_carry_faithfully(tmp_path, statement):
    source = _build_source(tmp_path, extra_sql=(statement,))
    before = _source_hashes(source)

    with pytest.raises(transfer.SchemaMismatch):
        _export(source, tmp_path / "bundle")

    assert not (tmp_path / "bundle").exists()
    assert _source_hashes(source) == before


def test_export_writes_only_the_allowed_tables(source, tmp_path):
    _export(source, tmp_path / "bundle")

    manifest = _read_manifest(tmp_path / "bundle")
    assert set(manifest["tables"]) == set(transfer.PRESERVED_TABLES)
    assert set(transfer.PRESERVED_TABLES) == {
        "users", "user_host_accounts", "alert_config", "token_blacklist", "audit_log"}
    text = (tmp_path / "bundle" / transfer.MANIFEST_NAME).read_text()
    assert METRIC_PAYLOAD not in text
    assert "net_conn_events" not in manifest["tables"]
    assert "runtime_config" not in manifest["tables"]


# ── restore ──────────────────────────────────────────


async def test_round_trip_preserves_every_account_table(source, fresh_target, tmp_path):
    _export(source, tmp_path / "bundle")

    report = _restore(tmp_path / "bundle", fresh_target, tmp_path / "run")

    got = _dump(fresh_target.db, transfer.PRESERVED_TABLES)
    assert got == source.expected, "restored account tables differ from the source (value or type)"
    users = {row[0][1]: {name: value for (_t, value), name in zip(row, (
        "email", "password_hash", "totp_secret", "totp_enabled", "must_change_password",
        "created_at", "role", "is_active", "tokens_valid_after"))} for row in got["users"]}
    assert users[ALICE]["password_hash"] == ALICE_HASH_V2
    assert users[ALICE]["totp_secret"] == ALICE_TOTP and users[ALICE]["totp_enabled"] == 1
    assert users[ALICE]["tokens_valid_after"] == ALICE_TOKENS_AFTER
    assert users[BOB]["is_active"] == 0 and users[BOB]["must_change_password"] == 1
    assert users[CAROL]["totp_secret"] == CAROL_TOTP and users[CAROL]["created_at"] == CAROL_CREATED
    assert users[ADMIN]["role"] == "admin"
    manifest = _read_manifest(tmp_path / "bundle")
    assert report.row_counts == {t: manifest["tables"][t]["row_count"] for t in transfer.PRESERVED_TABLES}
    assert report.row_counts == {t: len(rows) for t, rows in source.expected.items()}
    assert report.table_digests == {t: manifest["tables"][t]["digest"] for t in transfer.PRESERVED_TABLES}
    assert report.foreign_key_violations == 0
    assert (tmp_path / "run" / transfer.COMPLETE_MARKER).exists()


async def test_restore_keeps_the_audit_log_sequence_above_pruned_ids(source, fresh_target, tmp_path):
    _export(source, tmp_path / "bundle")

    _restore(tmp_path / "bundle", fresh_target, tmp_path / "run")

    conn = sqlite3.connect(f"file:{fresh_target.db}?mode=ro", uri=True)
    try:
        seq = conn.execute("SELECT seq FROM sqlite_sequence WHERE name = 'audit_log'").fetchone()[0]
        max_id = conn.execute("SELECT max(id) FROM audit_log").fetchone()[0]
    finally:
        conn.close()
    assert max_id == 4
    assert seq == source.audit_sequence == 5, "a pruned audit id could be handed out again"


async def test_restore_replaces_the_bootstrap_admin_instead_of_keeping_it(source, fresh_target, tmp_path):
    bootstrap = _dump(fresh_target.db, ("users",))["users"]
    assert len(bootstrap) == 1 and bootstrap[0][0] == ("str", ADMIN), "fixture: bootstrap admin"
    bootstrap_hash = bootstrap[0][1][1]
    assert bootstrap_hash != ADMIN_HASH
    _export(source, tmp_path / "bundle")

    report = _restore(tmp_path / "bundle", fresh_target, tmp_path / "run")

    users = {row[0][1]: row for row in _dump(fresh_target.db, ("users",))["users"]}
    assert set(users) == {ADMIN, ALICE, BOB, CAROL}
    assert users[ADMIN][1] == ("str", ADMIN_HASH), "the bootstrap admin's row survived under the same email"
    assert report.bootstrap_admin_removed == ADMIN
    assert not fresh_target.password_file.exists(), "the bootstrap admin's plaintext password file was left behind"
    assert report.initial_admin_password_removed is True


async def test_secret_key_is_restored_byte_for_byte(source, fresh_target, tmp_path):
    _export(source, tmp_path / "bundle")

    report = _restore(tmp_path / "bundle", fresh_target, tmp_path / "run")

    assert fresh_target.secret.read_bytes() == SECRET_BYTES
    assert _mode(fresh_target.secret) == 0o600
    assert report.secret_key_sha256 == hashlib.sha256(SECRET_BYTES).hexdigest()


async def test_monitoring_data_is_neither_exported_nor_restored(source, fresh_target, tmp_path):
    _export(source, tmp_path / "bundle")

    report = _restore(tmp_path / "bundle", fresh_target, tmp_path / "run")

    for table in MONITORING_TABLES:
        assert _count(fresh_target.db, table) == 0, f"{table} is not empty after restore"
    assert set(MONITORING_TABLES) <= set(report.monitoring_rows)
    assert set(report.monitoring_rows.values()) == {0}
    assert _count(source.db, "metrics") == 3   # still there, and still not ours to move


async def test_restore_refuses_a_target_that_is_not_fresh(source, fresh_target, tmp_path):
    _export(source, tmp_path / "bundle")
    _restore(tmp_path / "bundle", fresh_target, tmp_path / "run1")
    populated = _dump_all(fresh_target.db)

    with pytest.raises(transfer.TargetNotFresh):
        _restore(tmp_path / "bundle", fresh_target, tmp_path / "run2")

    assert _dump_all(fresh_target.db) == populated
    assert fresh_target.secret.read_bytes() == SECRET_BYTES
    assert not (tmp_path / "run2" / transfer.COMPLETE_MARKER).exists()


async def test_restore_refuses_a_target_that_already_started_collecting(source, fresh_target, tmp_path):
    _export(source, tmp_path / "bundle")
    conn = sqlite3.connect(fresh_target.db)
    conn.execute("INSERT INTO metrics (agent_id, timestamp, data) VALUES ('local', 1.0, '{}')")
    conn.commit()
    conn.close()
    before = _dump_all(fresh_target.db)

    with pytest.raises(transfer.TargetNotFresh) as excinfo:
        _restore(tmp_path / "bundle", fresh_target, tmp_path / "run")

    assert "metrics" in str(excinfo.value)
    assert _dump_all(fresh_target.db) == before
    assert fresh_target.secret.read_bytes() == FRESH_SECRET_HEX.encode()


async def test_restore_refuses_a_target_with_a_second_user(source, fresh_target, tmp_path):
    _export(source, tmp_path / "bundle")
    conn = sqlite3.connect(fresh_target.db)
    conn.execute("INSERT INTO users (email, password_hash, created_at) VALUES ('ops@example.com', 'x', 1.0)")
    conn.commit()
    conn.close()
    before = _dump_all(fresh_target.db)

    with pytest.raises(transfer.TargetNotFresh):
        _restore(tmp_path / "bundle", fresh_target, tmp_path / "run")

    assert _dump_all(fresh_target.db) == before


async def test_restore_refuses_a_tampered_manifest(source, fresh_target, tmp_path):
    bundle = tmp_path / "bundle"
    _export(source, bundle)
    before = _dump_all(fresh_target.db)
    manifest_path = bundle / transfer.MANIFEST_NAME
    manifest_path.write_bytes(manifest_path.read_bytes().replace(
        ALICE_HASH_V2.encode(), ALICE_HASH_V1.encode()))

    with pytest.raises(transfer.BundleRejected) as excinfo:
        _restore(bundle, fresh_target, tmp_path / "run")

    for secret in SENSITIVE:
        assert secret not in str(excinfo.value)
    assert _dump_all(fresh_target.db) == before
    assert fresh_target.secret.read_bytes() == FRESH_SECRET_HEX.encode()
    assert not (tmp_path / "run" / transfer.COMPLETE_MARKER).exists()


async def test_restore_refuses_a_manifest_edited_with_consistent_digests(source, fresh_target, tmp_path):
    """Recomputing the per-table digests after an edit is not enough: the
    completion marker pins the manifest bytes as well."""
    bundle = tmp_path / "bundle"
    _export(source, bundle)
    manifest = _read_manifest(bundle)
    users = manifest["tables"]["users"]
    row = next(r for r in users["rows"] if r[0] == BOB)
    row[users["columns"].index("role")] = "admin"
    users["digest"] = transfer.table_digest(users["columns"], users["rows"])
    (bundle / transfer.MANIFEST_NAME).write_bytes(transfer.canonical_bytes(manifest))
    before = _dump_all(fresh_target.db)

    with pytest.raises(transfer.BundleRejected):
        _restore(bundle, fresh_target, tmp_path / "run")

    assert _dump_all(fresh_target.db) == before


async def test_restore_refuses_rows_that_do_not_match_their_table_digest(source, fresh_target, tmp_path):
    """The completion marker was brought up to date after an edit, the table
    digest was not: the per-table digest has to catch it on its own."""
    bundle = tmp_path / "bundle"
    _export(source, bundle)
    manifest = _read_manifest(bundle)
    users = manifest["tables"]["users"]
    row = next(r for r in users["rows"] if r[0] == BOB)
    row[users["columns"].index("is_active")] = 1
    body = transfer.canonical_bytes(manifest)
    (bundle / transfer.MANIFEST_NAME).write_bytes(body)
    marker = json.loads((bundle / transfer.COMPLETE_MARKER).read_bytes())
    marker["manifest_sha256"] = hashlib.sha256(body).hexdigest()
    (bundle / transfer.COMPLETE_MARKER).write_bytes(transfer.canonical_bytes(marker))
    before = _dump_all(fresh_target.db)

    with pytest.raises(transfer.BundleRejected) as excinfo:
        _restore(bundle, fresh_target, tmp_path / "run")

    assert "users" in str(excinfo.value)
    assert _dump_all(fresh_target.db) == before


async def test_restore_refuses_a_tampered_secret_key(source, fresh_target, tmp_path):
    bundle = tmp_path / "bundle"
    _export(source, bundle)
    with open(bundle / transfer.SECRET_KEY_NAME, "ab") as f:
        f.write(b"x")
    before = _dump_all(fresh_target.db)

    with pytest.raises(transfer.BundleRejected) as excinfo:
        _restore(bundle, fresh_target, tmp_path / "run")

    for secret in SENSITIVE:
        assert secret not in str(excinfo.value)
    assert _dump_all(fresh_target.db) == before
    assert fresh_target.secret.read_bytes() == FRESH_SECRET_HEX.encode()
    assert not (tmp_path / "run" / transfer.COMPLETE_MARKER).exists()


async def test_restore_refuses_a_bundle_carrying_a_table_outside_the_allowlist(source, fresh_target, tmp_path):
    bundle = tmp_path / "bundle"
    _export(source, bundle)
    manifest = _read_manifest(bundle)
    manifest["tables"]["metrics"] = {
        "columns": ["id", "agent_id", "timestamp", "data"],
        "rows": [[1, "local", 1700000000.0, METRIC_PAYLOAD]],
    }
    _rewrite_bundle(bundle, manifest)
    before = _dump_all(fresh_target.db)

    with pytest.raises(transfer.BundleRejected):
        _restore(bundle, fresh_target, tmp_path / "run")

    assert _dump_all(fresh_target.db) == before
    assert _count(fresh_target.db, "metrics") == 0


async def test_restore_refuses_symlinks_and_existing_run_paths(source, fresh_target, tmp_path):
    bundle = tmp_path / "bundle"
    _export(source, bundle)
    before = _dump_all(fresh_target.db)

    run = tmp_path / "run"
    run.mkdir()
    (run / "keep").write_text("previous run")
    with pytest.raises(transfer.PathRejected):
        _restore(bundle, fresh_target, run)
    assert sorted(p.name for p in run.iterdir()) == ["keep"]

    link = tmp_path / "target-link.db"
    link.symlink_to(fresh_target.db)
    with pytest.raises(transfer.PathRejected):
        transfer.restore_bundle(bundle_dir=bundle, target_db=link, run_dir=tmp_path / "run2")
    assert not (tmp_path / "run2").exists()

    bundle_link = tmp_path / "bundle-link"
    bundle_link.symlink_to(bundle)
    with pytest.raises(transfer.PathRejected):
        _restore(bundle_link, fresh_target, tmp_path / "run3")
    assert not (tmp_path / "run3").exists()

    fresh_target.secret.unlink()
    fresh_target.secret.symlink_to(tmp_path / "somewhere-else.key")
    with pytest.raises(transfer.PathRejected):
        _restore(bundle, fresh_target, tmp_path / "run4")
    assert not (tmp_path / "run4").exists()
    assert fresh_target.secret.is_symlink()

    assert _dump_all(fresh_target.db) == before


async def test_a_failure_before_the_key_is_installed_leaves_the_target_untouched(source, fresh_target, tmp_path):
    """Rows for `users` are already applied when the `user_host_accounts` row
    below breaks its foreign key; the key has NOT been installed yet at that
    point. Everything rolls back: source and target database are as they
    were, so is the target secret.key (only because the failure came before
    the install), no marker exists, and the refusal carries the retry
    contract. A failure AFTER the key is installed is a different state and
    is covered by test_a_failure_after_the_key_is_installed_is_retry_safe."""
    bundle = tmp_path / "bundle"
    _export(source, bundle)
    manifest = _read_manifest(bundle)
    manifest["tables"]["user_host_accounts"]["rows"].append(["ghost@example.com", "local", "root"])
    _rewrite_bundle(bundle, manifest)
    before_source = _source_hashes(source)
    before_target = _dump_all(fresh_target.db)

    with pytest.raises(transfer.VerificationFailed) as excinfo:
        _restore(bundle, fresh_target, tmp_path / "run")

    assert "Do NOT start the target container" in str(excinfo.value)
    assert _source_hashes(source) == before_source
    assert _dump_all(fresh_target.db) == before_target
    assert fresh_target.secret.read_bytes() == FRESH_SECRET_HEX.encode()
    assert fresh_target.password_file.exists()
    assert not (tmp_path / "run" / transfer.COMPLETE_MARKER).exists()
    assert not (tmp_path / "run" / transfer.REPORT_NAME).exists()
    # The target is still fresh: an intact bundle restores into it afterwards.
    _export(source, tmp_path / "bundle2")
    _restore(tmp_path / "bundle2", fresh_target, tmp_path / "run2")
    assert _dump(fresh_target.db, transfer.PRESERVED_TABLES) == source.expected


class _InjectedAfterInstall:
    """The independent reviewer's failure: the real _install_secret_key runs
    to completion (bytes replaced, digest read back), then the step raises
    before COMMIT. `restore()` puts the real function back for the re-run."""

    def __init__(self, monkeypatch, after=None):
        self.calls = []
        self._monkeypatch = monkeypatch
        self._real = transfer._install_secret_key
        self._after = after

        def install_then_fail(path, data, expected_sha):
            self.calls.append(self._real(path, data, expected_sha))
            if self._after is not None:
                self._after()
            raise OSError("injected after secret replacement, before DB COMMIT")

        monkeypatch.setattr(transfer, "_install_secret_key", install_then_fail)

    def restore(self):
        self._monkeypatch.setattr(transfer, "_install_secret_key", self._real)


async def test_a_failure_after_the_key_is_installed_is_retry_safe(source, fresh_target, tmp_path, monkeypatch):
    """Contract for a failure between the key install and COMMIT: the target
    database is exactly its fresh state, the target secret.key MAY already be
    the bundle key, nothing is written to the run directory, the error says
    how to retry and not to start the container, and a second restore with
    the same bundle and a new run directory completes."""
    bundle = tmp_path / "bundle"
    _export(source, bundle)
    manifest = _read_manifest(bundle)
    before_source = _source_hashes(source)
    fresh_state = _dump_all(fresh_target.db)
    injected = _InjectedAfterInstall(monkeypatch)

    with pytest.raises(transfer.RestoreInterrupted) as excinfo:
        _restore(bundle, fresh_target, tmp_path / "run1")

    assert injected.calls == [True], "fixture: the key was not actually replaced before the failure"
    message = str(excinfo.value)
    assert "Do NOT start the target container" in message
    assert "COMPLETE" in message and "NEW --run-dir" in message
    for secret in SENSITIVE:
        assert secret not in message
    assert isinstance(excinfo.value.__cause__, OSError)
    assert _dump_all(fresh_target.db) == fresh_state, "the target database is not back in its fresh state"
    assert fresh_target.secret.read_bytes() == SECRET_BYTES     # allowed by the contract: already the bundle key
    assert fresh_target.password_file.exists()
    assert sorted(p.name for p in (tmp_path / "run1").iterdir()) == []
    assert _source_hashes(source) == before_source

    injected.restore()
    report = _restore(bundle, fresh_target, tmp_path / "run2")

    assert report.secret_key_installed is False, "the key left by the interrupted run was not accepted as already in place"
    assert _dump(fresh_target.db, transfer.PRESERVED_TABLES) == source.expected
    assert report.row_counts == {t: manifest["tables"][t]["row_count"] for t in transfer.PRESERVED_TABLES}
    assert report.table_digests == {t: manifest["tables"][t]["digest"] for t in transfer.PRESERVED_TABLES}
    assert fresh_target.secret.read_bytes() == SECRET_BYTES
    assert not fresh_target.password_file.exists()
    assert (tmp_path / "run2" / transfer.COMPLETE_MARKER).exists()
    assert _source_hashes(source) == before_source


async def test_cli_reports_an_interrupted_restore_without_printing_a_secret(source, fresh_target, tmp_path, monkeypatch, capsys):
    bundle = tmp_path / "bundle"
    _export(source, bundle)
    injected = _InjectedAfterInstall(monkeypatch)

    code = transfer.main(["restore", "--bundle", str(bundle), "--target-db", str(fresh_target.db),
                          "--run-dir", str(tmp_path / "run1")])
    captured = capsys.readouterr()

    assert code == 2
    assert transfer.RestoreInterrupted.code in captured.err
    assert "Do NOT start the target container" in captured.err
    assert captured.out == ""
    for secret in SENSITIVE:
        assert secret not in captured.out + captured.err

    injected.restore()
    code = transfer.main(["restore", "--bundle", str(bundle), "--target-db", str(fresh_target.db),
                          "--run-dir", str(tmp_path / "run2")])
    captured = capsys.readouterr()

    assert code == 0, captured.err
    for secret in SENSITIVE:
        assert secret not in captured.out + captured.err
    assert json.loads(captured.out)["secret_key_installed"] is False
    assert _dump(fresh_target.db, transfer.PRESERVED_TABLES) == source.expected


async def test_a_failed_rollback_is_not_reported_as_retry_safe(source, fresh_target, tmp_path, monkeypatch):
    """The connection dies under the restore after the key is installed, so
    ROLLBACK cannot run and nothing about the target may be claimed: a
    separate typed refusal asks for manual verification and gives no retry
    recipe."""
    bundle = tmp_path / "bundle"
    _export(source, bundle)
    connections = []
    real_connect = sqlite3.connect

    def recording_connect(*args, **kwargs):
        conn = real_connect(*args, **kwargs)
        connections.append(conn)
        return conn

    monkeypatch.setattr(transfer.sqlite3, "connect", recording_connect)
    _InjectedAfterInstall(monkeypatch, after=lambda: connections[-1].close())

    with pytest.raises(transfer.ManualVerificationRequired) as excinfo:
        _restore(bundle, fresh_target, tmp_path / "run")

    message = str(excinfo.value)
    assert "MANUAL VERIFICATION REQUIRED" in message
    assert "Do NOT start the target container" in message
    assert "NEW --run-dir" not in message, "a state that was not confirmed must not come with a retry recipe"
    assert not isinstance(excinfo.value, transfer.RestoreInterrupted)
    for secret in SENSITIVE:
        assert secret not in message
    assert not (tmp_path / "run" / transfer.COMPLETE_MARKER).exists()
    assert not (tmp_path / "run" / transfer.REPORT_NAME).exists()


async def test_a_rollback_error_is_manual_verification_even_if_the_target_reads_fresh(source, fresh_target, tmp_path, monkeypatch):
    """The transaction is already gone when the restore tries to roll it back
    (here: ended underneath it), so ROLLBACK itself errors. The target may
    well read back fresh — but a rollback that did not behave means the
    restore's picture of the connection is wrong, and that is reported as
    manual verification, never as retry-safe."""
    bundle = tmp_path / "bundle"
    _export(source, bundle)
    connections = []
    real_connect = sqlite3.connect

    def recording_connect(*args, **kwargs):
        conn = real_connect(*args, **kwargs)
        connections.append(conn)
        return conn

    monkeypatch.setattr(transfer.sqlite3, "connect", recording_connect)
    _InjectedAfterInstall(monkeypatch, after=lambda: connections[-1].execute("ROLLBACK"))
    fresh_state = _dump_all(fresh_target.db)

    with pytest.raises(transfer.ManualVerificationRequired) as excinfo:
        _restore(bundle, fresh_target, tmp_path / "run")

    message = str(excinfo.value)
    assert "rollback failed" in message
    assert "MANUAL VERIFICATION REQUIRED" in message and "NEW --run-dir" not in message
    assert _dump_all(fresh_target.db) == fresh_state     # true here, and still not claimed
    assert not (tmp_path / "run" / transfer.COMPLETE_MARKER).exists()
    assert not (tmp_path / "run" / transfer.REPORT_NAME).exists()


async def test_an_unconfirmed_fresh_state_is_not_reported_as_retry_safe(source, fresh_target, tmp_path, monkeypatch):
    """ROLLBACK succeeds but the target cannot be proven fresh afterwards
    (the re-check itself fails): still manual verification, not retry-safe."""
    bundle = tmp_path / "bundle"
    _export(source, bundle)
    real_assert_fresh = transfer._assert_fresh
    checks = []

    def assert_fresh_then_fail(conn):
        checks.append(1)
        if len(checks) == 1:
            return real_assert_fresh(conn)
        raise sqlite3.OperationalError("injected: the target could not be read back")

    monkeypatch.setattr(transfer, "_assert_fresh", assert_fresh_then_fail)
    _InjectedAfterInstall(monkeypatch)

    with pytest.raises(transfer.ManualVerificationRequired) as excinfo:
        _restore(bundle, fresh_target, tmp_path / "run")

    assert len(checks) == 2, "the fresh state was never re-checked after the rollback"
    assert "MANUAL VERIFICATION REQUIRED" in str(excinfo.value)
    assert "NEW --run-dir" not in str(excinfo.value)
    assert not (tmp_path / "run" / transfer.COMPLETE_MARKER).exists()
    assert not (tmp_path / "run" / transfer.REPORT_NAME).exists()


class _DirFsyncFailure:
    """Fail `_fsync_dir` for ONE directory while armed: the parent-directory
    fsync right after the key rename (first run), or the durability re-check
    of an already-present identical key (a re-run)."""

    def __init__(self, monkeypatch, directory: Path):
        self.directory = directory
        self.armed = True
        self.hits = 0
        real = transfer._fsync_dir

        def fsync_dir(path):
            if self.armed and Path(path) == self.directory:
                self.hits += 1
                raise OSError("injected: parent directory fsync failed")
            return real(path)

        monkeypatch.setattr(transfer, "_fsync_dir", fsync_dir)


class _SyscallLog:
    """Record fchmod/fsync calls on the target key file and its directory (by
    inode), each with what a concurrent reader of the target database sees at
    that moment: 1 user means nothing has been committed yet."""

    def __init__(self, monkeypatch, target: Target):
        self.events = []
        key_ino = os.stat(target.secret).st_ino
        dir_ino = os.stat(target.data_dir).st_ino
        real_fsync, real_fchmod = os.fsync, os.fchmod

        def note(name, fd):
            ino = os.fstat(fd).st_ino
            if ino in (key_ino, dir_ino):
                self.events.append((name, "key" if ino == key_ino else "dir", _count(target.db, "users")))

        def fsync(fd):
            note("fsync", fd)
            return real_fsync(fd)

        def fchmod(fd, mode):
            note("fchmod", fd)
            return real_fchmod(fd, mode)

        monkeypatch.setattr(os, "fsync", fsync)
        monkeypatch.setattr(os, "fchmod", fchmod)


async def test_an_identical_key_left_by_an_interrupted_run_is_made_durable_before_commit(
        source, fresh_target, tmp_path, monkeypatch):
    """First run: the key is renamed into place but the parent-directory fsync
    fails, so the run stops before COMMIT. Second run: the bytes already match
    — which proves nothing about whether that rename is durable — so the
    same-key branch must fchmod and fsync the file, fsync the directory, and
    only then may the database COMMIT."""
    bundle = tmp_path / "bundle"
    _export(source, bundle)
    manifest = _read_manifest(bundle)
    fresh_state = _dump_all(fresh_target.db)
    before_source = _source_hashes(source)
    failure = _DirFsyncFailure(monkeypatch, fresh_target.data_dir)

    with pytest.raises(transfer.RestoreInterrupted) as excinfo:
        _restore(bundle, fresh_target, tmp_path / "run1")

    assert failure.hits == 1, "fixture: the parent-directory fsync after the rename was not the failure"
    assert isinstance(excinfo.value.__cause__, OSError)
    assert _dump_all(fresh_target.db) == fresh_state
    assert fresh_target.secret.read_bytes() == SECRET_BYTES     # renamed into place; durability unproven
    assert not (tmp_path / "run1" / transfer.COMPLETE_MARKER).exists()
    for secret in SENSITIVE:
        assert secret not in str(excinfo.value)

    failure.armed = False
    log = _SyscallLog(monkeypatch, fresh_target)
    report = _restore(bundle, fresh_target, tmp_path / "run2")

    assert report.secret_key_installed is False
    assert [event[:2] for event in log.events] == [("fchmod", "key"), ("fsync", "key"), ("fsync", "dir")], log.events
    assert all(users_seen == 1 for _n, _w, users_seen in log.events), \
        "the database committed before the key was made durable"
    assert _count(fresh_target.db, "users") == 4                 # committed only afterwards
    assert _dump(fresh_target.db, transfer.PRESERVED_TABLES) == source.expected
    assert report.table_digests == {t: manifest["tables"][t]["digest"] for t in transfer.PRESERVED_TABLES}
    assert fresh_target.secret.read_bytes() == SECRET_BYTES
    assert _mode(fresh_target.secret) == 0o600
    assert (tmp_path / "run2" / transfer.COMPLETE_MARKER).exists()
    assert _source_hashes(source) == before_source


@pytest.mark.parametrize("failing", ["parent-directory", "file"])
async def test_a_failed_durability_recheck_of_an_identical_key_is_not_a_success(
        source, fresh_target, tmp_path, monkeypatch, capsys, failing):
    """Run 1 stops at the parent-directory fsync after the key rename. Run 2
    finds identical bytes but its own durability re-check fails (the
    directory fsync again, or the file fsync): no COMMIT, no report, no
    COMPLETE, database still fresh. Run 3 completes, through the CLI."""
    bundle = tmp_path / "bundle"
    _export(source, bundle)
    manifest = _read_manifest(bundle)
    fresh_state = _dump_all(fresh_target.db)
    before_source = _source_hashes(source)
    real_fsync = os.fsync
    failure = _DirFsyncFailure(monkeypatch, fresh_target.data_dir)

    with pytest.raises(transfer.RestoreInterrupted):
        _restore(bundle, fresh_target, tmp_path / "run1")
    assert failure.hits == 1
    assert fresh_target.secret.read_bytes() == SECRET_BYTES

    if failing == "file":
        failure.armed = False
        key_ino = os.stat(fresh_target.secret).st_ino

        def fsync_except_the_key(fd):
            if os.fstat(fd).st_ino == key_ino:
                raise OSError("injected: file fsync failed during the durability re-check")
            return real_fsync(fd)

        monkeypatch.setattr(os, "fsync", fsync_except_the_key)

    with pytest.raises(transfer.RestoreInterrupted) as excinfo:
        _restore(bundle, fresh_target, tmp_path / "run2")

    assert isinstance(excinfo.value.__cause__, OSError)
    assert _dump_all(fresh_target.db) == fresh_state, "the database committed although durability was not confirmed"
    assert fresh_target.secret.read_bytes() == SECRET_BYTES
    assert sorted(p.name for p in (tmp_path / "run2").iterdir()) == []
    for secret in SENSITIVE:
        assert secret not in str(excinfo.value)

    failure.armed = False
    monkeypatch.setattr(os, "fsync", real_fsync)
    code = transfer.main(["restore", "--bundle", str(bundle), "--target-db", str(fresh_target.db),
                          "--run-dir", str(tmp_path / "run3")])
    captured = capsys.readouterr()

    assert code == 0, captured.err
    report = json.loads(captured.out)
    assert report["secret_key_installed"] is False
    assert report["table_digests"] == {t: manifest["tables"][t]["digest"] for t in transfer.PRESERVED_TABLES}
    assert set(report["monitoring_rows"].values()) == {0}
    assert _dump(fresh_target.db, transfer.PRESERVED_TABLES) == source.expected
    assert fresh_target.secret.read_bytes() == SECRET_BYTES
    assert (tmp_path / "run3" / transfer.COMPLETE_MARKER).exists()
    for secret in SENSITIVE:
        assert secret not in captured.out + captured.err
    assert _source_hashes(source) == before_source


async def test_bundle_and_run_directories_are_private(source, fresh_target, tmp_path):
    bundle, run = tmp_path / "bundle", tmp_path / "run"
    _export(source, bundle)
    _restore(bundle, fresh_target, run)

    for directory in (bundle, run):
        assert _mode(directory) == 0o700, directory
        entries = sorted(p.name for p in directory.iterdir())
        assert entries and not any(name.startswith(".") for name in entries), entries
        for entry in directory.iterdir():
            assert entry.is_file() and not entry.is_symlink()
            assert _mode(entry) == 0o600, entry
    assert sorted(p.name for p in bundle.iterdir()) == sorted(
        [transfer.MANIFEST_NAME, transfer.SECRET_KEY_NAME, transfer.COMPLETE_MARKER])
    assert sorted(p.name for p in run.iterdir()) == sorted([transfer.REPORT_NAME, transfer.COMPLETE_MARKER])


# ── schema oracle ────────────────────────────────────


async def test_expected_schema_is_what_init_db_creates(fresh_target):
    assert transfer.CUTOVER_KEY == db._AGG_CUTOVER_KEY
    conn = sqlite3.connect(fresh_target.db)
    try:
        transfer.verify_schema(conn, "target")     # the real schema passes
        conn.execute("ALTER TABLE users ADD COLUMN nickname TEXT")
        with pytest.raises(transfer.SchemaMismatch) as excinfo:
            transfer.verify_schema(conn, "target")
        assert "users" in str(excinfo.value)
    finally:
        conn.close()


def test_export_refuses_a_source_whose_schema_drifted(source, tmp_path):
    # An older users table (no tokens_valid_after) — the export must not guess.
    conn = sqlite3.connect(tmp_path / "drifted.db")
    for stmt in OLD_BUILD_DDL:
        conn.execute(stmt.replace(",\n        tokens_valid_after REAL NOT NULL DEFAULT 0", ""))
    conn.commit()
    conn.close()
    shutil.copyfile(source.secret, tmp_path / "secret.key")

    with pytest.raises(transfer.SchemaMismatch):
        transfer.export_bundle(source_db=tmp_path / "drifted.db", bundle_dir=tmp_path / "bundle")
    assert not (tmp_path / "bundle").exists()


# ── command line ─────────────────────────────────────


def _cli(*args, cwd=BACKEND_DIR):
    env = {k: v for k, v in os.environ.items() if not k.startswith("GLASSOPS_")}
    return subprocess.run(
        [sys.executable, "-m", "app.account_state_transfer", *map(str, args)],
        cwd=cwd, env=env, capture_output=True, text=True, timeout=120,
    )


def test_importing_the_module_does_not_load_the_app_settings():
    """The utility runs on a host, outside the container: importing it must not
    resolve GLASSOPS_SECRET_KEY or generate a secret.key as app.config does."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GLASSOPS_")}
    proc = subprocess.run(
        [sys.executable, "-c",
         "import sys, app.account_state_transfer; "
         "print(sorted(m for m in sys.modules if m == 'app' or m.startswith('app.')))"],
        cwd=BACKEND_DIR, env=env, capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "['app', 'app.account_state_transfer']"


async def test_cli_round_trip_never_prints_a_secret(source, fresh_target, tmp_path):
    bundle, run = tmp_path / "bundle", tmp_path / "run"

    exported = _cli("export", "--source-db", source.db, "--bundle", bundle)
    assert exported.returncode == 0, exported.stderr
    restored = _cli("restore", "--bundle", bundle, "--target-db", fresh_target.db, "--run-dir", run)
    assert restored.returncode == 0, restored.stderr

    everything = exported.stdout + exported.stderr + restored.stdout + restored.stderr
    for secret in SENSITIVE:
        assert secret not in everything, "a secret reached stdout/stderr"
    export_report = json.loads(exported.stdout)
    restore_report = json.loads(restored.stdout)
    digest = hashlib.sha256(SECRET_BYTES).hexdigest()
    assert export_report["secret_key_sha256"] == restore_report["secret_key_sha256"] == digest
    assert export_report["row_counts"] == restore_report["row_counts"]
    assert export_report["table_digests"] == restore_report["table_digests"]
    assert fresh_target.secret.read_bytes() == SECRET_BYTES
    assert _dump(fresh_target.db, transfer.PRESERVED_TABLES) == source.expected


async def test_cli_exit_codes_distinguish_refusals(source, fresh_target, tmp_path):
    bundle = tmp_path / "bundle"
    assert _cli("export", "--source-db", source.db, "--bundle", bundle).returncode == 0

    again = _cli("export", "--source-db", source.db, "--bundle", bundle)
    assert again.returncode == 2
    assert transfer.PathRejected.code in again.stderr

    (bundle / transfer.COMPLETE_MARKER).unlink()
    incomplete = _cli("restore", "--bundle", bundle, "--target-db", fresh_target.db, "--run-dir", tmp_path / "run")
    assert incomplete.returncode == 2
    assert transfer.PathRejected.code in incomplete.stderr or transfer.BundleRejected.code in incomplete.stderr
    assert _count(fresh_target.db, "users") == 1
    for secret in SENSITIVE:
        assert secret not in again.stderr + incomplete.stderr
