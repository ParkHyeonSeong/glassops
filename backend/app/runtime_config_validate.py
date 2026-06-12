"""Shared validation for runtime_config values, used by BOTH the settings router and
the DB setter so no write path can persist an unvalidated value (INJECT-05). Format
only — request-context checks (e.g. the allowed_ips self-lockout) stay in the router."""

import ipaddress
import re

USERNAME_PATTERN = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")


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
        for entry in (e.strip() for e in (value or "").split(",")):
            if not entry:
                continue
            try:
                ipaddress.ip_network(entry, strict=False)
            except ValueError:
                raise ValueError(f"Invalid CIDR: {entry}")
