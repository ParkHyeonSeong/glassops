from agent.collectors.net_audit import NetAuditCollector, Snapshot


class FakeSource:
    def __init__(self, snapshots, iface_seq=None):
        # Each snapshot entry is a list[dict] (wrapped as ok=True) OR a Snapshot
        # (passed through, so a test can inject an ok=False / unavailable tick).
        self._snaps = list(snapshots)
        # iface_seq: one counter dict consumed per interface_counters() call
        # (called at first collect for baseline + once per rollup flush).
        self._iface_seq = list(iface_seq or [])
        self.i = -1
        self.j = -1

    def snapshot(self):
        self.i += 1
        s = self._snaps[min(self.i, len(self._snaps) - 1)]
        return s if isinstance(s, Snapshot) else Snapshot(s, ok=True)

    def interface_counters(self):
        if not self._iface_seq:
            return {}
        self.j += 1
        return self._iface_seq[min(self.j, len(self._iface_seq) - 1)]


def _conn(raddr="10.0.0.5", rport=443, pid=100, pname="curl"):
    return {"proto": "tcp", "laddr": "10.0.0.9", "lport": 55000,
            "raddr": raddr, "rport": rport, "status": "ESTABLISHED",
            "inode": 1, "pid": pid, "pname": pname}


def test_open_then_close_event_with_duration():
    clock = iter([1000.0, 1005.0, 1010.0])
    src = FakeSource([[_conn()], [_conn()], []])
    c = NetAuditCollector(src, clock=lambda: next(clock))

    r1 = c.collect()  # first snapshot: new -> open
    assert [e["event"] for e in r1["events"]] == ["open"]
    assert r1["events"][0]["raddr"] == "10.0.0.5"

    r2 = c.collect()  # unchanged -> no events
    assert r2["events"] == []

    r3 = c.collect()  # gone -> close with duration = 1010 - 1000
    assert [e["event"] for e in r3["events"]] == ["close"]
    assert r3["events"][0]["duration"] == 10.0


def test_close_preserves_process_attribution():
    # Review P2: the close event must report the process from the open, not blanks.
    clock = iter([1.0, 2.0])
    src = FakeSource([[_conn(pid=4321, pname="sshd")], []])
    c = NetAuditCollector(src, clock=lambda: next(clock))
    c.collect()                       # open
    close = c.collect()["events"][0]  # close
    assert close["event"] == "close"
    assert close["pid"] == 4321
    assert close["pname"] == "sshd"
    assert close["status"] == "ESTABLISHED"


def test_listen_and_wildcard_raddr_excluded():
    clock = iter([1.0])
    listen = {"proto": "tcp", "laddr": "0.0.0.0", "lport": 22, "raddr": "0.0.0.0",
              "rport": 0, "status": "LISTEN", "inode": 2, "pid": 1, "pname": "sshd"}
    src = FakeSource([[listen]])
    c = NetAuditCollector(src, clock=lambda: next(clock))
    assert c.collect()["events"] == []


def test_cap_keeps_closes_first_and_reports_dropped():
    # Review P2: 5 closes + 250 opens, cap 200 -> all closes kept, dropped counted.
    clock = iter([1.0, 2.0])
    closing = [_conn(raddr=f"9.9.9.{i}") for i in range(5)]
    src = FakeSource([closing, [_conn(raddr=f"10.0.0.{i}") for i in range(250)]])
    c = NetAuditCollector(src, max_events=200, clock=lambda: next(clock))
    c.collect()                 # opens the 5 "closing" conns
    out = c.collect()           # those 5 close + 250 new opens
    assert len(out["events"]) == 200
    close_count = sum(1 for e in out["events"] if e["event"] == "close")
    assert close_count == 5     # every close survived the cap
    assert out["dropped"] == (5 + 250) - 200


def test_rollup_bytes_are_per_bucket_deltas():
    # Review P1: rollup interface bytes = delta over the bucket, not cumulative.
    clock = iter([60.0, 65.0, 121.0])
    src = FakeSource(
        [[_conn()], [_conn()], [_conn()]],
        iface_seq=[
            {"eth0": {"bytes_in": 100, "bytes_out": 200, "packets_in": 1, "packets_out": 1}},  # baseline @60
            {"eth0": {"bytes_in": 150, "bytes_out": 260, "packets_in": 2, "packets_out": 2}},  # flush @121
        ],
    )
    c = NetAuditCollector(src, clock=lambda: next(clock))
    assert c.collect()["rollups"] == []      # bucket 1 opens (baseline captured)
    assert c.collect()["rollups"] == []      # still bucket 1
    r = c.collect()                          # 121s -> bucket 1 flushes
    assert len(r["rollups"]) == 1
    iface = r["rollups"][0]["interfaces"][0]
    assert iface["name"] == "eth0"
    assert iface["bytes_in"] == 50           # 150 - 100, not 150
    assert iface["bytes_out"] == 60          # 260 - 200
    assert r["rollups"][0]["top_talkers"][0]["raddr"] == "10.0.0.5"


def test_rollup_clamps_counter_reset():
    # A counter reset (reboot/if reset) must not yield negative deltas.
    clock = iter([60.0, 121.0])
    src = FakeSource(
        [[], []],
        iface_seq=[
            {"eth0": {"bytes_in": 1000, "bytes_out": 1000, "packets_in": 1, "packets_out": 1}},
            {"eth0": {"bytes_in": 10, "bytes_out": 10, "packets_in": 1, "packets_out": 1}},
        ],
    )
    c = NetAuditCollector(src, clock=lambda: next(clock))
    c.collect()
    iface = c.collect()["rollups"][0]["interfaces"][0]
    assert iface["bytes_in"] == 0            # clamped, not -990


def test_rollup_skips_iface_without_baseline():
    # Review P2: if /proc/net/dev was unreadable at bucket start (baseline {}), the
    # next flush must NOT store the full cumulative counter as a one-minute delta —
    # eth0 has no baseline, so it is omitted rather than reported as 900 bytes/min.
    clock = iter([60.0, 121.0])
    src = FakeSource(
        [[_conn()], [_conn()]],
        iface_seq=[
            {},                                                                        # baseline @60: /net/dev unreadable
            {"eth0": {"bytes_in": 900, "bytes_out": 900, "packets_in": 9, "packets_out": 9}},  # flush @121
        ],
    )
    c = NetAuditCollector(src, clock=lambda: next(clock))
    assert c.collect()["rollups"] == []          # @60 baseline (empty)
    r = c.collect()                              # @121 flush
    assert len(r["rollups"]) == 1
    assert r["rollups"][0]["interfaces"] == []   # no baseline for eth0 -> not emitted


def test_long_lived_connection_counts_in_every_bucket():
    # Review P2: a persistent tunnel (same conn across buckets) must appear in the
    # top-talkers of each bucket, not just the one it opened in.
    clock = iter([60.0, 121.0, 181.0])
    src = FakeSource([[_conn()], [_conn()], [_conn()]])   # same conn throughout
    c = NetAuditCollector(src, clock=lambda: next(clock))
    assert c.collect()["rollups"] == []                  # bucket A opens
    r1 = c.collect()                                     # 121s -> flush bucket A
    r2 = c.collect()                                     # 181s -> flush bucket B
    assert r1["rollups"][0]["top_talkers"][0] == {"raddr": "10.0.0.5", "conns": 1}
    assert r2["rollups"][0]["top_talkers"][0] == {"raddr": "10.0.0.5", "conns": 1}


def test_source_unavailable_keeps_prev_and_emits_no_close():
    # Review P1: a failed read (ok=False) must NOT be diffed as all-closed. _prev is
    # kept, so the connection produces no spurious close and no re-open on recovery.
    clock = iter([1.0, 2.0, 3.0])
    src = FakeSource([[_conn()], Snapshot([], ok=False, reason="unreadable"), [_conn()]])
    c = NetAuditCollector(src, clock=lambda: next(clock))
    assert [e["event"] for e in c.collect()["events"]] == ["open"]   # tick 1: open
    assert c.collect() == {"events": [], "rollups": [], "dropped": 0}  # tick 2: skipped
    assert c.collect()["events"] == []                              # tick 3: still open, no re-open


def test_source_gap_across_minute_discards_stale_rollup():
    # Review P1: 60s baseline, 120s+180s unreadable (crosses two boundaries), 240s
    # recovery. The multi-minute counter jump must NOT be emitted as the 60s bucket's
    # one-minute rollup — the stale bucket is discarded and re-baselined.
    clock = iter([60.0, 120.0, 180.0, 240.0])
    src = FakeSource(
        [[_conn()],
         Snapshot([], ok=False, reason="down"),
         Snapshot([], ok=False, reason="down"),
         [_conn()]],
        iface_seq=[
            {"eth0": {"bytes_in": 100, "bytes_out": 100, "packets_in": 1, "packets_out": 1}},   # baseline @60
            {"eth0": {"bytes_in": 900, "bytes_out": 900, "packets_in": 9, "packets_out": 9}},   # re-baseline @240
        ],
    )
    c = NetAuditCollector(src, clock=lambda: next(clock))
    assert c.collect()["rollups"] == []   # @60 baseline
    assert c.collect()["rollups"] == []   # @120 down (skipped)
    assert c.collect()["rollups"] == []   # @180 down (skipped)
    assert c.collect()["rollups"] == []   # @240 recovery: stale 3-min delta discarded
