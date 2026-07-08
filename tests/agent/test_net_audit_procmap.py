import os
from agent.collectors.net_audit import build_inode_pid_map, parse_proc_net_dev


def _make_proc(tmp_path, pid, comm, inode):
    pdir = tmp_path / str(pid)
    fddir = pdir / "fd"
    fddir.mkdir(parents=True)
    (pdir / "comm").write_text(comm + "\n")
    # symlink fd -> socket:[inode]
    os.symlink(f"socket:[{inode}]", fddir / "3")
    # a non-socket fd should be ignored
    (tmp_path / "target.txt").write_text("x")
    os.symlink(str(tmp_path / "target.txt"), fddir / "4")


def test_build_inode_pid_map(tmp_path):
    _make_proc(tmp_path, 4321, "sshd", 67890)
    m = build_inode_pid_map(str(tmp_path))
    assert m[67890] == (4321, "sshd")


def test_map_skips_unreadable_pids(tmp_path):
    # a "pid" dir without fd/ must not crash the scan
    (tmp_path / "999").mkdir()
    _make_proc(tmp_path, 4321, "nginx", 11111)
    m = build_inode_pid_map(str(tmp_path))
    assert m[11111] == (4321, "nginx")


def test_map_resolves_only_wanted_inodes(tmp_path):
    # Review P2: with a wanted set, only those inodes are resolved (others skipped).
    _make_proc(tmp_path, 4321, "sshd", 67890)
    _make_proc(tmp_path, 4322, "nginx", 11111)
    m = build_inode_pid_map(str(tmp_path), wanted={67890})
    assert m == {67890: (4321, "sshd")}


def test_parse_proc_net_dev():
    dev = (
        "Inter-|   Receive                                                |  Transmit\n"
        " face |bytes    packets errs drop fifo frame compressed multicast|bytes    packets\n"
        "  eth0: 1000      10    0    0    0     0          0         0     2000      20 0 0 0 0 0 0\n"
        "    lo:  500       5    0    0    0     0          0         0      500       5 0 0 0 0 0 0\n"
    )
    out = parse_proc_net_dev(dev)
    assert out["eth0"]["bytes_in"] == 1000
    assert out["eth0"]["bytes_out"] == 2000
    assert out["eth0"]["packets_in"] == 10
    assert out["lo"]["bytes_out"] == 500
