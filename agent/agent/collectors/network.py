"""Collects network metrics via psutil."""

import logging
import time as _time

import psutil

logger = logging.getLogger("glassops.agent")

# Previous state for delta/rate calculation
_prev_io: dict | None = None
_prev_time: float = 0


def collect_network() -> dict:
    global _prev_io, _prev_time

    try:
        now = _time.monotonic()
        io = psutil.net_io_counters()
        current_io = {
            "bytes_sent": io.bytes_sent,
            "bytes_recv": io.bytes_recv,
            "packets_sent": io.packets_sent,
            "packets_recv": io.packets_recv,
            "errin": io.errin,
            "errout": io.errout,
        }

        # Calculate rates (bytes/sec) with actual elapsed time
        rates = {"send_rate": 0, "recv_rate": 0}
        if _prev_io is not None and _prev_time > 0:
            elapsed = max(now - _prev_time, 0.1)
            rates["send_rate"] = max(0, int((current_io["bytes_sent"] - _prev_io["bytes_sent"]) / elapsed))
            rates["recv_rate"] = max(0, int((current_io["bytes_recv"] - _prev_io["bytes_recv"]) / elapsed))
        _prev_io = current_io
        _prev_time = now

        # Active connections (limit to avoid huge payloads)
        connections = []
        try:
            for conn in psutil.net_connections(kind="inet"):
                if conn.status == "NONE":
                    continue
                connections.append({
                    "type": "TCP" if conn.type == 1 else "UDP",
                    "laddr": f"{conn.laddr.ip}:{conn.laddr.port}" if conn.laddr else "",
                    "raddr": f"{conn.raddr.ip}:{conn.raddr.port}" if conn.raddr else "",
                    "status": conn.status,
                    "pid": conn.pid,
                })
                if len(connections) >= 100:
                    break
        except (psutil.AccessDenied, PermissionError):
            logger.debug("Access denied for net_connections, skipping")

        # Interface info
        interfaces = []
        try:
            addrs = psutil.net_if_addrs()
            stats = psutil.net_if_stats()
            for name, addr_list in addrs.items():
                iface_stat = stats.get(name)
                ipv4 = next((a.address for a in addr_list if a.family == 2), None)  # AF_INET
                if ipv4 and ipv4 != "127.0.0.1":
                    interfaces.append({
                        "name": name,
                        "ip": ipv4,
                        "is_up": iface_stat.isup if iface_stat else False,
                        "speed": iface_stat.speed if iface_stat else 0,
                    })
        except Exception:
            logger.debug("Failed to collect interface info")

        return {
            "io": current_io,
            "rates": rates,
            "connections": connections,
            "interfaces": interfaces,
            "connection_count": len(connections),
        }
    except Exception:
        logger.exception("Failed to collect network metrics")
        return {
            "io": {}, "rates": {"send_rate": 0, "recv_rate": 0},
            "connections": [], "interfaces": [], "connection_count": 0,
        }
