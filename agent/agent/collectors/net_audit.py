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
