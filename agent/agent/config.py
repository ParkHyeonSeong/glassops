import os
import logging
import sqlite3
import re
from contextlib import closing
from pathlib import Path
from urllib.parse import parse_qsl, urlencode

AGENT_ID = os.getenv("GLASSOPS_AGENT_ID", "agent-01")
AGENT_KEY = os.getenv("GLASSOPS_AGENT_KEY", "")
SERVER_URL = os.getenv("GLASSOPS_SERVER_URL", "ws://127.0.0.1:8000/ws/agent")
# TLS for remote (wss://) connections. Cert verification is always on; for a
# self-signed / private CA, point GLASSOPS_AGENT_CA at the CA bundle.
TLS_CA = os.getenv("GLASSOPS_AGENT_CA", "")
REQUIRE_TLS = os.getenv("GLASSOPS_REQUIRE_AGENT_TLS", "false").lower() == "true"
COLLECT_INTERVAL = max(1, int(os.getenv("GLASSOPS_COLLECT_INTERVAL", "1")))
ENABLE_GPU = os.getenv("GLASSOPS_ENABLE_GPU", "false").lower() == "true"
ENABLE_DOCKER = os.getenv("GLASSOPS_ENABLE_DOCKER", "false").lower() == "true"
ENABLE_NET_AUDIT = os.getenv("GLASSOPS_ENABLE_NET_AUDIT", "false").lower() == "true"
NET_AUDIT_MAX_EVENTS = max(1, int(os.getenv("GLASSOPS_NET_AUDIT_MAX_EVENTS", "200")))
NET_AUDIT_TOP_TALKERS = max(1, int(os.getenv("GLASSOPS_NET_AUDIT_TOP_TALKERS", "20")))

_last_runtime_overrides: dict[str, str] = {}


def read_runtime_overrides() -> dict[str, str]:
    """Read the bundled agent's saved choices; standalone agents use their env.

    Only the dashboard image supplies GLASSOPS_DB_PATH. Open a short-lived,
    read-only connection so the agent never creates a DB or writes configuration.
    A temporary read failure keeps the last applied choice.
    """
    global _last_runtime_overrides
    db_path = os.getenv("GLASSOPS_DB_PATH")
    if not db_path:
        return {}
    try:
        if db_path.startswith("file:"):
            base, _, query = db_path.partition("#")[0].partition("?")
            params = [(key, value) for key, value in parse_qsl(query) if key != "mode"]
            uri = base + "?" + urlencode([*params, ("mode", "ro")])
        else:
            uri = Path(db_path).resolve().as_uri() + "?mode=ro"
        with closing(sqlite3.connect(uri, uri=True, timeout=0.1)) as conn:
            rows = conn.execute(
                "SELECT key, value FROM runtime_config WHERE key IN "
                "('enable_gpu', 'enable_docker', 'collect_interval', 'terminal_user')"
            ).fetchall()
        overrides = {}
        for key, value in rows:
            if not isinstance(value, str):
                continue
            if key in ("enable_gpu", "enable_docker") and value in ("true", "false"):
                overrides[key] = value
            elif key == "terminal_user" and (not value or re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", value)):
                overrides[key] = value
            elif key == "collect_interval":
                try:
                    if 1 <= int(value) <= 60:
                        overrides[key] = value
                except ValueError:
                    pass
        _last_runtime_overrides = overrides
    except sqlite3.Error:
        logging.getLogger("glassops.agent").debug("Could not read saved runtime settings", exc_info=True)
    return dict(_last_runtime_overrides)


def gpu_collection_enabled(default: bool) -> bool:
    return read_runtime_overrides().get("enable_gpu", str(default).lower()) == "true"


def docker_collection_enabled(default: bool) -> bool:
    return read_runtime_overrides().get("enable_docker", str(default).lower()) == "true"


def collection_interval_seconds() -> int:
    return int(read_runtime_overrides().get("collect_interval", COLLECT_INTERVAL))


def terminal_default_user() -> str:
    return read_runtime_overrides().get("terminal_user", os.getenv("GLASSOPS_TERMINAL_USER", ""))
