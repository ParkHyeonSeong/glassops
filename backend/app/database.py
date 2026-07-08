"""SQLite database — metrics + users."""

import json
import logging
import os
import time

import aiosqlite
import bcrypt

from app.config import settings

logger = logging.getLogger("glassops.db")

_db_path = settings.db_path
_conn: aiosqlite.Connection | None = None


def _initial_admin_pw_file() -> str:
    """Path of the one-time initial-admin password file (data dir, alongside the DB)."""
    return os.path.join(os.path.dirname(_db_path) or ".", "initial_admin_password")


async def clear_initial_admin_password_file() -> None:
    """Best-effort removal of the one-time initial-admin password file once the admin
    has changed their password, so the plaintext credential doesn't linger on disk."""
    try:
        os.remove(_initial_admin_pw_file())
        logger.info("Removed initial admin password file after password change")
    except FileNotFoundError:
        pass
    except OSError:
        logger.warning("Could not remove initial admin password file", exc_info=True)


async def get_db() -> aiosqlite.Connection:
    global _conn
    if _conn is None:
        os.makedirs(os.path.dirname(_db_path) or ".", exist_ok=True)
        _conn = await aiosqlite.connect(_db_path)
        _conn.row_factory = aiosqlite.Row
        await _conn.execute("PRAGMA journal_mode=WAL")
    return _conn


async def close_db() -> None:
    global _conn
    if _conn is not None:
        await _conn.close()
        _conn = None


async def init_db() -> None:
    db = await get_db()

    await db.execute("""
        CREATE TABLE IF NOT EXISTS metrics (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            agent_id TEXT NOT NULL,
            timestamp REAL NOT NULL,
            data TEXT NOT NULL
        )
    """)
    await db.execute("""
        CREATE INDEX IF NOT EXISTS idx_metrics_agent_ts
        ON metrics (agent_id, timestamp)
    """)

    # Downsampled metrics for long-term storage
    await db.execute("""
        CREATE TABLE IF NOT EXISTS metrics_downsampled (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            agent_id TEXT NOT NULL,
            timestamp REAL NOT NULL,
            resolution TEXT NOT NULL,
            data TEXT NOT NULL
        )
    """)
    await db.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_metrics_ds_unique
        ON metrics_downsampled (agent_id, resolution, timestamp)
    """)

    await db.execute("""
        CREATE TABLE IF NOT EXISTS runtime_config (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
    """)

    await db.execute("""
        CREATE TABLE IF NOT EXISTS token_blacklist (
            token_hash TEXT PRIMARY KEY,
            expires_at REAL NOT NULL
        )
    """)

    await db.execute("""
        CREATE TABLE IF NOT EXISTS alert_config (
            id INTEGER PRIMARY KEY DEFAULT 1,
            config TEXT NOT NULL
        )
    """)

    await db.execute("""
        CREATE TABLE IF NOT EXISTS users (
            email TEXT PRIMARY KEY,
            password_hash TEXT NOT NULL,
            totp_secret TEXT,
            totp_enabled INTEGER DEFAULT 0,
            must_change_password INTEGER DEFAULT 0,
            created_at REAL NOT NULL,
            role TEXT NOT NULL DEFAULT 'user',
            is_active INTEGER NOT NULL DEFAULT 1,
            tokens_valid_after REAL NOT NULL DEFAULT 0
        )
    """)

    # Migrate older installs that lack role / is_active / tokens_valid_after.
    cursor = await db.execute("PRAGMA table_info(users)")
    existing_cols = {row["name"] for row in await cursor.fetchall()}
    if "role" not in existing_cols:
        await db.execute("ALTER TABLE users ADD COLUMN role TEXT NOT NULL DEFAULT 'user'")
    if "is_active" not in existing_cols:
        await db.execute("ALTER TABLE users ADD COLUMN is_active INTEGER NOT NULL DEFAULT 1")
    if "tokens_valid_after" not in existing_cols:
        await db.execute("ALTER TABLE users ADD COLUMN tokens_valid_after REAL NOT NULL DEFAULT 0")

    # Promote the earliest-created user to admin if no admin exists yet.
    cursor = await db.execute("SELECT COUNT(*) FROM users WHERE role = 'admin'")
    admin_count = (await cursor.fetchone())[0]
    if admin_count == 0:
        cursor = await db.execute("SELECT email FROM users ORDER BY created_at ASC LIMIT 1")
        first = await cursor.fetchone()
        if first:
            await db.execute("UPDATE users SET role = 'admin' WHERE email = ?", (first["email"],))
            logger.info("Promoted %s to admin (migration)", first["email"])

    # User ↔ host (agent) account mappings — controls per-user terminal access per host.
    await db.execute("""
        CREATE TABLE IF NOT EXISTS user_host_accounts (
            user_email TEXT NOT NULL,
            agent_id TEXT NOT NULL,
            host_user TEXT NOT NULL,
            PRIMARY KEY (user_email, agent_id),
            FOREIGN KEY (user_email) REFERENCES users(email) ON DELETE CASCADE
        )
    """)

    # Audit log — attributable record of host-root-equivalent actions and the
    # account lifecycle, persisted across restarts (survives container log rotation).
    await db.execute("""
        CREATE TABLE IF NOT EXISTS audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp REAL NOT NULL,
            user_email TEXT NOT NULL,
            action TEXT NOT NULL,
            agent_id TEXT NOT NULL DEFAULT '',
            detail TEXT NOT NULL DEFAULT '{}'
        )
    """)
    await db.execute("CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_log (timestamp)")

    await db.execute("""
        CREATE TABLE IF NOT EXISTS net_conn_events (
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
        )
    """)
    await db.execute("CREATE INDEX IF NOT EXISTS idx_nce_agent_ts ON net_conn_events (agent_id, ts)")
    await db.execute("CREATE INDEX IF NOT EXISTS idx_nce_raddr ON net_conn_events (agent_id, raddr)")
    await db.execute("""
        CREATE TABLE IF NOT EXISTS net_flow_rollup (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            agent_id TEXT NOT NULL,
            ts REAL NOT NULL,
            data TEXT NOT NULL
        )
    """)
    await db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_nfr_unique ON net_flow_rollup (agent_id, ts)")

    # Create default admin if no users exist at all
    cursor = await db.execute("SELECT COUNT(*) FROM users")
    user_count = (await cursor.fetchone())[0]

    if user_count == 0:
        import secrets
        default_email = os.getenv("GLASSOPS_ADMIN_EMAIL", "admin@glassops.local")
        env_pw = os.getenv("GLASSOPS_ADMIN_PASSWORD", "")

        if env_pw:
            # Explicit password set by admin
            password = env_pw
            must_change = False
        else:
            # No password configured — generate a random one-time password and
            # write it to a 0600 file in the data dir instead of the logs (logs are
            # captured by supervisord/docker and retained). Operator reads it once,
            # is forced to change it on first login, then deletes the file.
            password = secrets.token_urlsafe(16)
            must_change = True
            pw_file = _initial_admin_pw_file()
            try:
                with open(pw_file, "w") as f:
                    f.write(f"email: {default_email}\npassword: {password}\n")
                os.chmod(pw_file, 0o600)
                logger.warning("Initial admin password generated → %s "
                               "(read it, log in, change immediately, then delete the file)", pw_file)
            except OSError as e:
                # Never log the plaintext credential (logs are captured/retained).
                # The data dir already holds the DB we just wrote, so this is
                # near-impossible; fail closed and tell the operator how to recover.
                raise RuntimeError(
                    f"Could not write initial admin password file ({pw_file}): {e}. "
                    "Fix the data directory permissions, or set GLASSOPS_ADMIN_PASSWORD "
                    "and restart."
                ) from e

        pw_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
        await db.execute(
            "INSERT INTO users (email, password_hash, must_change_password, created_at, role, is_active) VALUES (?, ?, ?, ?, 'admin', 1)",
            (default_email, pw_hash, 1 if must_change else 0, time.time()),
        )
        logger.info("Admin user created: %s", default_email)

    await db.commit()


# ── Metrics ──────────────────────────────────────────


async def store_metric(agent_id: str, timestamp: float, data: dict) -> None:
    db = await get_db()
    await db.execute(
        "INSERT INTO metrics (agent_id, timestamp, data) VALUES (?, ?, ?)",
        (agent_id, timestamp, json.dumps(data)),
    )
    await db.commit()


async def get_recent_metrics(agent_id: str, limit: int = 60) -> list[dict]:
    db = await get_db()
    cursor = await db.execute(
        "SELECT timestamp, data FROM metrics WHERE agent_id = ? ORDER BY timestamp DESC LIMIT ?",
        (agent_id, limit),
    )
    rows = await cursor.fetchall()
    result = []
    for row in rows:
        entry = json.loads(row["data"])
        entry["timestamp"] = row["timestamp"]
        result.append(entry)
    return list(reversed(result))


async def get_metrics_range(
    agent_id: str, start: float, end: float, max_points: int = 500
) -> list[dict]:
    """Get metrics between start and end timestamps. Auto-selects resolution."""
    db = await get_db()
    duration = end - start

    # < 1 hour: raw data
    if duration <= 3600:
        cursor = await db.execute(
            "SELECT timestamp, data FROM metrics WHERE agent_id = ? AND timestamp BETWEEN ? AND ? ORDER BY timestamp",
            (agent_id, start, end),
        )
    # 1h - 24h: 1min downsampled
    elif duration <= 86400:
        cursor = await db.execute(
            "SELECT timestamp, data FROM metrics_downsampled WHERE agent_id = ? AND resolution = '1m' AND timestamp BETWEEN ? AND ? ORDER BY timestamp",
            (agent_id, start, end),
        )
    # > 24h: 5min downsampled
    else:
        cursor = await db.execute(
            "SELECT timestamp, data FROM metrics_downsampled WHERE agent_id = ? AND resolution = '5m' AND timestamp BETWEEN ? AND ? ORDER BY timestamp",
            (agent_id, start, end),
        )

    rows = await cursor.fetchall()
    result = []
    # Thin out if too many points
    step = max(1, len(rows) // max_points)
    for i, row in enumerate(rows):
        if i % step == 0:
            entry = json.loads(row["data"])
            entry["timestamp"] = row["timestamp"]
            result.append(entry)
    return result


async def get_container_history(
    agent_id: str, container_name: str, start: float, end: float, max_points: int = 500
) -> list[dict]:
    """Time-series for a single container by name. Walks the same auto-resolution
    tables as get_metrics_range, but extracts only the named container's cpu/mem
    so the response stays small."""
    snapshots = await get_metrics_range(agent_id, start, end, max_points=max_points * 4)
    result = []
    for snap in snapshots:
        ts = snap.get("timestamp")
        for c in snap.get("containers") or []:
            if c.get("name") == container_name:
                gpu = c.get("gpu") if isinstance(c.get("gpu"), dict) else None
                point = {
                    "t": ts,
                    "cpu": float(c.get("cpu_percent", 0) or 0),
                    "mem": float(c.get("mem_usage", 0) or 0),
                    "mem_limit": float(c.get("mem_limit", 0) or 0),
                    "vram": float(gpu.get("vram_bytes", 0) or 0) if gpu else 0.0,
                    "gpu_util": float(gpu.get("gpu_util", 0) or 0) if gpu else 0.0,
                    "gpu_present": bool(gpu) or bool(c.get("gpu_reserved")),
                }
                result.append(point)
                break
    # Thin out if the underlying fetch was generous
    if len(result) > max_points:
        step = max(1, len(result) // max_points)
        result = [p for i, p in enumerate(result) if i % step == 0]
    return result


async def downsample_metrics(resolution_seconds: int, resolution_label: str) -> int:
    """Aggregate raw metrics into downsampled buckets."""
    db = await get_db()
    now = time.time()

    # Find the latest downsampled timestamp for this resolution
    cursor = await db.execute(
        "SELECT MAX(timestamp) FROM metrics_downsampled WHERE resolution = ?",
        (resolution_label,),
    )
    row = await cursor.fetchone()
    last_ds = row[0] if row and row[0] else 0

    # Start after last downsampled bucket to avoid duplicates
    start = max(last_ds + resolution_seconds, now - 7 * 86400) if last_ds else now - 7 * 86400

    # Get raw metrics in buckets
    cursor = await db.execute(
        "SELECT agent_id, timestamp, data FROM metrics WHERE timestamp > ? ORDER BY timestamp",
        (start,),
    )
    rows = await cursor.fetchall()

    if not rows:
        return 0

    # Group by (agent_id, bucket)
    buckets: dict[tuple[str, int], list[dict]] = {}
    for row in rows:
        aid = row["agent_id"]
        bucket = int(row["timestamp"] // resolution_seconds) * resolution_seconds
        key = (aid, bucket)
        if key not in buckets:
            buckets[key] = []
        buckets[key].append(json.loads(row["data"]))

    # Average each bucket and insert
    count = 0
    for (aid, bucket_ts), entries in buckets.items():
        if bucket_ts > now - resolution_seconds:
            continue  # Skip current incomplete bucket

        avg = _average_metrics(entries)
        if avg:
            await db.execute(
                "INSERT OR REPLACE INTO metrics_downsampled (agent_id, timestamp, resolution, data) VALUES (?, ?, ?, ?)",
                (aid, float(bucket_ts), resolution_label, json.dumps(avg)),
            )
            count += 1

    await db.commit()
    return count


def _average_metrics(entries: list[dict]) -> dict | None:
    """Average numeric fields from a list of metric snapshots."""
    if not entries:
        return None

    # Use first entry as template, average CPU/MEM/Disk
    result = json.loads(json.dumps(entries[0]))  # deep copy
    n = len(entries)

    try:
        result["cpu"]["percent_total"] = sum(e.get("cpu", {}).get("percent_total", 0) for e in entries) / n
        result["memory"]["percent"] = sum(e.get("memory", {}).get("percent", 0) for e in entries) / n
        result["disk"]["percent"] = sum(e.get("disk", {}).get("percent", 0) for e in entries) / n

        # Average per-core
        cores = len(result.get("cpu", {}).get("percent_per_core", []))
        if cores:
            for ci in range(cores):
                result["cpu"]["percent_per_core"][ci] = sum(
                    e.get("cpu", {}).get("percent_per_core", [0] * cores)[ci] for e in entries
                ) / n

        # Average GPU metrics per device
        gpus = result.get("gpu", [])
        for gi, gpu in enumerate(gpus):
            for field in ("gpu_util", "mem_util", "temperature", "power_watts", "clock_sm_mhz"):
                vals = [e.get("gpu", [{}] * (gi + 1))[gi].get(field, 0) for e in entries if len(e.get("gpu", [])) > gi]
                gpu[field] = sum(vals) / len(vals) if vals else 0
            # Remove per-snapshot process data from downsampled
            gpu.pop("processes", None)

        # Aggregate per-container CPU/Mem by name. Containers can come and go within
        # a bucket; keying by name keeps history continuous across restart/recreate
        # while id changes. Latest non-zero mem_limit wins (limits can be updated).
        agg: dict[str, dict] = {}
        for e in entries:
            for c in e.get("containers") or []:
                name = c.get("name")
                if not name:
                    continue
                s = agg.get(name)
                if s is None:
                    s = {
                        "name": name,
                        "id": c.get("id", ""),
                        "image": c.get("image", ""),
                        "status": c.get("status", ""),
                        "state": c.get("state", ""),
                        "ports": c.get("ports", []),
                        "cpu_sum": 0.0,
                        "mem_sum": 0.0,
                        "samples": 0,
                        "mem_limit": c.get("mem_limit", 0),
                        "gpu_vram_sum": 0.0,
                        "gpu_util_sum": 0.0,
                        "gpu_vram_samples": 0,
                        "gpu_util_samples": 0,
                        "gpu_seen": False,
                    }
                    agg[name] = s
                s["cpu_sum"] += float(c.get("cpu_percent", 0) or 0)
                s["mem_sum"] += float(c.get("mem_usage", 0) or 0)
                s["samples"] += 1
                # Use the most recent metadata so the UI shows current status/image.
                s["status"] = c.get("status", s["status"])
                s["state"] = c.get("state", s["state"])
                s["image"] = c.get("image", s["image"])
                s["id"] = c.get("id", s["id"])
                s["ports"] = c.get("ports", s["ports"])
                ml = c.get("mem_limit", 0) or 0
                if ml > 0:
                    s["mem_limit"] = ml
                # Average VRAM/SM only across samples where the container actually held
                # GPU memory or was running compute — averaging zeros into the divisor
                # would understate usage for workloads that allocate intermittently.
                # gpu_reserved=true with zero util/vram still creates the field so the
                # downsampled history shows the idle period instead of dropping out.
                gpu = c.get("gpu")
                if isinstance(gpu, dict):
                    vram = float(gpu.get("vram_bytes", 0) or 0)
                    util = float(gpu.get("gpu_util", 0) or 0)
                    if vram > 0:
                        s["gpu_vram_sum"] += vram
                        s["gpu_vram_samples"] = s.get("gpu_vram_samples", 0) + 1
                    if util > 0:
                        s["gpu_util_sum"] += util
                        s["gpu_util_samples"] = s.get("gpu_util_samples", 0) + 1
                    s["gpu_seen"] = True
        out_containers = []
        for s in agg.values():
            entry = {
                "id": s["id"],
                "name": s["name"],
                "image": s["image"],
                "status": s["status"],
                "state": s["state"],
                "ports": s["ports"],
                "cpu_percent": s["cpu_sum"] / s["samples"] if s["samples"] else 0,
                "mem_usage": s["mem_sum"] / s["samples"] if s["samples"] else 0,
                "mem_limit": s["mem_limit"],
            }
            if s["gpu_seen"]:
                entry["gpu"] = {
                    "vram_bytes": s["gpu_vram_sum"] / s["gpu_vram_samples"] if s["gpu_vram_samples"] else 0,
                    "gpu_util": s["gpu_util_sum"] / s["gpu_util_samples"] if s["gpu_util_samples"] else 0,
                }
            out_containers.append(entry)
        result["containers"] = out_containers
    except (KeyError, IndexError, TypeError):
        pass

    # Drop heavy fields for downsampled data
    result.pop("processes", None)
    result.pop("network", None)

    return result


async def cleanup_old_metrics(max_age_hours: int = 1) -> int:
    """Delete raw metrics older than max_age_hours. Downsampled data kept for 7 days."""
    now = time.time()
    db = await get_db()

    # Raw: keep last 1 hour only (downsampled covers the rest). Also drop any row
    # timestamped in the future: the ingest path clamps forged timestamps (LOGIC-08),
    # but this is the defense-in-depth net for rows persisted before that landed, or
    # if the clamp is ever bypassed — otherwise an age-based cutoff never reaches them.
    raw_cutoff = now - (max_age_hours * 3600)
    future_cutoff = now + 300  # matches the ingest clamp's +300s tolerance
    cursor = await db.execute(
        "DELETE FROM metrics WHERE timestamp < ? OR timestamp > ?",
        (raw_cutoff, future_cutoff),
    )
    raw_deleted = cursor.rowcount

    # Downsampled: keep 7 days (and likewise prune any future-dated rows)
    ds_cutoff = now - (7 * 86400)
    await db.execute(
        "DELETE FROM metrics_downsampled WHERE timestamp < ? OR timestamp > ?",
        (ds_cutoff, future_cutoff),
    )

    await db.commit()
    return raw_deleted


async def store_net_audit(agent_id: str, ts: float, events: list, rollups: list) -> None:
    """Persist connection events + minute rollups. Metadata only — no payloads."""
    db = await get_db()
    if events:
        await db.executemany(
            "INSERT INTO net_conn_events "
            "(agent_id, ts, event, proto, laddr, lport, raddr, rport, status, pid, pname, duration) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            [(agent_id, float(e.get("ts", ts)), str(e.get("event", "")), str(e.get("proto", "")),
              e.get("laddr"), e.get("lport"), e.get("raddr"), e.get("rport"),
              e.get("status"), e.get("pid"), e.get("pname"), e.get("duration"))
             for e in events],
        )
    for r in rollups:
        await db.execute(
            "INSERT OR REPLACE INTO net_flow_rollup (agent_id, ts, data) VALUES (?,?,?)",
            (agent_id, float(r.get("ts", ts)), json.dumps(
                {"interfaces": r.get("interfaces", []), "top_talkers": r.get("top_talkers", [])})),
        )
    await db.commit()


async def cleanup_net_audit(event_days: int = 7, rollup_days: int = 30) -> int:
    now = time.time()
    future = now + 300
    db = await get_db()
    cur = await db.execute(
        "DELETE FROM net_conn_events WHERE ts < ? OR ts > ?",
        (now - event_days * 86400, future),
    )
    deleted = cur.rowcount
    await db.execute(
        "DELETE FROM net_flow_rollup WHERE ts < ? OR ts > ?",
        (now - rollup_days * 86400, future),
    )
    await db.commit()
    return deleted


async def get_net_conn_events(agent_id: str, before: float | None = None, limit: int = 200,
                              proto: str | None = None, raddr: str | None = None,
                              port: int | None = None, pid: int | None = None) -> list:
    db = await get_db()
    clauses = ["agent_id = ?"]
    params: list = [agent_id]
    if before is not None:
        clauses.append("ts < ?"); params.append(before)
    if proto:
        clauses.append("proto = ?"); params.append(proto)
    if raddr:
        clauses.append("raddr = ?"); params.append(raddr)
    if port is not None:
        clauses.append("(lport = ? OR rport = ?)"); params.extend([port, port])
    if pid is not None:
        clauses.append("pid = ?"); params.append(pid)
    params.append(max(1, min(limit, 1000)))  # clamp lower bound too: SQLite LIMIT -1 is unbounded (review P2)
    cur = await db.execute(
        f"SELECT ts, event, proto, laddr, lport, raddr, rport, status, pid, pname, duration "
        f"FROM net_conn_events WHERE {' AND '.join(clauses)} ORDER BY ts DESC LIMIT ?",
        params,
    )
    return [dict(r) for r in await cur.fetchall()]


async def get_net_flow_rollup(agent_id: str, start: float, end: float) -> list:
    db = await get_db()
    cur = await db.execute(
        "SELECT ts, data FROM net_flow_rollup WHERE agent_id = ? AND ts >= ? AND ts <= ? ORDER BY ts ASC",
        (agent_id, start, end),
    )
    out = []
    for r in await cur.fetchall():
        d = json.loads(r["data"])
        out.append({"ts": r["ts"], "interfaces": d.get("interfaces", []),
                    "top_talkers": d.get("top_talkers", [])})
    return out


# ── Users ────────────────────────────────────────────


async def get_user(email: str) -> dict | None:
    db = await get_db()
    cursor = await db.execute("SELECT * FROM users WHERE email = ?", (email,))
    row = await cursor.fetchone()
    if not row:
        return None
    return {
        "email": row["email"],
        "password_hash": row["password_hash"],
        "totp_secret": row["totp_secret"],
        "totp_enabled": bool(row["totp_enabled"]),
        "must_change_password": bool(row["must_change_password"]),
        "role": row["role"] if "role" in row.keys() else "user",
        "is_active": bool(row["is_active"]) if "is_active" in row.keys() else True,
        "tokens_valid_after": row["tokens_valid_after"] if "tokens_valid_after" in row.keys() else 0,
        "created_at": row["created_at"],
    }


async def list_users() -> list[dict]:
    db = await get_db()
    cursor = await db.execute(
        "SELECT email, role, is_active, totp_enabled, must_change_password, created_at "
        "FROM users ORDER BY created_at ASC"
    )
    rows = await cursor.fetchall()
    return [
        {
            "email": r["email"],
            "role": r["role"],
            "is_active": bool(r["is_active"]),
            "totp_enabled": bool(r["totp_enabled"]),
            "must_change_password": bool(r["must_change_password"]),
            "created_at": r["created_at"],
        }
        for r in rows
    ]


async def create_user(email: str, password_hash: str, role: str = "user", must_change_password: bool = True) -> bool:
    db = await get_db()
    try:
        await db.execute(
            "INSERT INTO users (email, password_hash, must_change_password, created_at, role, is_active) "
            "VALUES (?, ?, ?, ?, ?, 1)",
            (email, password_hash, 1 if must_change_password else 0, time.time(), role),
        )
        await db.commit()
        return True
    except Exception:
        logger.exception("create_user failed for %s", email)
        return False


async def delete_user(email: str) -> bool:
    db = await get_db()
    cursor = await db.execute("DELETE FROM users WHERE email = ?", (email,))
    await db.commit()
    return cursor.rowcount > 0


async def count_active_admins() -> int:
    db = await get_db()
    cursor = await db.execute(
        "SELECT COUNT(*) FROM users WHERE role = 'admin' AND is_active = 1"
    )
    return (await cursor.fetchone())[0]


async def get_user_host_accounts(user_email: str) -> dict[str, str]:
    db = await get_db()
    cursor = await db.execute(
        "SELECT agent_id, host_user FROM user_host_accounts WHERE user_email = ?",
        (user_email,),
    )
    rows = await cursor.fetchall()
    return {r["agent_id"]: r["host_user"] for r in rows}


async def set_user_host_accounts(user_email: str, mapping: dict[str, str]) -> None:
    """Replace the user's host account map atomically. Empty `host_user` removes the entry."""
    db = await get_db()
    await db.execute("DELETE FROM user_host_accounts WHERE user_email = ?", (user_email,))
    for agent_id, host_user in mapping.items():
        if not host_user:
            continue
        await db.execute(
            "INSERT INTO user_host_accounts (user_email, agent_id, host_user) VALUES (?, ?, ?)",
            (user_email, agent_id, host_user),
        )
    await db.commit()


# ── Runtime Config ───────────────────────────────────


async def get_runtime_config() -> dict[str, str]:
    db = await get_db()
    cursor = await db.execute("SELECT key, value FROM runtime_config")
    rows = await cursor.fetchall()
    return {row["key"]: row["value"] for row in rows}


async def set_runtime_config(key: str, value: str) -> None:
    from app.runtime_config_validate import validate_config_value
    ALLOWED_KEYS = {
        "enable_gpu", "enable_docker", "collect_interval",
        "terminal_user", "allowed_ips",
    }
    if key not in ALLOWED_KEYS:
        return
    validate_config_value(key, value)  # defense-in-depth: reject bad values at the setter too
    db = await get_db()
    await db.execute(
        "INSERT OR REPLACE INTO runtime_config (key, value) VALUES (?, ?)",
        (key, value),
    )
    await db.commit()


async def set_runtime_configs(configs: dict[str, str]) -> None:
    for key, value in configs.items():
        await set_runtime_config(key, value)


# ── Token Blacklist ──────────────────────────────────


async def blacklist_token(token_hash: str, expires_at: float) -> None:
    db = await get_db()
    await db.execute(
        "INSERT OR IGNORE INTO token_blacklist (token_hash, expires_at) VALUES (?, ?)",
        (token_hash, expires_at),
    )
    await db.commit()


async def is_token_blacklisted(token_hash: str) -> bool:
    db = await get_db()
    cursor = await db.execute(
        "SELECT 1 FROM token_blacklist WHERE token_hash = ?", (token_hash,)
    )
    return await cursor.fetchone() is not None


async def cleanup_blacklist() -> int:
    db = await get_db()
    cursor = await db.execute(
        "DELETE FROM token_blacklist WHERE expires_at < ?", (time.time(),)
    )
    await db.commit()
    return cursor.rowcount


# ── Audit log ────────────────────────────────────────


async def audit(user: str, action: str, agent_id: str = "", detail: dict | None = None) -> None:
    """Best-effort audit insert — never raises, so it can't block the action it
    records. A wedged audit DB must not stop an admin from killing a runaway process."""
    try:
        db = await get_db()
        await db.execute(
            "INSERT INTO audit_log (timestamp, user_email, action, agent_id, detail) "
            "VALUES (?, ?, ?, ?, ?)",
            (time.time(), (user or "")[:256], action, agent_id or "", json.dumps(detail or {})),
        )
        await db.commit()
    except Exception:
        logger.exception("audit insert failed: %s %s", user, action)


async def get_audit_log(limit: int = 200, before: float | None = None,
                        user: str | None = None, action: str | None = None) -> list[dict]:
    db = await get_db()
    clauses, params = [], []
    if before is not None:
        clauses.append("timestamp < ?"); params.append(before)
    if user:
        clauses.append("user_email = ?"); params.append(user)
    if action:
        clauses.append("action = ?"); params.append(action)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    params.append(max(1, min(limit, 1000)))
    cursor = await db.execute(
        f"SELECT timestamp, user_email, action, agent_id, detail FROM audit_log{where} "
        "ORDER BY timestamp DESC LIMIT ?", params,
    )
    out = []
    for r in await cursor.fetchall():
        try:
            detail = json.loads(r["detail"])
        except (ValueError, TypeError):
            detail = {}
        out.append({"timestamp": r["timestamp"], "user": r["user_email"],
                    "action": r["action"], "agent_id": r["agent_id"], "detail": detail})
    return out


async def cleanup_audit_log(max_age_days: int = 90, max_rows: int = 100_000) -> int:
    """Two-tier prune: drop rows older than max_age_days, then FIFO-cap at max_rows."""
    db = await get_db()
    cutoff = time.time() - max_age_days * 86400
    cursor = await db.execute("DELETE FROM audit_log WHERE timestamp < ?", (cutoff,))
    removed = cursor.rowcount
    cursor = await db.execute(
        "DELETE FROM audit_log WHERE id NOT IN "
        "(SELECT id FROM audit_log ORDER BY id DESC LIMIT ?)", (max_rows,),
    )
    removed += cursor.rowcount
    await db.commit()
    return removed


# ── Users ────────────────────────────────────────────

_ALLOWED_USER_FIELDS = {"password_hash", "totp_secret", "totp_enabled", "must_change_password", "role", "is_active", "tokens_valid_after"}


async def update_user(email: str, **fields) -> bool:
    db = await get_db()
    if not fields:
        return False
    # Whitelist columns to prevent SQL injection via key names
    safe_fields = {k: v for k, v in fields.items() if k in _ALLOWED_USER_FIELDS}
    if not safe_fields:
        return False
    set_clause = ", ".join(f"{k} = ?" for k in safe_fields)
    values = list(safe_fields.values()) + [email]
    await db.execute(f"UPDATE users SET {set_clause} WHERE email = ?", values)
    await db.commit()
    return True
