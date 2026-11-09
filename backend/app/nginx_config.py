"""Render explicitly confirmed IP rules whenever the bundled nginx starts.

Runs as root under the existing nginx supervisor program. Do not import the
backend database/config bootstrap here: startup reads must never initialize DBs.
"""
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
from contextlib import closing
from pathlib import Path
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit

from app.runtime_config_validate import normalize_ip_rules

IP_CONF = Path("/etc/nginx/conf.d/ip-whitelist.conf")
_MARKER = "# GlassOps allowed_ips: "


def _startup_rules() -> str:
    db_path = os.getenv("GLASSOPS_DB_PATH", "./data/glassops.db")
    if db_path.startswith("file:"):
        base, _, query = db_path.partition("#")[0].partition("?")
        params = [(key, value) for key, value in parse_qsl(query) if key != "mode"]
        uri = base + "?" + urlencode([*params, ("mode", "ro")])
        path = Path(unquote(urlsplit(base).path))
    else:
        path = Path(db_path).resolve()
        uri = path.as_uri() + "?mode=ro"
    confirmed = None
    if path.exists():
        with closing(sqlite3.connect(uri, uri=True, timeout=1)) as conn:
            # Absence during first boot is different from a corrupt/unreadable DB.
            exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='runtime_config'"
            ).fetchone()
            if exists:
                row = conn.execute(
                    "SELECT value FROM runtime_config WHERE key = 'allowed_ips_confirmed'"
                ).fetchone()
                if row:
                    confirmed = row[0]
    return normalize_ip_rules(confirmed if confirmed is not None else os.getenv("GLASSOPS_ALLOWED_IPS", ""))


def render_ip_rules(value: str) -> str:
    value = normalize_ip_rules(value)
    lines = [_MARKER + json.dumps(value), "geo $ip_whitelist {", f"  default {0 if value else 1};"]
    if value:
        # Keep the dashboard's internal connections accessible, as before.
        networks = ["127.0.0.1/32", "::1/128", *value.split(",")]
        lines.extend(f"  {net} 1;" for net in dict.fromkeys(networks))
    return "\n".join([*lines, "}", ""])


def _replace_config(content: bytes) -> None:
    # Neither the candidate nor a backup may match nginx's *.conf include.
    with tempfile.NamedTemporaryFile(dir=IP_CONF.parent, prefix=".ip-whitelist-", suffix=".tmp", delete=False) as temp:
        path = Path(temp.name)
        try:
            temp.write(content)
            temp.flush()
            os.fchmod(temp.fileno(), 0o644)  # appuser must be able to report the active policy
            os.replace(path, IP_CONF)
        finally:
            path.unlink(missing_ok=True)


def prepare_ip_rules() -> str:
    value = _startup_rules()
    previous = IP_CONF.read_bytes() if IP_CONF.exists() else None
    _replace_config(render_ip_rules(value).encode())
    try:
        result = subprocess.run(["nginx", "-t"], capture_output=True, text=True, timeout=5)
        if result.returncode:
            raise RuntimeError(result.stderr.strip() or "nginx configuration check failed")
    except Exception:
        if previous is None:
            IP_CONF.unlink(missing_ok=True)
        else:
            _replace_config(previous)
        raise
    return value


def read_active_ip_rules() -> str | None:
    """Read the generated policy; the caller must ALSO verify nginx is serving."""
    try:
        content = IP_CONF.read_text()
        first = content.splitlines()[0]
        if not first.startswith(_MARKER):
            return None
        value = json.loads(first[len(_MARKER):])
        if not isinstance(value, str) or content != render_ip_rules(value):
            return None
        return normalize_ip_rules(value)
    except (OSError, ValueError, IndexError):
        return None


if __name__ == "__main__":
    if sys.argv[1:] != ["run"]:
        raise SystemExit("Usage: python -m app.nginx_config run")
    prepare_ip_rules()
    os.execvp("nginx", ["nginx", "-g", "daemon off;"])
