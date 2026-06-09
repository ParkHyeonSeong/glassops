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
            pw_file = os.path.join(os.path.dirname(settings.db_path) or ".", "initial_admin_password")
            try:
                with open(pw_file, "w") as f:
                    f.write(f"email: {default_email}\npassword: {password}\n")
                os.chmod(pw_file, 0o600)
                logger.warning("Initial admin password generated → %s "
                               "(read it, log in, change immediately, then delete the file)", pw_file)
            except OSError:
                logger.warning("Initial admin password (set GLASSOPS_ADMIN_PASSWORD next time): %s / %s",
                               default_email, password)

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

    # Raw: keep last 1 hour only (downsampled covers the rest)
    raw_cutoff = now - (max_age_hours * 3600)
    cursor = await db.execute("DELETE FROM metrics WHERE timestamp < ?", (raw_cutoff,))
    raw_deleted = cursor.rowcount

    # Downsampled: keep 7 days
    ds_cutoff = now - (7 * 86400)
    await db.execute("DELETE FROM metrics_downsampled WHERE timestamp < ?", (ds_cutoff,))

    await db.commit()
    return raw_deleted


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
    ALLOWED_KEYS = {
        "enable_gpu", "enable_docker", "collect_interval",
        "terminal_user", "allowed_ips",
    }
    if key not in ALLOWED_KEYS:
        return
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
