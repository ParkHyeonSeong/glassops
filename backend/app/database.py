"""SQLite database for metrics storage — singleton connection."""

import json
import os
import time

import aiosqlite

from app.config import settings

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
    await db.commit()


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
    cursor = await db.execute(
        "DELETE FROM metrics WHERE timestamp < ?", (cutoff,)
    )
    await db.commit()
    return cursor.rowcount
