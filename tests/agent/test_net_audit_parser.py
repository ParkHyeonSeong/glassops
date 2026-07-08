from agent.collectors.net_audit import parse_proc_net

# Real /proc/net/tcp format: sl local_address rem_address st ... inode
TCP_SAMPLE = (
    "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode\n"
    "   0: 0100007F:1F90 00000000:0000 0A 00000000:00000000 00:00000000 00000000     0        0 12345 1 ...\n"
    "   1: 0100007F:8AE2 0100007F:1F90 01 00000000:00000000 00:00000000 00000000  1000        0 67890 1 ...\n"
)


def test_parse_tcp_listen_and_established():
    rows = parse_proc_net(TCP_SAMPLE, "tcp")
    assert len(rows) == 2
    listen, est = rows[0], rows[1]
    # 0100007F little-endian -> 127.0.0.1 ; 1F90 -> 8080
    assert listen["laddr"] == "127.0.0.1"
    assert listen["lport"] == 8080
    assert listen["raddr"] == "0.0.0.0"
    assert listen["rport"] == 0
    assert listen["status"] == "LISTEN"
    assert listen["inode"] == 12345
    assert est["status"] == "ESTABLISHED"
    assert est["rport"] == 8080
    assert est["inode"] == 67890


def test_parse_udp_has_empty_status():
    udp = (
        "  sl  local_address rem_address   st ... inode\n"
        "   0: 0100007F:0035 00000000:0000 07 00000000:00000000 00:00000000 00000000 0 0 11111 2 ...\n"
    )
    rows = parse_proc_net(udp, "udp")
    assert rows[0]["lport"] == 53
    assert rows[0]["status"] == ""
    assert rows[0]["inode"] == 11111


def test_parse_ipv6():
    # ::1 = 0000...0001 rendered as 32 hex chars, per-4-byte little-endian words
    v6 = (
        "  sl  local_address rem_address st ... inode\n"
        "   0: 00000000000000000000000001000000:1F90 00000000000000000000000000000000:0000 0A "
        "00000000:00000000 00:00000000 00000000 0 0 22222 1 ...\n"
    )
    rows = parse_proc_net(v6, "tcp6")
    assert rows[0]["laddr"] == "::1"
    assert rows[0]["lport"] == 8080
    assert rows[0]["inode"] == 22222
