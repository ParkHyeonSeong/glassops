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


import time as _time

_WILDCARD = {"", "0.0.0.0", "::"}


def _conn_key(c: dict) -> tuple:
    return (c["proto"], c["laddr"], c["lport"], c["raddr"], c["rport"])


class NetAuditCollector:
    def __init__(self, source: ConnectionSource, max_events: int = 200,
                 top_talkers: int = 20, clock=_time.time):
        self._source = source
        self._max_events = max_events
        self._top_talkers = top_talkers
        self._clock = clock
        # conn key -> {"ts": open_ts, "pid", "pname", "status"} — attribution kept
        # so the eventual close can report which process's connection ended (P2).
        self._prev: dict[tuple, dict] = {}
        self._bucket: int | None = None            # current minute bucket (epoch // 60)
        self._bucket_start_if: dict | None = None  # iface counters at bucket start (P1)
        # raddr -> set of DISTINCT active conn keys observed while this bucket was
        # open. Counting distinct active connections (not just new opens) means a
        # long-lived SSH/C2/DB tunnel appears in the top-talkers of EVERY bucket it
        # spans, not only the bucket it opened in (review P2).
        self._bucket_conns: dict[str, set] = {}
        # Set when a source outage crosses a minute boundary: the pending bucket is
        # no longer a clean 1-min window (its byte delta would span the whole gap),
        # so the next good tick discards it and re-baselines instead (review P1).
        self._bucket_invalid = False

    def collect(self) -> dict:
        now = self._clock()
        snap = self._source.snapshot()
        if not snap.ok:
            # Source unavailable this tick: SKIP the diff entirely. Diffing a failed
            # read against _prev would emit a false 'close' for every live connection
            # (and a false 're-open' next tick), poisoning the audit trail (review P1).
            # Keep all state (_prev, bucket accounting) intact and emit nothing. If the
            # outage has crossed into a new minute, mark the pending bucket invalid so
            # its (now gap-spanning) delta is discarded rather than mis-stored (review P1).
            if self._bucket is not None and int(now // 60) != self._bucket:
                self._bucket_invalid = True
            logger.warning("net_audit: source unavailable (%s); skipping tick", snap.reason)
            return {"events": [], "rollups": [], "dropped": 0}
        conns = [c for c in snap.conns if c.get("raddr") not in _WILDCARD]
        current = {_conn_key(c): c for c in conns}

        opens: list[dict] = []
        for key, c in current.items():
            if key not in self._prev:
                self._prev[key] = {"ts": now, "pid": c.get("pid"),
                                   "pname": c.get("pname", ""), "status": c.get("status", "")}
                opens.append(self._event("open", now, c, None))

        closes: list[dict] = []
        for key in list(self._prev):
            if key not in current:
                meta = self._prev.pop(key)
                proto, laddr, lport, raddr, rport = key
                closes.append({
                    "event": "close", "ts": now, "proto": proto,
                    "laddr": laddr, "lport": lport, "raddr": raddr, "rport": rport,
                    "status": meta["status"], "pid": meta["pid"], "pname": meta["pname"],
                    "duration": round(now - meta["ts"], 3),
                })

        # Cap policy (P2): keep every close first (they carry duration + attribution),
        # fill the remaining budget with opens, and report how many were dropped.
        events = closes + opens
        dropped = 0
        if len(events) > self._max_events:
            dropped = len(events) - self._max_events
            logger.warning("net_audit: dropping %d events over cap %d",
                           dropped, self._max_events)
            events = events[:self._max_events]

        # Flush the PREVIOUS bucket before recording this cycle's active conns, so a
        # persistent connection is attributed to every bucket it is alive in (P2).
        rollups = self._maybe_rollup(now)
        for key, c in current.items():
            self._bucket_conns.setdefault(c["raddr"], set()).add(key)

        return {"events": events, "rollups": rollups, "dropped": dropped}

    def _event(self, kind: str, ts: float, c: dict, duration) -> dict:
        return {
            "event": kind, "ts": ts, "proto": c["proto"],
            "laddr": c["laddr"], "lport": c["lport"],
            "raddr": c["raddr"], "rport": c["rport"], "status": c.get("status", ""),
            "pid": c.get("pid"), "pname": c.get("pname", ""), "duration": duration,
        }

    def _maybe_rollup(self, now: float) -> list[dict]:
        bucket = int(now // 60)
        if self._bucket is None:
            self._bucket = bucket
            self._bucket_start_if = self._source.interface_counters()
            return []
        if bucket == self._bucket:
            return []
        # bucket changed -> flush the previous bucket with per-bucket byte DELTAS.
        cur_if = self._source.interface_counters()
        if self._bucket_invalid:
            # A source outage crossed this bucket's boundary; its delta would span the
            # whole gap (e.g. 12:00 baseline, 12:01-12:03 down, 12:04 up => 4 min of
            # bytes mis-stored as the 12:00 one-minute rollup). Discard and re-baseline
            # cleanly on this good tick instead (review P1).
            self._bucket = bucket
            self._bucket_start_if = cur_if
            self._bucket_conns = {}
            self._bucket_invalid = False
            return []
        start_if = self._bucket_start_if or {}
        ifaces = []
        for name, c in cur_if.items():
            if name not in start_if:
                # No baseline for this interface — /proc/net/dev was unreadable at
                # bucket start (baseline {}) or the interface appeared mid-bucket.
                # Skip it: subtracting a missing baseline would store the full
                # cumulative counter as if it were one minute of traffic (review P2).
                continue
            s = start_if[name]
            ifaces.append({
                "name": name,
                "bytes_in": max(0, c.get("bytes_in", 0) - s.get("bytes_in", 0)),
                "bytes_out": max(0, c.get("bytes_out", 0) - s.get("bytes_out", 0)),
            })
        # conns = number of distinct active connections to that peer during the bucket.
        talkers = sorted(((r, len(keys)) for r, keys in self._bucket_conns.items()),
                         key=lambda kv: kv[1], reverse=True)
        rollup = {
            "ts": float(self._bucket * 60),
            "interfaces": ifaces,
            "top_talkers": [{"raddr": r, "conns": n} for r, n in talkers[:self._top_talkers]],
        }
        self._bucket = bucket
        self._bucket_start_if = cur_if
        self._bucket_conns = {}
        return [rollup]
