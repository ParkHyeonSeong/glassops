"""Host-level PM2 apps collector.

Finds PM2 daemons by their process title, reads PM2's pid files through
<proc_root>/<daemon pid>/root and reports each running app (name, home,
instances, pids, summed CPU % and RSS). Never reads /proc/*/environ and never
reports command lines or paths beyond name and home.

Discovery (daemons and their pid files) runs on one worker thread, so a PM2
home on a hung network mount never stalls the agent's event loop; each cycle
only samples the pids that the last finished discovery found.

Imports only the standard library, psutil and cgroup_stats so it can also be
exercised in a bare container with python3-psutil.
"""

import json
import logging
import os
import re
import stat as stat_mod
import time
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Callable

import psutil

from agent.collectors import cgroup_stats

logger = logging.getLogger("glassops.agent")

_TITLE_RE = re.compile(r"^PM2 v\S+: God Daemon \((.+)\)$")
_MAX_HOME_BYTES = 4096
_MAX_DAEMONS = 32
_MAX_PID_FILES = 256
_MAX_APPS = 256
_MAX_APPS_JSON = 64 * 1024     # characters of pm2_apps as the agent sends it (json.dumps)
_MAX_PID_FILE_BYTES = 32
_MAX_PPID_HOPS = 32
_MAX_CMDLINE_BYTES = 8192 + 64
# Discovery cannot know an app's CPU % and RSS yet, so it measures each entry with the widest
# values they can print: a positive float in 23 characters and an RSS sum in 20 digits.
_WIDEST_CPU = 1.7976931348623157e308
_WIDEST_MEM = 10**20 - 1

_Apps = dict[tuple[str, str], set[int]]   # (home, app name) -> pids


def _app_entry(name: str, home: str, pids: list[int], cpu: float, mem: int) -> dict:
    """One pm2_apps entry, as the snapshot carries it."""
    return {"name": name, "home": home, "instances": len(pids), "pids": pids,
            "cpu_percent": cpu, "mem_bytes": mem}


def _sent_size(name: str, home: str, pids: set[int]) -> int:
    """The most characters this app can take in pm2_apps as the agent sends it."""
    return len(json.dumps(_app_entry(name, home, sorted(pids), _WIDEST_CPU, _WIDEST_MEM)))


def parse_daemon_home(cmdline: str) -> str | None:
    """Return PM2_HOME from a God Daemon title, or None if absent or unsafe."""
    m = _TITLE_RE.match(cmdline.strip())
    if not m:
        return None
    home = m.group(1)
    if not home.startswith("/") or home != os.path.normpath(home):
        return None
    if ".." in home.split("/"):
        return None
    if len(home.encode("utf-8", "surrogateescape")) > _MAX_HOME_BYTES:
        return None
    return home


def parse_pid_filename(filename: str) -> tuple[str, int] | None:
    """'<app name>-<pm_id>.pid' -> (app name, pm_id), splitting at the last '-'."""
    if not filename.endswith(".pid"):
        return None
    stem = filename[:-4]
    name, sep, ident = stem.rpartition("-")
    if not sep or not name or not ident.isascii() or not ident.isdigit():
        return None
    return name, int(ident)


def read_pid_file(path: str) -> int | None:
    """Read a pid file: regular file, no symlink, <= 32 bytes, positive decimal."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        if not stat_mod.S_ISREG(os.fstat(fd).st_mode):
            return None
        data = os.read(fd, _MAX_PID_FILE_BYTES + 1)
    except OSError:
        return None
    finally:
        os.close(fd)
    if len(data) > _MAX_PID_FILE_BYTES:
        return None
    text = data.strip()
    if not text.isdigit():      # bytes.isdigit() is ASCII-only
        return None
    pid = int(text)
    return pid if pid > 0 else None


class _PsutilStats:
    """Cached psutil.Process per pid; CPU % needs the previous sample."""

    def __init__(self) -> None:
        self._procs: dict[int, psutil.Process] = {}

    def __call__(self, pid: int) -> tuple[float, int] | None:
        try:
            proc = self._procs.get(pid)
            if proc is None:
                proc = self._procs[pid] = psutil.Process(pid)
            return proc.cpu_percent(None), proc.memory_info().rss
        except (psutil.Error, OSError):
            self._procs.pop(pid, None)
            return None


class Pm2Collector:
    def __init__(
        self,
        proc_root: str,
        stats: Callable[[int], tuple[float, int] | None] | None = None,
        in_container: Callable[[int], bool] | None = None,
        clock: Callable[[], float] = time.monotonic,
        rediscover_s: float = 10.0,
        submit: Callable[[Callable[[], _Apps]], Future[_Apps]] | None = None,
    ) -> None:
        self._root = proc_root
        self._stats = stats or _PsutilStats()
        self._in_container = in_container or (
            lambda pid: cgroup_stats.container_id_for_pid(pid) is not None
        )
        self._clock = clock
        self._rediscover_s = rediscover_s
        self._submit = submit or _submit_discovery
        self._last: float | None = None             # when the last discovery started
        self._pending: Future[_Apps] | None = None  # the discovery in flight, if any
        self._apps: _Apps = {}
        self._failure_logged = False
        self._skip_logged: set[str] = set()

    # --- discovery (on the discovery thread) -------------------------------
    def _read_cmdline(self, pid: str) -> str | None:
        try:
            with open(os.path.join(self._root, pid, "cmdline"), "rb") as f:
                raw = f.read(_MAX_CMDLINE_BYTES)
        except OSError:
            return None
        return raw.replace(b"\0", b" ").decode("utf-8", "replace").strip()

    def _ppid(self, pid: int) -> int | None:
        try:
            with open(os.path.join(self._root, str(pid), "stat"), "rb") as f:
                data = f.read(4096)     # comm is raw bytes, possibly not UTF-8
            return int(data.rpartition(b")")[2].split()[1])
        except (OSError, ValueError, IndexError):
            return None

    def _descends_from(self, pid: int, daemon: int) -> bool:
        cur = pid
        for _ in range(_MAX_PPID_HOPS):
            ppid = self._ppid(cur)
            if ppid is None or ppid <= 0:
                return False
            if ppid == daemon:
                return True
            cur = ppid
        return False

    def _skipped(self, what: str) -> None:
        """A cap was reached: each cap is logged once per collector (forged titles could repeat it forever)."""
        if what not in self._skip_logged:
            self._skip_logged.add(what)
            logger.warning("pm2: more than %s; the rest are skipped", what)

    def _daemons(self) -> list[tuple[str, str]]:
        """(pid, PM2_HOME) of the daemons outside containers, by ascending pid, at most 32."""
        found: list[tuple[str, str]] = []
        for entry in sorted((e for e in os.listdir(self._root) if e.isascii() and e.isdigit()), key=int):
            cmdline = self._read_cmdline(entry)
            if not cmdline or not cmdline.startswith("PM2 v"):
                continue
            home = parse_daemon_home(cmdline)
            if home is None or self._in_container(int(entry)):
                continue
            if len(found) == _MAX_DAEMONS:
                self._skipped(f"{_MAX_DAEMONS} PM2 daemons")
                break
            found.append((entry, home))
        return found

    def _daemon_apps(self, entry: str, home: str) -> dict[str, set[int]]:
        """App name -> live pids under one daemon, from its first 256 pid files by file name."""
        pids_dir = os.path.join(self._root, entry, "root") + home + "/pids"
        try:
            names = sorted(n for n in os.listdir(pids_dir) if n.endswith(".pid"))
        except OSError:
            return {}
        if len(names) > _MAX_PID_FILES:
            self._skipped(f"{_MAX_PID_FILES} pid files in a PM2 home")
        daemon = int(entry)
        apps: dict[str, set[int]] = {}
        for fname in names[:_MAX_PID_FILES]:
            parsed = parse_pid_filename(fname)
            if parsed is None:
                continue
            pid = read_pid_file(os.path.join(pids_dir, fname))
            if pid is None:
                continue
            if not os.path.exists(os.path.join(self._root, str(pid), "stat")):
                continue
            if not self._descends_from(pid, daemon):
                continue
            apps.setdefault(parsed[0], set()).add(pid)
        return apps

    def _discover(self) -> _Apps:
        """Apps by daemon pid (ascending), then by name, while they fit: 256 apps and 64 KiB as sent."""
        apps: _Apps = {}
        size = 2                                    # the list's brackets
        for entry, home in self._daemons():
            found = self._daemon_apps(entry, home)
            for name in sorted(found):
                if len(apps) == _MAX_APPS:
                    self._skipped(f"{_MAX_APPS} PM2 apps")
                    return apps
                size += _sent_size(name, home, found[name]) + (2 if apps else 0)   # ", " between
                if size > _MAX_APPS_JSON:
                    self._skipped(f"{_MAX_APPS_JSON} characters of PM2 apps")
                    return apps
                apps.setdefault((home, name), set()).update(found[name])
        return apps

    # --- per cycle -------------------------------------------------------
    def _refresh(self) -> None:
        """Start a discovery when one is due and none is in flight; use one that has finished."""
        now = self._clock()
        if self._pending is None and (self._last is None or now - self._last >= self._rediscover_s):
            self._last = now
            self._pending = self._submit(self._discover)
        if self._pending is not None and self._pending.done():
            finished, self._pending = self._pending, None
            try:
                self._apps = finished.result()
            except Exception:
                # The last good result stays in use; the next discovery is due as usual.
                if not self._failure_logged:
                    self._failure_logged = True
                    logger.exception("pm2 discovery failed")

    def collect(self) -> list[dict]:
        if not os.path.isdir(self._root):
            logger.debug("pm2: %s not available", self._root)
            return []
        self._refresh()
        out = []
        for (home, name), pids in self._apps.items():
            cpu, mem, alive = 0.0, 0, []
            for pid in sorted(pids):
                s = self._stats(pid)
                if s is None:
                    continue
                alive.append(pid)
                cpu += s[0]
                mem += s[1]
            pids.intersection_update(alive)
            if alive:
                out.append(_app_entry(name, home, alive, cpu, mem))
        out.sort(key=lambda a: (a["home"], a["name"]))
        return out


_collector: Pm2Collector | None = None
_failure_logged = False
_pool: ThreadPoolExecutor | None = None


def _submit_discovery(discover: Callable[[], _Apps]) -> Future[_Apps]:
    """Runs a discovery on the one PM2 discovery thread, started on first use."""
    global _pool
    if _pool is None:
        _pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="pm2-discovery")
    return _pool.submit(discover)


def reset_collector(proc_root: str | None = None) -> None:
    """(Re)initialise the process-wide collector. Called at startup / in tests.

    Also stops the discovery thread: a discovery still running ends on its own and its
    result goes with the old collector; the new collector starts a thread of its own.
    """
    global _collector, _failure_logged, _pool
    if _pool is not None:
        _pool.shutdown(wait=False)
        _pool = None
    _collector = Pm2Collector(proc_root or os.environ.get("HOST_PROC", "/proc"))
    _failure_logged = False


def collect() -> list[dict]:
    """PM2 apps for this cycle; never raises, logs the first failure once."""
    global _failure_logged
    try:
        if _collector is None:
            reset_collector()
        return _collector.collect()
    except Exception:
        if not _failure_logged:
            _failure_logged = True
            logger.exception("pm2 collect failed")
        return []
