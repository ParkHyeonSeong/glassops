"""Trusted-proxy aware client IP and scheme resolution.

Forwarded headers (X-Real-IP / X-Forwarded-For / X-Forwarded-Proto) are believed
ONLY when the real TCP peer is a configured trusted proxy (GLASSOPS_TRUSTED_PROXIES,
default the bundled nginx at 127.0.0.1). Otherwise the real peer address is used,
so a client can't spoof its IP to dodge login lockout / rate limiting, or claim
https to influence cookie flags.
"""

import ipaddress

from app.config import settings

_cache: dict = {"raw": None, "nets": []}


def _trusted_nets() -> list:
    raw = settings.trusted_proxies or ""
    if _cache["raw"] != raw:
        nets = []
        for entry in raw.split(","):
            entry = entry.strip()
            if not entry:
                continue
            try:
                nets.append(ipaddress.ip_network(entry, strict=False))
            except ValueError:
                pass
        _cache["raw"] = raw
        _cache["nets"] = nets
    return _cache["nets"]


def _peer_trusted(peer: str) -> bool:
    try:
        addr = ipaddress.ip_address(peer)
    except ValueError:
        return False
    # The bundled nginx (and in-container health checks) reach uvicorn over loopback;
    # trust it implicitly so overriding GLASSOPS_TRUSTED_PROXIES with only an upstream
    # LB CIDR doesn't silently drop trust in the bundled proxy and collapse every
    # client to 127.0.0.1. Safe because uvicorn is loopback-bound.
    if addr.is_loopback:
        return True
    return any(addr in net for net in _trusted_nets())


def _header(scope, name: bytes) -> str:
    for k, v in scope.get("headers", []):
        if k == name:
            return v.decode("latin-1")
    return ""


def resolve_client_ip(scope) -> str:
    """Real client IP: a forwarded header only if the direct peer is trusted."""
    client = scope.get("client")
    peer = client[0] if client else ""
    if peer and _peer_trusted(peer):
        xri = _header(scope, b"x-real-ip").strip()
        if xri:
            return xri
        xff = _header(scope, b"x-forwarded-for").strip()
        if xff:
            return xff.split(",")[0].strip()
    return peer or "unknown"


def request_is_secure(scope) -> bool:
    """Whether the original request reached the edge over HTTPS (for Secure cookies)."""
    if settings.force_secure_cookies:
        return True
    if scope.get("scheme") == "https":
        return True
    client = scope.get("client")
    peer = client[0] if client else ""
    if peer and _peer_trusted(peer):
        return _header(scope, b"x-forwarded-proto").strip().lower() == "https"
    return False
