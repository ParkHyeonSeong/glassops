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

    # Create default admin if not exists
    cursor = await db.execute("SELECT email FROM users WHERE email = ?", ("admin@glassops.local",))
    if not await cursor.fetchone():
        default_pw = os.getenv("GLASSOPS_ADMIN_PASSWORD", "admin")
        pw_hash = bcrypt.hashpw(default_pw.encode(), bcrypt.gensalt()).decode()
        await db.execute(
            "INSERT INTO users (email, password_hash, must_change_password, created_at) VALUES (?, ?, ?, ?)",
            ("admin@glassops.local", pw_hash, 1 if default_pw == "admin" else 0, time.time()),
        )
        logger.info("Default admin user created: admin@glassops.local")

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


async def cleanup_old_metrics(max_age_hours: int = 24) -> int:
    cutoff = time.time() - (max_age_hours * 3600)
    db = await get_db()
    cursor = await db.execute("DELETE FROM metrics WHERE timestamp < ?", (cutoff,))
    await db.commit()
    return cursor.rowcount


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
