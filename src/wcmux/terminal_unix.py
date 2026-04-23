from __future__ import annotations

import asyncio
import errno
import fcntl
import os
import signal
import struct
import termios
from typing import Optional

import ptyprocess

READ_CHUNK = 8192


class UnixTerminal:
    """PTY-backed shell wrapper using ptyprocess + asyncio add_reader."""

    def __init__(self, shell: str, rows: int = 24, cols: int = 80,
                 cwd: Optional[str] = None) -> None:
        spawn_cwd: Optional[str] = None
        if cwd and os.path.isabs(cwd) and os.path.isdir(cwd):
            spawn_cwd = cwd
        self._proc = ptyprocess.PtyProcess.spawn(
            [shell],
            dimensions=(rows, cols),
            env={**os.environ, "TERM": "xterm-256color"},
            cwd=spawn_cwd,
        )
        self.pid: int = self._proc.pid
        self._fd: int = self._proc.fd
        self._queue: asyncio.Queue[bytes | None] = asyncio.Queue()
        self._closed = False
        self._loop = asyncio.get_event_loop()
        self._loop.add_reader(self._fd, self._on_readable)

    # --- Protocol impl --------------------------------------------------

    def write(self, data: str) -> None:
        if self._closed:
            return
        try:
            os.write(self._fd, data.encode("utf-8"))
        except OSError as e:
            if e.errno in (errno.EIO, errno.EBADF):
                self._mark_closed()
            else:
                raise

    async def read(self) -> str:
        chunk = await self._queue.get()
        if chunk is None:
            raise EOFError("pty closed")
        return chunk.decode("utf-8", errors="replace")

    def resize(self, rows: int, cols: int) -> None:
        if self._closed:
            return
        if rows <= 0 or cols <= 0:
            return
        size = struct.pack("HHHH", rows, cols, 0, 0)
        try:
            fcntl.ioctl(self._fd, termios.TIOCSWINSZ, size)
        except OSError:
            pass

    def close(self) -> None:
        if self._closed:
            return
        self._mark_closed()
        try:
            self._proc.kill(signal.SIGHUP)
        except Exception:
            pass
        try:
            self._proc.close(force=True)
        except Exception:
            pass

    @property
    def alive(self) -> bool:
        return (not self._closed) and self._proc.isalive()

    # --- Internals ------------------------------------------------------

    def _on_readable(self) -> None:
        try:
            data = os.read(self._fd, READ_CHUNK)
        except OSError as e:
            if e.errno in (errno.EIO, errno.EBADF):
                data = b""
            else:
                self._queue.put_nowait(None)
                self._mark_closed()
                return
        if not data:
            # EOF
            self._queue.put_nowait(None)
            self._mark_closed()
            return
        self._queue.put_nowait(data)

    def _mark_closed(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._loop.remove_reader(self._fd)
        except Exception:
            pass
