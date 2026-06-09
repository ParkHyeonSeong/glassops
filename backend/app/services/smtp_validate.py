"""SMTP target validation — block the obvious SSRF / internal-port-scan primitives
while still allowing a legitimate internal relay.

We reject loopback, link-local (covers cloud metadata 169.254.169.254), unspecified,
multicast and reserved addresses, and restrict the port to known SMTP ports. RFC1918
private ranges are deliberately NOT blocked so an internal corporate relay keeps
working. An operator allowlist (GLASSOPS_SMTP_ALLOWED_HOSTS) bypasses the IP checks
for hosts explicitly vouched for.

Note: this is a validate-then-connect check, so a DNS-rebind / TOCTOU gap remains.
Given the endpoints are admin-only and the trusted-LAN model, pinning the resolved
IP into the connection isn't worth the complexity — the realistic, scriptable
SSRF/port-scan primitive (loopback + metadata + arbitrary ports) is what we close.
"""

import ipaddress
import socket

from app.config import settings

ALLOWED_SMTP_PORTS = {25, 465, 587, 2525}


def _allowed_hosts() -> set[str]:
    return {h.strip().lower() for h in (settings.smtp_allowed_hosts or "").split(",") if h.strip()}


def _ip_blocked(value: str) -> bool:
    try:
        addr = ipaddress.ip_address(value)
    except ValueError:
        return False
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped:
        addr = addr.ipv4_mapped
    return (addr.is_loopback or addr.is_link_local or addr.is_unspecified
            or addr.is_multicast or addr.is_reserved)


def validate_smtp_target(host: str, port: int) -> None:
    """Raise ValueError if (host, port) is not a permitted SMTP target."""
    host = (host or "").strip()
    if not host:
        raise ValueError("SMTP host is required")
    if "//" in host or any(c in host for c in " \t\r\n/@"):
        raise ValueError("Invalid SMTP host")
    try:
        port = int(port)
    except (TypeError, ValueError):
        raise ValueError("Invalid SMTP port")
    if port not in ALLOWED_SMTP_PORTS:
        raise ValueError(f"SMTP port must be one of {sorted(ALLOWED_SMTP_PORTS)}")

    if _allowed_hosts():
        if host.lower() not in _allowed_hosts():
            raise ValueError("SMTP host is not in GLASSOPS_SMTP_ALLOWED_HOSTS")
        return  # operator-vouched host: skip the resolution-based checks

    if _ip_blocked(host):
        raise ValueError("SMTP host resolves to a blocked address")
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        raise ValueError("SMTP host does not resolve")
    for info in infos:
        if _ip_blocked(info[4][0]):
            raise ValueError("SMTP host resolves to a blocked address")
