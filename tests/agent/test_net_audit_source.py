import os
from agent.collectors.net_audit import HostNetnsProcSource


def _write_netns(tmp_path, pid=1):
    net = tmp_path / str(pid) / "net"
    net.mkdir(parents=True)
    (net / "tcp").write_text(
        "  sl  local_address rem_address st ... inode\n"
        "   0: 0100007F:1F90 0100007F:8AE2 01 0 0 0 0 0 67890 1 ...\n"
    )
    (net / "tcp6").write_text("header\n")
    (net / "udp").write_text("header\n")
    (net / "udp6").write_text("header\n")
    (net / "dev").write_text(
        "h\nh\n  eth0: 1000 10 0 0 0 0 0 0 2000 20 0 0 0 0 0 0\n"
    )


def _write_owner(tmp_path, pid, comm, inode):
    fddir = tmp_path / str(pid) / "fd"
    fddir.mkdir(parents=True)
    (tmp_path / str(pid) / "comm").write_text(comm + "\n")
    os.symlink(f"socket:[{inode}]", fddir / "3")


def test_snapshot_enriches_pid_and_pname(tmp_path):
    _write_netns(tmp_path)
    _write_owner(tmp_path, 4321, "sshd", 67890)
    src = HostNetnsProcSource(host_proc=str(tmp_path))
    snap = src.snapshot()
    assert snap.ok is True
    est = [c for c in snap.conns if c["status"] == "ESTABLISHED"][0]
    assert est["pid"] == 4321
    assert est["pname"] == "sshd"


def test_snapshot_reports_unavailable_when_netns_unreadable(tmp_path):
    # Review P1: nothing written -> tcp/tcp6 unreadable -> ok=False (NOT an empty
    # snapshot, which would look like every connection closing at once).
    src = HostNetnsProcSource(host_proc=str(tmp_path))
    snap = src.snapshot()
    assert snap.ok is False
    assert snap.conns == []
    assert snap.reason  # non-empty degraded reason


def test_snapshot_ok_false_on_partial_read(tmp_path):
    # Review P1 (round 5): tcp missing but tcp6/udp present is a PARTIAL read — it must
    # be ok=False, not a partial snapshot (which would false-close all tcp connections).
    net = tmp_path / "1" / "net"
    net.mkdir(parents=True)
    for n in ("tcp6", "udp", "udp6"):
        (net / n).write_text("header\n")
    (net / "dev").write_text("h\nh\n  eth0: 1 1 0 0 0 0 0 0 2 2 0 0 0 0 0 0\n")
    # NOTE: no tcp file written.
    snap = HostNetnsProcSource(host_proc=str(tmp_path)).snapshot()
    assert snap.ok is False


def test_snapshot_ok_false_when_udp_missing(tmp_path):
    # Review P3: udp is a REQUIRED proto (present on every Linux netns), so its absence
    # is a partial read -> ok=False, not a tolerated optional like tcp6/udp6.
    net = tmp_path / "1" / "net"
    net.mkdir(parents=True)
    for n in ("tcp", "tcp6", "udp6"):
        (net / n).write_text("header\n")
    (net / "dev").write_text("h\nh\n  eth0: 1 1 0 0 0 0 0 0 2 2 0 0 0 0 0 0\n")
    # NOTE: no udp file written.
    snap = HostNetnsProcSource(host_proc=str(tmp_path)).snapshot()
    assert snap.ok is False


def test_snapshot_ok_when_ipv6_protos_absent(tmp_path):
    # IPv6 disabled: tcp6/udp6 files simply don't exist. tcp present -> ok=True, and
    # the absent optional protos are tolerated (not treated as a read failure).
    net = tmp_path / "1" / "net"
    net.mkdir(parents=True)
    (net / "tcp").write_text(
        "  sl  local_address rem_address st ... inode\n"
        "   0: 0100007F:1F90 0100007F:8AE2 01 0 0 0 0 0 67890 1 ...\n"
    )
    (net / "udp").write_text("header\n")
    (net / "dev").write_text("h\nh\n  eth0: 1 1 0 0 0 0 0 0 2 2 0 0 0 0 0 0\n")
    # NOTE: no tcp6/udp6 files.
    snap = HostNetnsProcSource(host_proc=str(tmp_path)).snapshot()
    assert snap.ok is True
    assert len(snap.conns) == 1


def test_interface_counters(tmp_path):
    _write_netns(tmp_path)
    src = HostNetnsProcSource(host_proc=str(tmp_path))
    ifs = src.interface_counters()
    assert ifs["eth0"]["bytes_out"] == 2000
