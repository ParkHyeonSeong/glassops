"""Dashboard-only runtime archive. No database, configuration or secret imports.

The bundled root agent rotates existing writers; readers are strictly read-only.
Archive names record UTC rotation time. A later mtime wins when reopening was
delayed, so no record is expired while it can still be inside the retention window.
"""
import argparse
import asyncio
import grp
import logging
import os
import re
import signal
import stat
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path("/app/data/logs")
RETENTION_SECONDS = 7 * 24 * 60 * 60
ROTATION_SECONDS = 60 * 60
CHANNELS = ("bootstrap", "supervisor", "backend", "agent", "nginx-startup", "nginx-access", "nginx-error")
_ARCHIVE = re.compile(
    r"^(" + "|".join(CHANNELS) + r")\.(\d{8}T\d{12}Z)\.([a-f0-9]{12})\.log$"
)
_SUPERVISOR_CONFIG = "/etc/supervisor/conf.d/glassops.conf"
logger = logging.getLogger("glassops.runtime_logs")


def _owned_file(path: Path) -> None:
    if path.is_symlink():
        raise OSError(f"Runtime log must be a regular file: {path.name}")
    path.touch(exist_ok=True)
    if not path.is_file():
        raise OSError(f"Runtime log must be a regular file: {path.name}")
    path.chmod(0o640)
    if os.geteuid() == 0:
        os.chown(path, 0, grp.getgrnam("appuser").gr_gid)


def _archive_time(path: Path) -> float | None:
    match = _ARCHIVE.fullmatch(path.name)
    if not match:
        return None
    try:
        return datetime.strptime(match[2], "%Y%m%dT%H%M%S%fZ").replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        return None


def _archives(root: Path):
    for path in root.iterdir():
        end = _archive_time(path)
        if end is None:
            continue
        try:
            info = path.lstat()
        except FileNotFoundError:  # Another read may overlap normal rotation/expiry.
            continue
        if stat.S_ISREG(info.st_mode):
            yield path, max(end, info.st_mtime), (info.st_dev, info.st_ino)


def _close_active(root: Path, channel: str, now: float | None) -> None:
    path = root / f"{channel}.log"
    if path.is_symlink():
        raise OSError(f"Runtime log must be a regular file: {path.name}")
    if path.exists() and path.stat().st_size:
        # Startup closes the previous instance using its actual last-write time,
        # not today's time. Runtime rotation allows for buffered late writes.
        end = path.stat().st_mtime if now is None else now
        stamp = datetime.fromtimestamp(end, timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        path.rename(root / f"{channel}.{stamp}.{uuid.uuid4().hex[:12]}.log")
    _owned_file(path)


def _prune(root: Path, now: float, held: set[tuple[int, int]]) -> int:
    removed = 0
    for path, end, inode in _archives(root):
        if end <= now - RETENTION_SECONDS and inode not in held:
            path.unlink(missing_ok=True)
            removed += 1
    return removed


def prepare_runtime_logs(root: Path = ROOT, now: float | None = None) -> None:
    """Container startup only, before Supervisor/writers exist (single instance)."""
    now = time.time() if now is None else now
    if root.is_symlink():
        raise OSError("Runtime archive directory must not be a symlink")
    # nginx workers must traverse this directory to reopen their own logs.
    # Other users cannot list it or read the 0640 log files.
    root.mkdir(parents=True, exist_ok=True, mode=0o751)
    root.chmod(0o751)
    if os.geteuid() == 0:
        os.chown(root, 0, grp.getgrnam("appuser").gr_gid)
    for channel in CHANNELS:
        _close_active(root, channel, None)
    _prune(root, now, set())


def _control(*arguments: str) -> str:
    return subprocess.run(
        ["supervisorctl", "-c", _SUPERVISOR_CONFIG, *arguments],
        check=True, capture_output=True, text=True, timeout=5,
    ).stdout.strip()


def _supervisor_pid() -> int:
    pid = int(_control("pid"))
    if pid <= 0 or b"supervisord" not in Path(f"/proc/{pid}/cmdline").read_bytes():
        raise OSError("Cannot identify this container's Supervisor")
    return pid


def _reopen_writers() -> None:
    # Never signal PID 1: this image normally shares the host PID namespace.
    os.kill(_supervisor_pid(), signal.SIGUSR2)
    nginx_pid = int(_control("pid", "nginx"))
    if nginx_pid <= 0:
        raise OSError("nginx is stopped or starting; retry log reopening later")
    command = Path(f"/proc/{nginx_pid}/cmdline").read_bytes().split(b"\0", 1)[0].rsplit(b"/", 1)[-1]
    if command != b"nginx" and not command.startswith(b"nginx: master process"):
        raise OSError("nginx is still starting; retry log reopening later")
    os.kill(nginx_pid, signal.SIGUSR1)


def _open_log_inodes() -> set[tuple[int, int]]:
    """Inspect only this Supervisor and its nginx master/workers, never the host."""
    pids = {_supervisor_pid()}
    nginx_pid = int(_control("pid", "nginx"))
    if nginx_pid > 0:
        pids.add(nginx_pid)
        # nginx owns its worker descriptors. The file may disappear on a normal
        # service exit; a permission failure is not proof a descriptor is closed.
        try:
            children = Path(f"/proc/{nginx_pid}/task/{nginx_pid}/children").read_text()
            pids.update(int(pid) for pid in children.split())
        except FileNotFoundError:
            pass
    held = set()
    for pid in pids:
        try:
            descriptors = list(Path(f"/proc/{pid}/fd").iterdir())
        except FileNotFoundError:
            continue
        for descriptor in descriptors:
            try:
                info = descriptor.stat()
                held.add((info.st_dev, info.st_ino))
            except FileNotFoundError:
                pass
    return held


def rotate_runtime_logs(root: Path = ROOT, now: float | None = None) -> None:
    now = time.time() if now is None else now
    for channel in CHANNELS:
        _close_active(root, channel, now)
    # A failed signal leaves the renamed file intact; the next cycle retries.
    _reopen_writers()


def prune_runtime_logs(root: Path = ROOT, now: float | None = None) -> int:
    return _prune(root, time.time() if now is None else now, _open_log_inodes())


def _maintain_once() -> None:
    now = time.time()
    rotate_runtime_logs(ROOT, now)
    prune_runtime_logs(ROOT, now)


async def maintain_runtime_logs(stop: asyncio.Event) -> None:
    """Independent of metrics/WebSocket availability; failures never end collection."""
    while not stop.is_set():
        work = asyncio.create_task(asyncio.to_thread(_maintain_once))
        try:
            await asyncio.shield(work)
        except asyncio.CancelledError:
            # to_thread cannot be killed; join the bounded file/control operation.
            try:
                await work
            except Exception:
                logger.exception("Runtime log maintenance failed during shutdown")
            raise
        except Exception:
            logger.exception("Runtime log maintenance failed; retaining existing files")
        if not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), ROTATION_SECONDS - time.time() % ROTATION_SECONDS)
            except asyncio.TimeoutError:
                pass


def _retained_files(root: Path) -> list[tuple[Path, float]]:
    if not root.is_dir():
        raise FileNotFoundError("Runtime archive has not been initialized")
    files = [(path, end) for path, end, _ in _archives(root)
             if end > time.time() - RETENTION_SECONDS]
    for channel in CHANNELS:
        path = root / f"{channel}.log"
        try:
            info = path.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISREG(info.st_mode):
            files.append((path, info.st_mtime))
    if not files:
        raise FileNotFoundError("Runtime archive has not been initialized")
    return sorted(files, key=lambda item: (item[1], item[0].name))


def retained_log_file(root: Path, name: str) -> Path:
    if Path(name).name != name or name in ("", ".", ".."):
        raise ValueError("Use a managed log filename from list")
    for path, _ in _retained_files(root):
        if path.name == name:
            return path
    raise FileNotFoundError("Retained log file not found")


def _reverse_lines(path: Path):
    """Read a tail from the end; never scan a seven-day archive for a UI refresh."""
    with path.open("rb") as stream:
        position = stream.seek(0, os.SEEK_END)
        pending = b""
        at_end = True
        while position:
            size = min(position, 8192)
            position -= size
            stream.seek(position)
            lines = (stream.read(size) + pending).split(b"\n")
            pending = lines.pop(0)
            for line in reversed(lines):
                if not (at_end and line == b""):
                    yield line.decode("utf-8", errors="replace").rstrip("\r")
                at_end = False
        if pending:
            yield pending.decode("utf-8", errors="replace").rstrip("\r")


def read_runtime_logs(root: Path, *, tail: int = 500, search: str = "") -> dict:
    tail = max(1, min(tail, 5000))
    # Match the existing system-log reader: search within a bounded recent tail.
    count = tail * 2 if search else tail
    latest = []
    for path, _ in reversed(_retained_files(root)):
        for line in _reverse_lines(path):
            latest.append(f"[{path.name}] {line}")
            if len(latest) >= count:
                break
        if len(latest) >= count:
            break
    lines = list(reversed(latest))
    if search:
        lines = [line for line in lines if search.casefold() in line.casefold()]
    lines = lines[-tail:]
    return {"lines": lines, "total": len(lines)}


def main() -> None:
    parser = argparse.ArgumentParser(description="Dashboard runtime logs in the existing data volume")
    parser.add_argument("command", choices=("prepare", "finish-bootstrap", "maintain", "read", "list", "follow"))
    parser.add_argument("--tail", type=int)
    parser.add_argument("--search", default="")
    parser.add_argument("--file", help="Managed filename returned by list; read its complete contents")
    args = parser.parse_args()
    try:
        if args.command == "prepare":
            prepare_runtime_logs()
        elif args.command == "finish-bootstrap":
            _close_active(ROOT, "bootstrap", None)
        elif args.command == "maintain":
            _maintain_once()
        elif args.command == "list":
            for path, _ in _retained_files(ROOT):
                print(path.name)
        elif args.command == "read":
            if args.file:
                with retained_log_file(ROOT, args.file).open(errors="replace") as stream:
                    for line in stream:
                        sys.stdout.write(line)
            else:
                for line in read_runtime_logs(ROOT, tail=args.tail or 500, search=args.search)["lines"]:
                    print(line)
        else:
            # GNU tail already follows renamed/recreated files without a daemon,
            # metadata index, or another custom streaming implementation.
            paths = [str(retained_log_file(ROOT, f"{channel}.log")) for channel in CHANNELS]
            os.execvp("tail", ["tail", "-n", str(max(1, min(args.tail or 200, 5000))), "-F", "--", *paths])
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        parser.exit(1, f"Runtime logs: {exc}\n")


if __name__ == "__main__":
    main()
