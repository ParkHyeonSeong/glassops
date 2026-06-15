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


def reap_orphans() -> None:
    """Process-wide backstop: reap any of OUR exited fork children that a
    per-session reaper missed. Safe because spawn()'s os.fork() is the agent's
    ONLY child source — there is no subprocess/Popen/posix_spawn anywhere in the
    agent, so waitpid(-1) can only ever reap a shell we spawned. Non-blocking
    (WNOHANG); call once per main-loop tick.

    Do NOT replace this with signal(SIGCHLD, SIG_IGN): that makes the explicit
    waitpid() calls below fail with ECHILD."""
    while True:
        try:
            pid, _ = os.waitpid(-1, os.WNOHANG)
        except (ChildProcessError, OSError):
            return  # no children, or a transient error — nothing to reap
        if pid == 0:
            return  # children exist but none have exited
        logger.debug("Reaped orphan child pid=%d", pid)


async def _reap_and_kill(pid: int) -> None:
    """Escalate a SIGTERM'd child to SIGKILL if it won't exit on its own. The final
    reap (waitpid) is delegated to the process-wide reap_orphans() backstop in the
    main loop, so we never block a thread-pool worker on a wedged (D-state) child.
    Safe against PID reuse: an unreaped child PID is never recycled."""
    for _ in range(30):  # ~3s grace for a clean exit
        await asyncio.sleep(0.1)
        try:
            if os.waitpid(pid, os.WNOHANG)[0] == pid:
                return  # exited and reaped
        except (ChildProcessError, OSError):
            return  # already gone
    try:
        os.kill(pid, signal.SIGKILL)
    except (ProcessLookupError, OSError):
        pass
    # The SIGKILLed child briefly becomes a zombie until reap_orphans() sweeps it
    # (<= one COLLECT_INTERVAL). No blocking waitpid here.


def _schedule_reap(pid: int) -> None:
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop is not None:
        t = loop.create_task(_reap_and_kill(pid))
        _reap_tasks.add(t)
        t.add_done_callback(_reap_tasks.discard)
    else:  # no event loop (rare) — best-effort synchronous escalation + reap
        # No event loop means reap_orphans() isn't running, so this path must reap
        # the child itself (it's off the loop, so a blocking waitpid is fine here).
        import time as _time
        for _ in range(30):
            _time.sleep(0.1)
            try:
                if os.waitpid(pid, os.WNOHANG)[0] == pid:
                    return
            except (ChildProcessError, OSError):
                return
        try:
            os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, 0)
        except (ProcessLookupError, ChildProcessError, OSError):
            pass


class TerminalSession:
    def __init__(self) -> None:
        self.master_fd: int | None = None
        self.pid: int | None = None

    @property
    def is_alive(self) -> bool:
        if self.pid is None:
            return False
        try:
            reaped, _ = os.waitpid(self.pid, os.WNOHANG)
            if reaped == self.pid:
                self.pid = None   # exited and reaped — clear so kill() won't signal a
                return False       # (possibly reused) pid, and we don't double-reap
            os.kill(self.pid, 0)
            return True
        except (ChildProcessError, ProcessLookupError, OSError):
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
            # Child
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
        else:
            os.close(slave_fd)
            self.pid = child_pid
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
            except ProcessLookupError:
                pid = None  # already gone
        if self.master_fd is not None:
            try:
                os.close(self.master_fd)
            except OSError:
                pass
            self.master_fd = None
        if pid is not None:
            # SIGTERM may be ignored (a trapping shell / wedged su) — escalate to
            # SIGKILL after a grace period so a host shell can't linger.
            _schedule_reap(pid)
