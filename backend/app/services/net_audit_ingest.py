"""Route the net_audit sub-field off the metric snapshot into its own tables,
enforcing the "metadata only" contract at the SERVER trust boundary.

Kept as a standalone seam so it is unit-testable without the WebSocket handler.
The field is POPPED from `data` so it never reaches broadcast_to_clients or the
metrics blob (net-audit data is admin-only, never fanned out). The agent is not
trusted to self-limit: every field is whitelisted, enum-checked, length-capped,
and finite-checked here before it can touch the DB."""

import ipaddress
import logging
import math

from app.database import store_net_audit

logger = logging.getLogger("glassops.net_audit")

_EVENTS = {"open", "close"}
_PROTOS = {"tcp", "tcp6", "udp", "udp6"}
_MAX_EVENTS = 500
_MAX_ROLLUPS = 5
_MAX_IFACES = 64
_MAX_TALKERS = 100
_STR_CAP = {"proto": 8, "laddr": 64, "raddr": 64, "status": 32, "pname": 64, "name": 32}


def _s(v, cap: int) -> str:
    if v is None:
        return ""
    return (v if isinstance(v, str) else str(v))[:cap]


def _int(v, lo: int, hi: int):
    if isinstance(v, bool) or not isinstance(v, int):
        return None
    return v if lo <= v <= hi else None


def _finite(v):
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    return float(v) if math.isfinite(v) else None


def _ip(v) -> str:
    """Return the canonical IP string, or "" if not a valid address. The agent is
    untrusted, so an address field is only allowed to hold a real IP — never an
    arbitrary payload string (review P2)."""
    if not isinstance(v, str) or not v:
        return ""
    try:
        return str(ipaddress.ip_address(v))
    except ValueError:
        return ""


def _clean_event(e: dict, server_ts: float):
    if not isinstance(e, dict):
        return None
    if e.get("event") not in _EVENTS or e.get("proto") not in _PROTOS:
        return None
    dur = _finite(e.get("duration"))
    if dur is not None and dur < 0:
        dur = None
    return {
        # ts is PINNED to the outer metric's server-verified timestamp — the agent's
        # per-event ts is not trusted (matches the DB column comment). All events in
        # a snapshot occurred within ~1 collect interval, so this is exact enough and
        # cannot be forged to poison time-range queries (review P2).
        "event": e["event"], "ts": server_ts, "proto": e["proto"],
        "laddr": _ip(e.get("laddr")),
        "lport": _int(e.get("lport"), 0, 65535),
        "raddr": _ip(e.get("raddr")),
        "rport": _int(e.get("rport"), 0, 65535),
        "status": _s(e.get("status"), _STR_CAP["status"]),
        "pid": _int(e.get("pid"), 0, 2**31 - 1),
        "pname": _s(e.get("pname"), _STR_CAP["pname"]),
        "duration": dur,
    }


def _clean_rollup(r: dict, server_ts: float):
    if not isinstance(r, dict):
        return None
    # Rollup ts is a legitimately-past bucket start, but a forged future value must
    # not slip in — clamp anything implausible to the server timestamp (review P2).
    ts = _finite(r.get("ts"))
    if ts is None or ts <= 0 or ts > server_ts + 300:
        ts = server_ts
    ifaces = []
    raw_if = r.get("interfaces")
    if isinstance(raw_if, list):
        for it in raw_if[:_MAX_IFACES]:
            if not isinstance(it, dict):
                continue
            ifaces.append({
                "name": _s(it.get("name"), _STR_CAP["name"]),
                "bytes_in": max(0.0, _finite(it.get("bytes_in")) or 0.0),
                "bytes_out": max(0.0, _finite(it.get("bytes_out")) or 0.0),
            })
    talkers = []
    raw_t = r.get("top_talkers")
    if isinstance(raw_t, list):
        for t in raw_t[:_MAX_TALKERS]:
            if not isinstance(t, dict):
                continue
            talkers.append({
                "raddr": _ip(t.get("raddr")),
                "conns": _int(t.get("conns"), 0, 2**31 - 1) or 0,
            })
    return {"ts": ts, "interfaces": ifaces, "top_talkers": talkers}


async def extract_and_store_net_audit(agent_id: str, timestamp: float, data: dict) -> None:
    payload = data.pop("net_audit", None)
    if not isinstance(payload, dict):
        return

    events = []
    raw_events = payload.get("events")
    if isinstance(raw_events, list):
        for e in raw_events[:_MAX_EVENTS]:
            ce = _clean_event(e, timestamp)
            if ce is not None:
                events.append(ce)

    rollups = []
    raw_rollups = payload.get("rollups")
    if isinstance(raw_rollups, list):
        for r in raw_rollups[:_MAX_ROLLUPS]:
            cr = _clean_rollup(r, timestamp)
            if cr is not None:
                rollups.append(cr)

    if not events and not rollups:
        return
    try:
        await store_net_audit(agent_id, timestamp, events, rollups)
    except Exception:
        logger.exception("Failed to store net_audit for %s", agent_id)
