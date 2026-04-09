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
            created_at REAL NOT NULL
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
            # No password configured — generate random one-time password
            password = secrets.token_urlsafe(16)
            must_change = True
            logger.warning("=" * 60)
            logger.warning("  INITIAL ADMIN PASSWORD (change immediately!)")
            logger.warning("  Email:    %s", default_email)
            logger.warning("  Password: %s", password)
            logger.warning("=" * 60)

        pw_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
        await db.execute(
            "INSERT INTO users (email, password_hash, must_change_password, created_at) VALUES (?, ?, ?, ?)",
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
    except (KeyError, IndexError, TypeError):
        pass

    # Drop heavy fields for downsampled data
    result.pop("processes", None)
    result.pop("containers", None)
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
    }


_ALLOWED_USER_FIELDS = {"password_hash", "totp_secret", "totp_enabled", "must_change_password"}


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
