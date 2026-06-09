"""Shared WebSocket auth helpers — origin check + token via Sec-WebSocket-Protocol.

Browser WebSockets can't set custom headers, so the access token is passed as a
subprotocol ("bearer, <jwt>") instead of a URL query param — keeping it out of
proxy/access logs and browser history — with an access_token cookie fallback.
Browsers require the server to echo one of the offered subprotocols, so handlers
accept with `accept_subprotocol(ws)` (returns "bearer" when offered).
"""

from urllib.parse import urlparse

from fastapi import WebSocket

from app.config import settings

_BEARER = "bearer"

_origin_cache: dict = {"raw": None, "hosts": set()}


def _allowed_hosts() -> set[str]:
    """Hostnames parsed from GLASSOPS_ALLOWED_ORIGINS (cached on the raw string)."""
    raw = settings.allowed_origins or ""
    if _origin_cache["raw"] != raw:
        hosts = set()
        for entry in raw.split(","):
            entry = entry.strip()
            if not entry:
                continue
            host = urlparse(entry if "//" in entry else "//" + entry).hostname
            if host:
                hosts.add(host.lower())
        _origin_cache["raw"] = raw
        _origin_cache["hosts"] = hosts
    return _origin_cache["hosts"]


def origin_ok(ws: WebSocket) -> bool:
    """CSWSH/CSRF guard for the browser WS channels (the agent transport does NOT
    use this — it authenticates with x-agent-key headers and sends no Origin).

    Fail-closed: a missing Origin is rejected (real browsers always send one on WS
    handshakes). When GLASSOPS_ALLOWED_ORIGINS is set, the Origin hostname must be
    in that allowlist. Otherwise fall back to matching the request Host hostname
    (port-insensitive, since nginx `$host` may strip the port for same-host LAN)."""
    origin = ws.headers.get("origin", "")
    o_host = urlparse(origin).hostname if origin else ""
    if not o_host:
        return False
    allowed = _allowed_hosts()
    if allowed:
        return o_host.lower() in allowed
    host = ws.headers.get("host", "")
    return bool(host) and o_host == urlparse("//" + host).hostname


def _offered(ws: WebSocket) -> list[str]:
    raw = ws.headers.get("sec-websocket-protocol", "")
    return [p.strip() for p in raw.split(",") if p.strip()]


def ws_token(ws: WebSocket) -> str:
    """Extract the bearer token from the Sec-WebSocket-Protocol header
    ("bearer, <jwt>"), falling back to the access_token cookie."""
    for p in _offered(ws):
        if p != _BEARER:
            return p
    return ws.cookies.get("access_token", "")


def accept_subprotocol(ws: WebSocket) -> str | None:
    """Echo "bearer" when the client offered it so the browser accepts the
    negotiated subprotocol; None when auth came via cookie (no subprotocol)."""
    return _BEARER if _BEARER in _offered(ws) else None
