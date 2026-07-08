"""Network connection audit collector.

Reads the HOST init-netns connection table from /host/proc/1/net/* so it works
on both the bridged bundled agent and the host-netns standalone agent WITHOUT
any new Linux capability (only the already-mounted read-only /host/proc). Diffs
successive snapshots into open/close events and aggregates a per-minute rollup.
Metadata only — no payloads.
"""

import logging
import os
import socket
import struct

logger = logging.getLogger("glassops.agent")

TCP_STATES = {
    "01": "ESTABLISHED", "02": "SYN_SENT", "03": "SYN_RECV", "04": "FIN_WAIT1",
    "05": "FIN_WAIT2", "06": "TIME_WAIT", "07": "CLOSE", "08": "CLOSE_WAIT",
    "09": "LAST_ACK", "0A": "LISTEN", "0B": "CLOSING",
}


def _decode_addr(hex_addr: str) -> tuple[str, int]:
    """Decode a /proc/net hex 'ADDRESS:PORT' field to (ip, port)."""
    addr_hex, port_hex = hex_addr.split(":")
    port = int(port_hex, 16)
    if len(addr_hex) == 8:  # IPv4: 4 little-endian bytes
        ip = socket.inet_ntop(socket.AF_INET, struct.pack("<I", int(addr_hex, 16)))
    else:  # IPv6: four little-endian 32-bit words
        words = [addr_hex[i:i + 8] for i in range(0, 32, 8)]
        packed = b"".join(struct.pack("<I", int(w, 16)) for w in words)
        ip = socket.inet_ntop(socket.AF_INET6, packed)
    return ip, port


def parse_proc_net(text: str, proto: str) -> list[dict]:
    """Parse a /proc/net/{tcp,tcp6,udp,udp6} table into connection rows."""
    rows: list[dict] = []
    is_tcp = proto.startswith("tcp")
    for line in text.splitlines()[1:]:  # skip header
        parts = line.split()
        if len(parts) < 10:
            continue
        try:
            laddr, lport = _decode_addr(parts[1])
            raddr, rport = _decode_addr(parts[2])
            status = TCP_STATES.get(parts[3].upper(), parts[3]) if is_tcp else ""
            inode = int(parts[9])
        except (ValueError, IndexError):
            continue
        rows.append({
            "proto": proto, "laddr": laddr, "lport": lport,
            "raddr": raddr, "rport": rport, "status": status, "inode": inode,
        })
    return rows


import re

_SOCKET_RE = re.compile(r"socket:\[(\d+)\]")
_FD_SCAN_BUDGET = 200_000   # cap fds inspected per call (high-fd hosts)
_scan_degraded = 0          # bumped + logged when the budget is hit


def build_inode_pid_map(host_proc: str, wanted: set[int] | None = None,
                        budget: int = _FD_SCAN_BUDGET) -> dict[int, tuple[int, str]]:
    """Map socket inode -> (pid, comm) by scanning <host_proc>/<pid>/fd/*.

    When `wanted` is given, only those inodes are resolved and the scan stops as
    soon as all are found — so steady state (no new connections) costs almost
    nothing (review P2). `budget` caps total fds inspected; on overflow the scan
    stops early, logs, and bumps a degraded counter instead of stalling the loop.
    """
    global _scan_degraded
    mapping: dict[int, tuple[int, str]] = {}
    scanned = 0
    try:
        entries = os.listdir(host_proc)
    except OSError:
        return mapping
    for entry in entries:
        if not entry.isdigit():
            continue
        if wanted is not None and not (wanted - mapping.keys()):
            break  # every wanted inode already resolved
        pid = int(entry)
        fd_dir = os.path.join(host_proc, entry, "fd")
        try:
            fds = os.listdir(fd_dir)
        except OSError:
            continue  # process gone or not readable
        comm = ""
        for fd in fds:
            scanned += 1
            if scanned > budget:
                _scan_degraded += 1
                logger.warning("net_audit: fd scan budget %d exceeded; inode map "
                               "partial (degraded=%d)", budget, _scan_degraded)
                return mapping
            try:
                target = os.readlink(os.path.join(fd_dir, fd))
            except OSError:
                continue
            m = _SOCKET_RE.match(target)
            if not m:
                continue
            inode = int(m.group(1))
            if wanted is not None and inode not in wanted:
                continue
            if not comm:
                try:
                    with open(os.path.join(host_proc, entry, "comm")) as f:
                        comm = f.read().strip()
                except OSError:
                    comm = ""
            mapping[inode] = (pid, comm)
    return mapping


def parse_proc_net_dev(text: str) -> dict[str, dict]:
    """Parse /proc/net/dev into per-interface counters."""
    out: dict[str, dict] = {}
    for line in text.splitlines():
        if ":" not in line:
            continue
        name, _, rest = line.partition(":")
        name = name.strip()
        cols = rest.split()
        if len(cols) < 16:
            continue
        try:
            out[name] = {
                "bytes_in": int(cols[0]), "packets_in": int(cols[1]),
                "bytes_out": int(cols[8]), "packets_out": int(cols[9]),
            }
        except (ValueError, IndexError):
            continue
    return out


from abc import ABC, abstractmethod
from typing import NamedTuple

_PROTOS = ("tcp", "tcp6", "udp", "udp6")


class Snapshot(NamedTuple):
    conns: list          # list[dict]
    ok: bool             # False -> source could not be read this tick
    reason: str = ""     # degraded reason when not ok


class ConnectionSource(ABC):
    """Pluggable source of the host connection table. Default impl parses
    /host/proc/<netns_pid>/net/*; future impls could use conntrack or eBPF."""

    @abstractmethod
    def snapshot(self) -> Snapshot: ...

    @abstractmethod
    def interface_counters(self) -> dict[str, dict]: ...


class HostNetnsProcSource(ConnectionSource):
    def __init__(self, host_proc: str | None = None, netns_pid: int = 1):
        self._proc = host_proc or os.environ.get("HOST_PROC", "/host/proc")
        self._netns_pid = netns_pid
        # inode -> (pid, comm) cache. Only NEW connection inodes trigger a scan;
        # steady state does zero fd scanning (review P2).
        self._inode_cache: dict[int, tuple[int, str]] = {}

    def _net_path(self, name: str) -> str:
        return os.path.join(self._proc, str(self._netns_pid), "net", name)

    def _read_proto(self, path: str) -> tuple[str, str | None]:
        """Read a /proc/net proto table. Returns ("ok", text); ("absent", None) if
        the file does not exist (optional proto, e.g. IPv6 disabled); or
        ("error", None) if it exists but could not be read."""
        try:
            with open(path) as f:
                return ("ok", f.read())
        except FileNotFoundError:
            return ("absent", None)
        except OSError:
            logger.debug("net_audit: could not read %s", path)
            return ("error", None)

    def _read(self, path: str) -> str | None:
        """Return file text, or None if unreadable (used for /net/dev)."""
        try:
            with open(path) as f:
                return f.read()
        except OSError:
            return None

    def snapshot(self) -> Snapshot:
        states = {p: self._read_proto(self._net_path(p)) for p in _PROTOS}
        # Complete-or-skip (review P1): a PARTIAL read must NOT be diffed — the missing
        # proto's live connections would all look closed (then re-open when it recovers).
        # `tcp` is the availability canary (present on every Linux netns); if it is absent
        # or unreadable, the whole host netns is unavailable this tick. We NEVER fall back
        # to the container's own netns via psutil (review P1).
        if states["tcp"][0] != "ok":
            return Snapshot([], ok=False, reason=f"tcp table {states['tcp'][0]}")
        # Any OTHER proto that EXISTS but failed to read ("error") is a partial failure
        # -> skip the whole tick. A genuinely-absent optional proto ("absent", e.g.
        # tcp6/udp6 on an IPv6-disabled host) contributes nothing and is tolerated.
        for proto in ("tcp6", "udp", "udp6"):
            if states[proto][0] == "error":
                return Snapshot([], ok=False, reason=f"{proto} table unreadable")
        rows: list[dict] = []
        for proto in _PROTOS:
            state, text = states[proto]
            if state == "ok":
                rows.extend(parse_proc_net(text, proto))
        wanted = {r["inode"] for r in rows if r.get("inode")}
        missing = wanted - self._inode_cache.keys()
        if missing:
            self._inode_cache.update(build_inode_pid_map(self._proc, wanted=missing))
        # Drop entries whose sockets are gone so the cache can't grow unbounded.
        self._inode_cache = {i: v for i, v in self._inode_cache.items() if i in wanted}
        for r in rows:
            pid, pname = self._inode_cache.get(r["inode"], (None, ""))
            r["pid"] = pid
            r["pname"] = pname
        return Snapshot(rows, ok=True)

    def interface_counters(self) -> dict[str, dict]:
        return parse_proc_net_dev(self._read(self._net_path("dev")) or "")
