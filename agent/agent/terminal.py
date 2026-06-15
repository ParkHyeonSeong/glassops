"""PTY terminal session — used by the agent to spawn a shell on the host.

Mirrors backend/app/services/terminal_service.py with one extension:
`spawn(host_user)` accepts a per-call host username override so the dashboard
can pick the local account based on the GlassOps user's mapping.
"""

import asyncio
import fcntl
import logging
import os
import pty
import select
import signal
import struct
import termios

logger = logging.getLogger("glassops.agent.terminal")

# Background SIGKILL-escalation tasks (held so the loop doesn't GC them mid-run).
_reap_tasks: set = set()

# Live child pid -> owning TerminalSession. spawn() registers; whoever reaps the
# child removes it. This keeps the two reapers (a session's own is_alive and the
# process-wide reap_orphans backstop) from breaking the invariant that a live
# session's pid is never silently recycled: when ANY reaper reaps a child it
# clears the owner's `pid`, so kill()/_reap_and_kill never signal a pid that has
# been reaped (and possibly recycled to another process).
_owned_pids: dict[int, "TerminalSession"] = {}


def reap_orphans() -> None:
    """Reap every exited fork child and notify its owning session.

    Safe because spawn()'s os.fork() is the agent's ONLY child source (no
    subprocess/Popen/posix_spawn anywhere in the agent), so waitpid(-1) can only
    ever reap a shell we spawned. Non-blocking (WNOHANG). Called once per
    main-loop tick as the backstop for any child a session's own reaper missed
    (e.g. a session torn down before its reader observed the exit).

    Do NOT use signal(SIGCHLD, SIG_IGN) instead: that makes the explicit
    waitpid() calls elsewhere fail with ECHILD."""
    while True:
        try:
            pid, _ = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return  # no children left
        except OSError:
            return  # unexpected — leave the rest for the next tick
        if pid == 0:
            return  # children exist but none have exited
        session = _owned_pids.pop(pid, None)
        if session is not None:
            session.pid = None  # owner must not later signal this (recyclable) pid
        logger.debug("Reaped child pid=%d%s", pid, "" if session else " (orphan)")


async def _reap_and_kill(pid: int, session: "TerminalSession") -> None:
    """Escalate a SIGTERM'd child to SIGKILL if it won't exit on its own. Reaping
    is delegated to reap_orphans() / the session's is_alive, so we never block a
    thread-pool worker on a wedged (D-state) child.

    Ownership-checked against PID reuse: if the child is reaped during the grace
    period, `_owned_pids[pid]` is cleared (or, if the pid was recycled, reassigned
    to a different session), so we stop before SIGKILLing a pid we no longer own."""
    for _ in range(30):  # ~3s grace for a clean exit
        if _owned_pids.get(pid) is not session:
            return  # reaped (and maybe recycled) elsewhere — no longer ours
        await asyncio.sleep(0.1)
    if _owned_pids.get(pid) is not session:
        return
    try:
        os.kill(pid, signal.SIGKILL)
    except (ProcessLookupError, OSError):
        pass
    # The SIGKILLed child becomes a brief zombie until reap_orphans() sweeps it.


def _schedule_reap(pid: int, session: "TerminalSession") -> None:
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # No running loop — should never happen (kill() only runs inside the loop).
        # Best effort, NON-blocking: the reaper isn't running here, so just SIGKILL
        # and leave the eventual init-reparent to reap; never block on waitpid(pid,0)
        # (a D-state child would hang the caller forever).
        logger.warning("_schedule_reap with no running loop; best-effort SIGKILL pid=%d", pid)
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
        _owned_pids.pop(pid, None)
        return
    t = loop.create_task(_reap_and_kill(pid, session))
    _reap_tasks.add(t)
    t.add_done_callback(_reap_tasks.discard)


async def shutdown_reap(timeout: float = 5.0) -> None:
    """On agent shutdown, drive the reaper while in-flight SIGKILL escalations
    finish, then do a final sweep — the per-tick backstop stops once the main loop
    exits, so an escalated child would otherwise be abandoned mid-grace. Polling
    reap_orphans() here also lets a cleanly-exiting shell be reaped at once instead
    of waiting out the full grace (its escalation then short-circuits)."""
    for _ in range(max(1, int(timeout / 0.1))):
        reap_orphans()
        if not _reap_tasks:
            break
        await asyncio.sleep(0.1)
    reap_orphans()


class TerminalSession:
    def __init__(self) -> None:
        self.master_fd: int | None = None
        self.pid: int | None = None

    @property
    def is_alive(self) -> bool:
        pid = self.pid
        if pid is None:
            return False
        try:
            reaped, _ = os.waitpid(pid, os.WNOHANG)
            if reaped == pid:
                _owned_pids.pop(pid, None)
                self.pid = None   # exited and reaped — clear so kill() won't signal a
                return False       # (possibly reused) pid, and we don't double-reap
            os.kill(pid, 0)
            return True
        except (ChildProcessError, ProcessLookupError, OSError):
            _owned_pids.pop(pid, None)
            self.pid = None
            return False

    def spawn(self, host_user: str | None = None) -> None:
        """Spawn a shell in a PTY. Uses nsenter to reach the host (pid=host)."""
        use_nsenter = os.path.exists("/host/proc/1/ns/pid")
        terminal_user = host_user or os.environ.get("GLASSOPS_TERMINAL_USER", "")

        if use_nsenter:
            if terminal_user:
                cmd = ["nsenter", "--target", "1", "--mount", "--uts", "--ipc", "--net", "--pid", "--",
                       "su", "-", terminal_user]
            elif os.environ.get("GLASSOPS_ALLOW_LOGIN_PROMPT", "").lower() == "true":
                # Opt-in: a host `login` prompt lets the operator authenticate as ANY
                # host account (incl. root). Off by default so an unconfigured deploy
                # doesn't expose a host login prompt to every dashboard admin.
                cmd = ["nsenter", "--target", "1", "--mount", "--uts", "--ipc", "--net", "--pid", "--",
                       "login"]
            else:
                raise RuntimeError(
                    "No terminal user configured — set GLASSOPS_TERMINAL_USER, add a per-user "
                    "host-account mapping, or set GLASSOPS_ALLOW_LOGIN_PROMPT=true to allow a host login prompt.")
        else:
            cmd = [os.environ.get("SHELL", "/bin/bash"), "--login"]

        # Resolve cmd BEFORE allocating the PTY so a refusal above doesn't leak fds.
        master_fd, slave_fd = pty.openpty()
        self.master_fd = master_fd  # set now so a failed fork's fd is still closeable

        try:
            child_pid = os.fork()
        except OSError:
            os.close(slave_fd)
            os.close(master_fd)
            self.master_fd = None
            raise

        if child_pid == 0:
            # Child — must NEVER return into the parent's inherited Python stack.
            # Any failure here (exec of a missing nsenter/su/SHELL, ioctl/setsid
            # error) must _exit, not raise: otherwise the forked child unwinds back
            # up into the agent's event loop and runs a second agent on the
            # inherited backend socket. os._exit skips atexit/buffer flushing.
            try:
                os.close(master_fd)
                os.setsid()
                fcntl.ioctl(slave_fd, termios.TIOCSCTTY, 0)
                os.dup2(slave_fd, 0)
                os.dup2(slave_fd, 1)
                os.dup2(slave_fd, 2)
                os.close(slave_fd)
                os.environ["TERM"] = "xterm-256color"
                os.environ["SHELL"] = "/bin/bash"
                os.environ["HOME"] = os.environ.get("HOME", "/root")
                os.execvp(cmd[0], cmd)
            except BaseException:
                os._exit(127)
        else:
            os.close(slave_fd)
            self.pid = child_pid
            _owned_pids[child_pid] = self
            logger.info("Terminal spawned: pid=%d nsenter=%s user=%s", child_pid, use_nsenter, terminal_user or "(login)")

    async def read(self) -> bytes:
        if self.master_fd is None:
            return b""
        loop = asyncio.get_event_loop()
        try:
            return await loop.run_in_executor(None, self._blocking_read)
        except OSError:
            return b""

    def _blocking_read(self) -> bytes:
        if self.master_fd is None:
            return b""
        r, _, _ = select.select([self.master_fd], [], [], 0.1)
        if r:
            return os.read(self.master_fd, 4096)
        return b""

    def write(self, data: bytes) -> None:
        if self.master_fd is None:
            return
        os.write(self.master_fd, data)

    def resize(self, rows: int, cols: int) -> None:
        if self.master_fd is None:
            return
        winsize = struct.pack("HHHH", rows, cols, 0, 0)
        fcntl.ioctl(self.master_fd, termios.TIOCSWINSZ, winsize)

    def kill(self) -> None:
        pid = self.pid
        self.pid = None
        if pid is not None:
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pid = None  # already gone (or unsignalable) — nothing to escalate
        if self.master_fd is not None:
            try:
                os.close(self.master_fd)
            except OSError:
                pass
            self.master_fd = None
        if pid is not None:
            # SIGTERM may be ignored (a trapping shell / wedged su) — escalate to
            # SIGKILL after a grace period so a host shell can't linger. Ownership
            # stays in _owned_pids until the child is reaped (see _reap_and_kill).
            _schedule_reap(pid, self)
