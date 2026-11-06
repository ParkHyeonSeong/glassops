import builtins, io, json, os, threading, time
from concurrent.futures import Future, ThreadPoolExecutor, wait

import pytest

from agent.collectors import pm2

HOME = "/home/hspark/.pm2"
TITLE = f"PM2 v5.3.0: God Daemon ({HOME})"

def _proc(root, pid, ppid, cmdline="node app.js"):
    d = root / str(pid); d.mkdir(parents=True, exist_ok=True)
    (d / "stat").write_text(f"{pid} (node x) S {ppid} 0 0")
    (d / "cmdline").write_bytes(cmdline.encode() + b"\0")
    (d / "environ").write_text("SECRET=1")          # must never be read

def _proc_raw(root, pid, ppid, comm: bytes):
    d = root / str(pid); d.mkdir(parents=True, exist_ok=True)
    (d / "stat").write_bytes(f"{pid} (".encode() + comm + f") S {ppid} 0 0".encode())
    (d / "cmdline").write_bytes(b"node app.js\0")

def _pidfile(root, daemon, name, pid, home=HOME):
    p = root / str(daemon) / "root" / home.lstrip("/") / "pids"; p.mkdir(parents=True, exist_ok=True)
    (p / name).write_text(f"{pid}\n")

def _inline(discover):
    """Runs a discovery at once on the calling thread: most tests check what it finds, not where."""
    f = Future()
    try:
        f.set_result(discover())
    except Exception as e:
        f.set_exception(e)
    return f

def _collector(root, stats, **kw):
    return pm2.Pm2Collector(str(root), stats=lambda pid: stats.get(pid),
                            in_container=kw.pop("in_container", lambda pid: False),
                            submit=kw.pop("submit", _inline), **kw)

def _names(apps):
    return [a["name"] for a in apps]

def _until(cond, timeout=2.0):
    deadline = time.monotonic() + timeout
    while not cond() and time.monotonic() < deadline:
        time.sleep(0.01)
    return cond()

def _cap_lines(caplog):
    return [r.getMessage() for r in caplog.records if "skipped" in r.getMessage()]

def test_parse_daemon_home():
    assert pm2.parse_daemon_home(TITLE) == HOME
    assert pm2.parse_daemon_home("PM2 v5.3.0: God Daemon (/root/.pm2)  ") == "/root/.pm2"
    for bad in ["node app.js", "PM2 v5: God Daemon (rel/.pm2)", "PM2 v5: God Daemon (/home/x/../../etc)",
                "PM2 v5: God Daemon (/home//x/.pm2)", "PM2 v5: God Daemon (/" + "a" * 5000 + ")"]:
        assert pm2.parse_daemon_home(bad) is None

def test_parse_pid_filename():
    assert pm2.parse_pid_filename("my-app-1-0.pid") == ("my-app-1", 0)
    assert pm2.parse_pid_filename("api-12.pid") == ("api", 12)
    for bad in ["api.pid", "-3.pid", "api-x.pid", "api-1.txt"]:
        assert pm2.parse_pid_filename(bad) is None

def test_read_pid_file_safety(tmp_path):
    good = tmp_path / "a-0.pid"; good.write_text("4123\n")
    assert pm2.read_pid_file(str(good)) == 4123
    (tmp_path / "l-0.pid").symlink_to(good)
    (tmp_path / "big-0.pid").write_text("1" * 40)
    (tmp_path / "bad-0.pid").write_text("12ab")
    (tmp_path / "hi-0.pid").write_bytes(b"41\xff23\n")
    (tmp_path / "dir-0.pid").mkdir()
    (tmp_path / "zero-0.pid").write_text("0\n")
    os.mkfifo(tmp_path / "fifo-0.pid")
    keep = os.open(tmp_path / "fifo-0.pid", os.O_RDWR)      # FIFO holding a valid-looking pid
    try:
        os.write(keep, b"4123\n")
        for name in ["l-0.pid", "big-0.pid", "bad-0.pid", "hi-0.pid", "dir-0.pid", "zero-0.pid", "fifo-0.pid"]:
            assert pm2.read_pid_file(str(tmp_path / name)) is None
    finally:
        os.close(keep)

def test_collect_fork_and_cluster(tmp_path):
    _proc(tmp_path, 100, 1, TITLE)
    for pid in (200, 300, 301): _proc(tmp_path, pid, 100)
    _pidfile(tmp_path, 100, "admin-api-0.pid", 200)
    _pidfile(tmp_path, 100, "api-1.pid", 300); _pidfile(tmp_path, 100, "api-2.pid", 301)
    stats = {200: (3.0, 1000), 300: (1.0, 500), 301: (2.0, 700)}
    assert _collector(tmp_path, stats).collect() == [
        {"name": "admin-api", "home": HOME, "instances": 1, "pids": [200], "cpu_percent": 3.0, "mem_bytes": 1000},
        {"name": "api", "home": HOME, "instances": 2, "pids": [300, 301], "cpu_percent": 3.0, "mem_bytes": 1200}]

def test_container_daemons_skipped(tmp_path):
    _proc(tmp_path, 100, 1, TITLE); _proc(tmp_path, 200, 100); _pidfile(tmp_path, 100, "a-0.pid", 200)
    assert _collector(tmp_path, {200: (1.0, 1)}, in_container=lambda pid: pid == 100).collect() == []

def test_stale_foreign_and_grandchild_pids(tmp_path):
    _proc(tmp_path, 100, 1, TITLE); _proc(tmp_path, 400, 1); _proc(tmp_path, 450, 100); _proc(tmp_path, 500, 450)
    _pidfile(tmp_path, 100, "gone-0.pid", 999)       # no such process
    _pidfile(tmp_path, 100, "foreign-1.pid", 400)    # not under the daemon
    _pidfile(tmp_path, 100, "deep-2.pid", 500)       # grandchild: accepted
    out = _collector(tmp_path, {400: (1.0, 1), 500: (2.0, 2)}).collect()
    assert [a["name"] for a in out] == ["deep"]

def test_ppid_walk_handles_odd_comm(tmp_path):
    _proc(tmp_path, 100, 1, TITLE)
    _proc_raw(tmp_path, 200, 100, "app-결제서버".encode()[:15])   # cut mid-character: not UTF-8
    _proc_raw(tmp_path, 201, 100, b"node (x) y")                  # ")" inside comm
    _proc_raw(tmp_path, 202, 100, b"a) S 1 (b")                   # fake fields: last ")" rule
    _pidfile(tmp_path, 100, "kr-0.pid", 200); _pidfile(tmp_path, 100, "par-1.pid", 201)
    _pidfile(tmp_path, 100, "fake-2.pid", 202)
    stats = {200: (1.0, 1), 201: (1.0, 1), 202: (1.0, 1)}
    assert [a["name"] for a in _collector(tmp_path, stats).collect()] == ["fake", "kr", "par"]

def test_pid_files_capped_at_256(tmp_path, caplog):
    _proc(tmp_path, 100, 1, TITLE)
    for i in range(300):
        _proc(tmp_path, 1000 + i, 100); _pidfile(tmp_path, 100, f"w-{i}.pid", 1000 + i)
    out = _collector(tmp_path, {1000 + i: (0.0, 1) for i in range(300)}).collect()
    assert sum(len(a["pids"]) for a in out) == 256
    assert len(_cap_lines(caplog)) == 1

def test_daemons_capped_at_32(tmp_path, caplog):
    # 40 daemons (pids 90..129) with one app each. Pid order is the reverse of home order and
    # differs from text order ("100" < "90"), so the 32 kept (the lowest pids) show the rule.
    homes = [f"/home/u{39 - i:02d}/.pm2" for i in range(40)]
    for i, home in enumerate(homes):
        _proc(tmp_path, 90 + i, 1, f"PM2 v5.3.0: God Daemon ({home})")
        _proc(tmp_path, 2000 + i, 90 + i); _pidfile(tmp_path, 90 + i, "app-0.pid", 2000 + i, home=home)
    t = [0.0]
    c = _collector(tmp_path, {2000 + i: (0.0, 1) for i in range(40)}, clock=lambda: t[0])
    assert [a["home"] for a in c.collect()] == sorted(homes[:32])
    t[0] = 11.0
    assert len(c.collect()) == 32
    assert len(_cap_lines(caplog)) == 1                 # once per collector, not per discovery

def test_apps_capped_at_256(tmp_path, caplog):
    # Daemon 100 (home b) runs 200 apps and daemon 101 (home a) 57: 257 in all. Kept: by daemon
    # pid, then by app name, so all of daemon 100's and the first 56 names of daemon 101's. Pids
    # run against the names, home order against pid order, and "x+1-0.pid" sorts before
    # "x-0.pid" though "x" is the smaller name, so no other order keeps the same set.
    names = {100: [f"w{i:03d}" for i in range(200)], 101: [f"w{i:03d}" for i in range(55)] + ["x", "x+1"]}
    stats = {}
    for daemon, home in ((100, "/home/b/.pm2"), (101, "/home/a/.pm2")):
        _proc(tmp_path, daemon, 1, f"PM2 v5.3.0: God Daemon ({home})")
        for i, name in enumerate(names[daemon]):
            pid = daemon * 1000 + len(names[daemon]) - i
            _proc(tmp_path, pid, daemon); _pidfile(tmp_path, daemon, f"{name}-0.pid", pid, home=home)
            stats[pid] = (0.0, 1)
    out = _collector(tmp_path, stats).collect()
    assert len(out) == 256
    assert [a["name"] for a in out if a["home"] == "/home/b/.pm2"] == names[100]
    assert [a["name"] for a in out if a["home"] == "/home/a/.pm2"] == names[101][:56]
    assert len(_cap_lines(caplog)) == 1

def test_apps_capped_at_64_kib_as_sent(tmp_path, caplog):
    # Daemon 100's PM2 home is 4000 bytes of control characters, each sent as \u0001 (six
    # characters): after daemon 99's short app only a000 and a001 of its 300 apps fit in 64 KiB.
    # The budget then stops, so daemon 101's short app is skipped too: what is kept is a prefix
    # of the order (daemon pid, then app name).
    long_home = "/" + "/".join(["\x01" * 249] * 16)
    found = {"99": ("/home/u/.pm2", {"z": {990}}),
             "100": (long_home, {f"a{i:03d}": {1000 + i} for i in reversed(range(300))}),
             "101": ("/home/v/.pm2", {"y": {1990}})}
    stats = {p: (1.0, 1) for _, apps in found.values() for pids in apps.values() for p in pids}
    t = [0.0]
    c = _collector(tmp_path, stats, clock=lambda: t[0])
    c._daemons = lambda: [(pid, home) for pid, (home, _) in found.items()]
    c._daemon_apps = lambda pid, home: found[pid][1]
    out = c.collect()
    assert len(long_home.encode()) == 4000 and len(json.dumps(out)) <= 65536
    assert sorted((a["home"], a["name"]) for a in out) == sorted(
        [("/home/u/.pm2", "z"), (long_home, "a000"), (long_home, "a001")])
    t[0] = 11.0
    assert len(c.collect()) == 3                        # the next discovery is cut the same way
    lines = _cap_lines(caplog)
    assert len(lines) == 1 and "65536 characters" in lines[0]

def test_normal_host_is_not_cut_by_the_budget(tmp_path, caplog):
    _proc(tmp_path, 100, 1, TITLE)
    for i, name in enumerate(["api", "web", "worker"]):
        _proc(tmp_path, 200 + i, 100); _pidfile(tmp_path, 100, f"{name}-{i}.pid", 200 + i)
    out = _collector(tmp_path, {200: (1.0, 10), 201: (2.0, 20), 202: (3.0, 30)}).collect()
    assert [(a["name"], a["pids"]) for a in out] == [("api", [200]), ("web", [201]), ("worker", [202])]
    assert _cap_lines(caplog) == []

def test_each_cap_is_logged_once(tmp_path, caplog):
    c = _collector(tmp_path, {})
    for what in ("32 PM2 daemons", "256 PM2 apps", "32 PM2 daemons", "256 PM2 apps"):
        c._skipped(what)
    assert _cap_lines(caplog) == ["pm2: more than 32 PM2 daemons; the rest are skipped",
                                  "pm2: more than 256 PM2 apps; the rest are skipped"]

def _entry_chars(home, name="a000", pid=1000000):
    """Characters of one pm2_apps entry as the agent sends it, with CPU 0.0 and RSS 0."""
    return len(json.dumps({"name": name, "home": home, "instances": 1, "pids": [pid],
                           "cpu_percent": 0.0, "mem_bytes": 0}))

def _home_of_size(chars):
    """A PM2 home (under 4096 bytes) whose app entry takes exactly `chars` characters as sent:
    100 "é" and then control characters (six characters each), topped up with ASCII."""
    home = "/" + "é" * 100
    home += "\x01" * ((chars - _entry_chars(home)) // 6)
    return home + "b" * (chars - _entry_chars(home))

@pytest.mark.parametrize("chars, kept", [(21805, 2), (16343, 4)])
def test_apps_budget_is_exact_for_the_widest_values(tmp_path, chars, kept):
    # Each app takes `chars` characters with CPU 0.0 and RSS 0, and 39 more with the widest values a
    # cycle can send: a 23-character float and a 20-digit RSS. With 21805 a third app fits only
    # without that room, the ", " separators or the brackets; with 16343 four apps fill the
    # 65536 characters exactly.
    home = _home_of_size(chars)
    apps = {f"a{i:03d}": {1000000 + i} for i in range(10)}
    c = _collector(tmp_path, {1000000 + i: (1.2345678901234567e+100, 10 ** 19) for i in range(10)})
    c._daemons = lambda: [("100", home)]
    c._daemon_apps = lambda pid, h: apps
    out = c.collect()
    assert len(home.encode()) < 4096 and _entry_chars(home) == chars
    assert [a["name"] for a in out] == [f"a{i:03d}" for i in range(kept)]
    assert len(json.dumps(out)) <= 65536

def test_never_opens_environ(tmp_path, monkeypatch):
    _proc(tmp_path, 100, 1, TITLE); _proc(tmp_path, 200, 100); _pidfile(tmp_path, 100, "a-0.pid", 200)
    opened, real_open, real_os_open = [], builtins.open, os.open
    def spy(fn):
        return lambda p, *a, **k: (opened.append((str(p), threading.current_thread().name)), fn(p, *a, **k))[1]
    monkeypatch.setattr(builtins, "open", spy(real_open))
    monkeypatch.setattr(io, "open", spy(real_open))
    monkeypatch.setattr(os, "open", spy(real_os_open))
    c = _collector(tmp_path, {200: (1.0, 1)}, submit=None)     # the agent's own discovery thread
    try:
        assert _until(lambda: _names(c.collect()) == ["a"])
    finally:
        pm2.reset_collector()
    assert any(thread.startswith("pm2-discovery") for _, thread in opened)
    assert not any(p.endswith("environ") for p, _ in opened)

def test_rediscovery_rate_limited(tmp_path):
    t = [0.0]
    _proc(tmp_path, 100, 1, TITLE); _proc(tmp_path, 200, 100); _proc(tmp_path, 201, 100)
    _pidfile(tmp_path, 100, "a-0.pid", 200)
    c = _collector(tmp_path, {200: (1.0, 1), 201: (1.0, 1)}, clock=lambda: t[0])
    assert [a["name"] for a in c.collect()] == ["a"]
    _pidfile(tmp_path, 100, "b-1.pid", 201)
    t[0] = 5.0;  assert [a["name"] for a in c.collect()] == ["a"]
    t[0] = 11.0; assert [a["name"] for a in c.collect()] == ["a", "b"]

# --- discovery off the caller's thread ------------------------------------------------

class _Threaded:
    """A collector whose discoveries run on a one-thread pool of the test's own. While `gate`
    is clear a discovery waits at its start; `submitted` keeps every discovery, in order."""

    def __init__(self, root, stats, **kw):
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="pm2-test")
        self.gate = threading.Event()
        self.submitted: list[Future] = []
        self.c = _collector(root, stats, submit=self._submit, **kw)

    def _submit(self, discover):
        def held():
            if not self.gate.wait(5):
                raise TimeoutError("gate never opened")
            return discover()
        self.submitted.append(self.pool.submit(held))
        return self.submitted[-1]

    def finish(self):
        """Opens the gate and waits until the last discovery has finished."""
        self.gate.set()
        assert not wait(self.submitted[-1:], timeout=5).not_done

@pytest.fixture
def threaded(tmp_path):
    made = []
    def make(stats, **kw):
        made.append(_Threaded(tmp_path, stats, **kw))
        return made[-1]
    yield make
    for h in made:
        h.gate.set()
        h.pool.shutdown(wait=True)

def _tree(root):
    """Daemon 100 runs app a (pid 200); pid 201 is its child for an app added later."""
    _proc(root, 100, 1, TITLE); _proc(root, 200, 100); _proc(root, 201, 100)
    _pidfile(root, 100, "a-0.pid", 200)

STATS = {200: (1.0, 1), 201: (1.0, 1)}

def _promptly(collect):
    start = time.monotonic()
    out = collect()
    assert time.monotonic() - start < 0.5, "collect() waited for the discovery"
    return out

def test_collect_never_waits_for_discovery(tmp_path, threaded):
    _tree(tmp_path)
    t = [0.0]
    h = threaded(STATS, clock=lambda: t[0])
    assert _promptly(h.c.collect) == []                 # nothing before the first one finishes
    h.finish()
    assert _names(h.c.collect()) == ["a"]
    _pidfile(tmp_path, 100, "b-1.pid", 201)
    h.gate.clear(); t[0] = 11.0                         # the next one hangs at its start
    for _ in range(3):
        assert _names(_promptly(h.c.collect)) == ["a"]  # the previous result meanwhile
    assert len(h.submitted) == 2 and not h.submitted[1].done()

def test_one_discovery_at_a_time(tmp_path, threaded):
    _tree(tmp_path)
    t = [0.0]
    h = threaded(STATS, clock=lambda: t[0])
    for now in (0.0, 11.0, 25.0):
        t[0] = now
        h.c.collect()
    assert len(h.submitted) == 1                        # still running at 25 s: none started since
    h.finish()
    h.c.collect()
    t[0] = 40.0
    h.c.collect()
    assert len(h.submitted) == 2                        # finished, and due: the next one starts

def test_next_collect_reports_a_finished_discovery(tmp_path, threaded):
    _tree(tmp_path)
    t = [0.0]
    h = threaded(STATS, clock=lambda: t[0])
    h.c.collect(); h.finish()
    assert _names(h.c.collect()) == ["a"]
    _pidfile(tmp_path, 100, "b-1.pid", 201)
    h.gate.clear(); t[0] = 11.0
    assert _names(h.c.collect()) == ["a"]
    h.finish()                                          # the gate opens and the discovery completes
    assert _names(h.c.collect()) == ["a", "b"]

def test_failed_discovery_keeps_the_last_result(tmp_path, threaded, caplog):
    _tree(tmp_path)
    t, fail = [0.0], [False]
    def in_container(pid):
        if fail[0]:
            raise OSError("boom")
        return False
    h = threaded(STATS, clock=lambda: t[0], in_container=in_container)
    h.c.collect(); h.finish()
    assert _names(h.c.collect()) == ["a"]
    fail[0] = True
    for now in (11.0, 22.0):
        t[0] = now
        h.c.collect(); h.finish()
        assert _names(h.c.collect()) == ["a"]           # the last good result stays in use
    assert all(isinstance(f.exception(), OSError) for f in h.submitted[1:])
    assert [r.getMessage() for r in caplog.records if "discovery failed" in r.getMessage()] == ["pm2 discovery failed"]

def test_no_discovery_without_proc_root(tmp_path):
    def submit(discover):
        raise AssertionError("discovery started without a proc root")
    c = _collector(tmp_path / "missing", {}, submit=submit)
    assert c.collect() == [] and c.collect() == []

def _discovery_threads():
    return [t.name for t in threading.enumerate() if t.name.startswith("pm2-discovery")]

def test_reset_collector_leaves_no_discovery_behind(tmp_path, monkeypatch):
    gate, ran, hung = threading.Event(), [], []
    def discover(self):
        ran.append(self)
        # Only the first collector's discovery hangs, like a dead mount: chosen by identity, as the
        # two threads may reach this point in either order.
        if self is hung[0] and not gate.wait(5):
            raise TimeoutError("gate never opened")
        return {}
    monkeypatch.setattr(pm2.Pm2Collector, "_discover", discover)
    pools = []                                          # kept alive: only reset_collector may stop them
    try:
        pm2.reset_collector(str(tmp_path))
        hung.append(pm2._collector)
        pm2.collect()
        pools.append(pm2._pool)
        pm2.reset_collector(str(tmp_path))
        pm2.collect()                                   # a fresh thread: not queued behind the hung one
        pools.append(pm2._pool)
        assert _until(lambda: len(ran) == 2) and not hung[0]._pending.done()
    finally:
        gate.set()
        pm2.reset_collector(str(tmp_path))
    assert _until(lambda: not _discovery_threads()), _discovery_threads()

def test_module_collect_never_raises(monkeypatch):
    class Boom:
        def collect(self): raise RuntimeError("x")
    monkeypatch.setattr(pm2, "_collector", Boom())
    assert pm2.collect() == []

async def test_snapshot_always_has_pm2_apps(monkeypatch):
    import agent.main as m
    monkeypatch.setattr(m.pm2, "collect", lambda: [])
    assert (await m.collect_metrics())["pm2_apps"] == []
