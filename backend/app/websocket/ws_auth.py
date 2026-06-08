"""Shared WebSocket auth helpers — origin check + token via Sec-WebSocket-Protocol.

Browser WebSockets can't set custom headers, so the access token is passed as a
subprotocol ("bearer, <jwt>") instead of a URL query param — keeping it out of
proxy/access logs and browser history — with an access_token cookie fallback.
Browsers require the server to echo one of the offered subprotocols, so handlers
accept with `accept_subprotocol(ws)` (returns "bearer" when offered).
"""

from urllib.parse import urlparse

from fastapi import WebSocket

_BEARER = "bearer"


def origin_ok(ws: WebSocket) -> bool:
    """CSWSH/CSRF guard. Compare hostnames only — a reverse proxy (nginx `$host`)
    may strip the port, so a netloc compare would wrongly reject same-host browsers.
    A missing Origin (non-browser client) is allowed; it still needs a valid token."""
    origin = ws.headers.get("origin", "")
    host = ws.headers.get("host", "")
    if origin and host:
        return urlparse(origin).hostname == urlparse("//" + host).hostname
    return True


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
