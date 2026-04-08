"""PTY terminal service — spawns shell and bridges to WebSocket."""

import asyncio
import fcntl
import logging
import os
import pty
import select
import signal
import struct
import termios

logger = logging.getLogger("glassops.terminal")

SESSION_TIMEOUT = 300  # 5 minutes idle


class TerminalSession:
    def __init__(self) -> None:
        self.master_fd: int | None = None
        self.pid: int | None = None
        self._last_activity = asyncio.get_event_loop().time()

    @property
    def is_alive(self) -> bool:
        if self.pid is None:
            return False
        try:
            os.waitpid(self.pid, os.WNOHANG)
            os.kill(self.pid, 0)
            return True
        except (ChildProcessError, ProcessLookupError, OSError):
            return False

    @property
    def idle_seconds(self) -> float:
        return asyncio.get_event_loop().time() - self._last_activity

    def spawn(self) -> None:
        """Spawn a shell in a PTY. Uses nsenter for host access if pid=host."""
        master_fd, slave_fd = pty.openpty()

        # Detect if we can access host via nsenter (pid=host mode)
        use_nsenter = os.path.exists("/host/proc/1/ns/pid")
        terminal_user = os.environ.get("GLASSOPS_TERMINAL_USER", "")

        if use_nsenter:
            if terminal_user:
                # nsenter to host, then su to user (will prompt for password)
                cmd = ["nsenter", "--target", "1", "--mount", "--uts", "--ipc", "--net", "--pid", "--",
                       "su", "-", terminal_user]
            else:
                # No user specified — drop to login prompt
                cmd = ["nsenter", "--target", "1", "--mount", "--uts", "--ipc", "--net", "--pid", "--",
                       "login"]
        else:
            cmd = [os.environ.get("SHELL", "/bin/bash"), "--login"]

        child_pid = os.fork()
        if child_pid == 0:
            # Child process
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
            # Parent
            os.close(slave_fd)
            self.master_fd = master_fd
            self.pid = child_pid
            logger.info("Terminal spawned: pid=%d, nsenter=%s", child_pid, use_nsenter)

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
        self._last_activity = asyncio.get_event_loop().time()
        os.write(self.master_fd, data)

    def resize(self, rows: int, cols: int) -> None:
        if self.master_fd is None:
            return
        winsize = struct.pack("HHHH", rows, cols, 0, 0)
        fcntl.ioctl(self.master_fd, termios.TIOCSWINSZ, winsize)

    def kill(self) -> None:
        if self.pid is not None:
            try:
                os.kill(self.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            self.pid = None
        if self.master_fd is not None:
            try:
                os.close(self.master_fd)
            except OSError:
                pass
            self.master_fd = None
