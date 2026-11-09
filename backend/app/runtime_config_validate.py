"""Shared validation for runtime_config values, used by BOTH the settings router and
the DB setter so no write path can persist an unvalidated value (INJECT-05). Format
only — request-context checks (e.g. the allowed_ips self-lockout) stay in the router."""

import ipaddress
import re

USERNAME_PATTERN = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")


def normalize_ip_rules(value: str) -> str:
    """Canonical nginx-compatible CIDRs. Only an explicit empty value allows all."""
    if value == "":
        return ""
    networks = set()
    for entry in (part.strip() for part in value.split(",")):
        if not entry:
            continue
        try:
            if "%" in entry:  # IPv6 scoped addresses are not nginx geo CIDRs.
                raise ValueError
            networks.add(ipaddress.ip_network(entry, strict=False))
        except ValueError:
            raise ValueError(f"Invalid CIDR: {entry}") from None
    if not networks:
        raise ValueError("Enter at least one IP/CIDR, or clear the field to allow all")
    return ",".join(str(net) for net in sorted(
        networks, key=lambda net: (net.version, int(net.network_address), net.prefixlen)
    ))


def validate_config_value(key: str, value: str) -> None:
    """Raise ValueError if `value` is not a valid runtime_config value for `key`."""
    if key == "collect_interval":
        try:
            n = int(value)
        except (TypeError, ValueError):
            raise ValueError("Interval must be a number")
        if n < 1 or n > 60:
            raise ValueError("Interval must be 1-60")
    elif key in ("enable_gpu", "enable_docker"):
        if value not in ("true", "false"):
            raise ValueError(f"{key} must be 'true' or 'false'")
    elif key == "terminal_user":
        if value and not USERNAME_PATTERN.match(value):
            raise ValueError("Invalid username format")
    elif key == "allowed_ips":
        normalize_ip_rules(value)
