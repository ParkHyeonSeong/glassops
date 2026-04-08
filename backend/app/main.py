import asyncio
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import AsyncIterator

from fastapi import FastAPI, WebSocket
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.database import init_db, close_db, cleanup_old_metrics, get_recent_metrics
from app.websocket.agent_ws import handle_agent_ws, connected_agents
from app.websocket.client_ws import handle_client_ws
from app.routers.docker import router as docker_router
from app.routers.logs import router as logs_router
from app.routers.auth import router as auth_router
from app.websocket.terminal_ws import handle_terminal_ws

logger = logging.getLogger("glassops")


@asynccontextmanager
async def lifespan(app_instance: FastAPI) -> AsyncIterator[None]:
    """Startup/shutdown lifecycle."""
    app_instance.state.settings = settings
    await init_db()
    logger.info("Database initialized: %s", settings.db_path)

    async def periodic_cleanup() -> None:
        while True:
            try:
                await asyncio.sleep(3600)
                deleted = await cleanup_old_metrics(max_age_hours=24)
                if deleted:
                    logger.info("Cleaned up %d old metric records", deleted)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Error in periodic cleanup")

    cleanup_task = asyncio.create_task(periodic_cleanup())
    yield
    cleanup_task.cancel()
    await close_db()


app = FastAPI(title="GlassOps", version="0.1.0", lifespan=lifespan)

allowed_origins = [
    o.strip()
    for o in os.getenv(
        "GLASSOPS_CORS_ORIGINS",
        "http://localhost:3000,http://localhost:3300",
    ).split(",")
    if o.strip() and o.strip() != "*"
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE"],
    allow_headers=["*"],
)

app.include_router(docker_router)
app.include_router(logs_router)
app.include_router(auth_router)


# ── REST endpoints ──────────────────────────────────────


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/api/info")
async def info():
    return {
        "name": "GlassOps",
        "version": "0.1.0",
        "phase": 3,
    }


@app.get("/api/time")
async def server_time():
    now = datetime.now(timezone.utc)
    return {
        "utc": now.isoformat(),
        "timestamp": now.timestamp(),
    }


@app.get("/api/agents")
async def list_agents():
    return {
        "agents": [
            {"id": aid, "connected": True}
            for aid in connected_agents
        ]
    }


@app.get("/api/metrics/{agent_id}/history")
async def metrics_history(agent_id: str, limit: int = 60):
    data = await get_recent_metrics(agent_id, min(limit, 300))
    return {"agent_id": agent_id, "metrics": data}


# ── WebSocket endpoints ─────────────────────────────────


@app.websocket("/ws/terminal")
async def ws_terminal(ws: WebSocket):
    await handle_terminal_ws(ws)


@app.websocket("/ws/agent")
async def ws_agent(ws: WebSocket):
    await handle_agent_ws(ws)


@app.websocket("/ws/client")
async def ws_client(ws: WebSocket):
    await handle_client_ws(ws)
