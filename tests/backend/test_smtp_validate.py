import socket

import pytest

from app.config import settings
from app.services.smtp_validate import ALLOWED_SMTP_PORTS, validate_smtp_target


@pytest.fixture(autouse=True)
def no_allowlist(monkeypatch):
    """Default to the resolution-based branch; allowlist tests opt in."""
    monkeypatch.setattr(settings, "smtp_allowed_hosts", "")


@pytest.fixture
def resolves_public(monkeypatch):
    """Pin getaddrinfo to a routable address so tests never touch real DNS."""
    def fake(host, port, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("93.184.216.34", port))]
    monkeypatch.setattr(socket, "getaddrinfo", fake)


@pytest.mark.parametrize("port", sorted(ALLOWED_SMTP_PORTS))
def test_allowed_ports_accepted(port, resolves_public):
    validate_smtp_target("relay.example.com", port)


@pytest.mark.parametrize("port", [0, 24, 8025, 1025, 65535, -1])
def test_other_ports_rejected(port, resolves_public):
    with pytest.raises(ValueError, match="SMTP port must be one of"):
        validate_smtp_target("relay.example.com", port)


def test_non_integer_port_rejected(resolves_public):
    with pytest.raises(ValueError, match="Invalid SMTP port"):
        validate_smtp_target("relay.example.com", "not-a-port")


def test_empty_host_rejected():
    with pytest.raises(ValueError, match="SMTP host is required"):
        validate_smtp_target("   ", 587)


@pytest.mark.parametrize("host", [
    "smtp://relay.example.com",     # scheme
    "relay.example.com/path",       # path
    "user@relay.example.com",       # userinfo
    "relay example.com",            # inner space
    "relay.example.com\nX",         # header injection
])
def test_structurally_invalid_hosts_rejected(host):
    with pytest.raises(ValueError, match="Invalid SMTP host"):
        validate_smtp_target(host, 587)


def test_surrounding_whitespace_is_normalised(resolves_public):
    # Leading/trailing whitespace must not defeat the checks or reach the socket.
    validate_smtp_target("  relay.example.com  ", 587)


@pytest.mark.parametrize("host", [
    "127.0.0.1",           # loopback
    "0.0.0.0",             # unspecified
    "169.254.169.254",     # cloud metadata (link-local)
    "224.0.0.1",           # multicast
    "::1",                 # IPv6 loopback
    "::ffff:127.0.0.1",    # IPv4-mapped loopback
])
def test_blocked_ip_literals_rejected(host):
    with pytest.raises(ValueError, match="blocked address"):
        validate_smtp_target(host, 587)


def test_private_relay_still_allowed(monkeypatch):
    # RFC1918 is deliberately NOT blocked — an internal corporate relay must work.
    def fake(host, port, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("10.4.0.9", port))]
    monkeypatch.setattr(socket, "getaddrinfo", fake)

    validate_smtp_target("relay.internal.example", 587)


def test_host_resolving_to_blocked_address_rejected(monkeypatch):
    def fake(host, port, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("127.0.0.1", port))]
    monkeypatch.setattr(socket, "getaddrinfo", fake)

    with pytest.raises(ValueError, match="blocked address"):
        validate_smtp_target("rebind.example.com", 587)


def test_dns_failure_rejected(monkeypatch):
    def boom(host, port, **kwargs):
        raise socket.gaierror("no such host")
    monkeypatch.setattr(socket, "getaddrinfo", boom)

    with pytest.raises(ValueError, match="does not resolve"):
        validate_smtp_target("nx.example.invalid", 587)


def test_allowlisted_host_bypasses_resolution(monkeypatch):
    monkeypatch.setattr(settings, "smtp_allowed_hosts", "mailpit")

    def boom(host, port, **kwargs):
        raise AssertionError("allowlisted host must not be resolved")
    monkeypatch.setattr(socket, "getaddrinfo", boom)

    validate_smtp_target("mailpit", 2525)


def test_host_outside_allowlist_rejected(monkeypatch):
    monkeypatch.setattr(settings, "smtp_allowed_hosts", "mailpit")

    with pytest.raises(ValueError, match="GLASSOPS_SMTP_ALLOWED_HOSTS"):
        validate_smtp_target("relay.example.com", 587)


def test_allowlist_matching_ignores_case_and_padding(monkeypatch):
    monkeypatch.setattr(settings, "smtp_allowed_hosts", "  Mailpit , Relay.Example.COM ")

    validate_smtp_target("MAILPIT", 2525)
    validate_smtp_target("relay.example.com", 587)


def test_allowlist_does_not_relax_the_port_rule(monkeypatch):
    monkeypatch.setattr(settings, "smtp_allowed_hosts", "mailpit")

    with pytest.raises(ValueError, match="SMTP port must be one of"):
        validate_smtp_target("mailpit", 8025)
