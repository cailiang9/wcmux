from __future__ import annotations

import sys
from typing import Protocol, runtime_checkable


@runtime_checkable
class Terminal(Protocol):
    pid: int

    def write(self, data: str) -> None: ...
    async def read(self) -> str: ...       # next chunk; raises EOFError on close
    def resize(self, rows: int, cols: int) -> None: ...
    def close(self) -> None: ...
    @property
    def alive(self) -> bool: ...


def spawn(shell: str, rows: int = 24, cols: int = 80) -> "Terminal":
    if sys.platform.startswith("win"):
        from .terminal_windows import WindowsTerminal  # noqa: F401
        return WindowsTerminal(shell, rows=rows, cols=cols)  # type: ignore[arg-type]
    from .terminal_unix import UnixTerminal
    return UnixTerminal(shell, rows=rows, cols=cols)
