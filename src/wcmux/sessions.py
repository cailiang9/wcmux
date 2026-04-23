from __future__ import annotations

import asyncio
import collections
import secrets
import time
from dataclasses import dataclass, field
from typing import Optional

from .cwd import read_cwd, shorten as shorten_cwd
from .terminal import Terminal, spawn as spawn_terminal

MAX_TABS = 20
BUFFER_BYTES = 256 * 1024
RETENTION_SECONDS = 5 * 60
CWD_POLL_SECONDS = 2.0


@dataclass
class TabState:
    tab_id: str
    name: str
    terminal: Terminal
    buffer: collections.deque = field(default_factory=collections.deque)
    buffer_size: int = 0
    cwd_full: str = ""
    cwd_display: str = ""
    active_ws: int = 0
    pending_close_task: Optional[asyncio.Task] = None
    created_at: float = field(default_factory=time.time)
    pump_task: Optional[asyncio.Task] = None
    subscribers: list[asyncio.Queue] = field(default_factory=list)

    def append_output(self, chunk: str) -> None:
        self.buffer.append(chunk)
        self.buffer_size += len(chunk.encode("utf-8", errors="ignore"))
        while self.buffer_size > BUFFER_BYTES and len(self.buffer) > 1:
            old = self.buffer.popleft()
            self.buffer_size -= len(old.encode("utf-8", errors="ignore"))

    def replay_text(self) -> str:
        return "".join(self.buffer)


@dataclass
class UserSession:
    sid: str
    tabs: "collections.OrderedDict[str, TabState]" = field(
        default_factory=collections.OrderedDict
    )
    seq: int = 0

    def next_default_name(self) -> str:
        self.seq += 1
        return f"Shell {self.seq}"


class SessionRegistry:
    def __init__(self, shell: str) -> None:
        self._sessions: dict[str, UserSession] = {}
        self._shell = shell
        self._cwd_poll_task: Optional[asyncio.Task] = None

    def start_background_tasks(self) -> None:
        if self._cwd_poll_task is None or self._cwd_poll_task.done():
            self._cwd_poll_task = asyncio.create_task(self._cwd_poller())

    async def _cwd_poller(self) -> None:
        try:
            while True:
                await asyncio.sleep(CWD_POLL_SECONDS)
                for us in list(self._sessions.values()):
                    for tab in list(us.tabs.values()):
                        try:
                            pid = tab.terminal.pid
                        except Exception:
                            pid = 0
                        new_full = read_cwd(pid) if pid else None
                        new_full = new_full or ""
                        if new_full != tab.cwd_full:
                            tab.cwd_full = new_full
                            tab.cwd_display = shorten_cwd(new_full) if new_full else ""
                            for q in list(tab.subscribers):
                                try:
                                    q.put_nowait(("cwd", {
                                        "full": tab.cwd_full,
                                        "display": tab.cwd_display,
                                    }))
                                except asyncio.QueueFull:
                                    pass
        except asyncio.CancelledError:
            return
        except Exception:
            return

    # ---- user session lookup ----
    def get_or_create(self, sid: str) -> UserSession:
        us = self._sessions.get(sid)
        if us is None:
            us = UserSession(sid=sid)
            self._sessions[sid] = us
        return us

    def get(self, sid: str) -> Optional[UserSession]:
        return self._sessions.get(sid)

    # ---- tab lifecycle ----
    def create_tab(self, sid: str, rows: int = 24, cols: int = 80) -> TabState:
        us = self.get_or_create(sid)
        if len(us.tabs) >= MAX_TABS:
            raise ValueError(f"tab limit reached ({MAX_TABS})")
        tab_id = secrets.token_urlsafe(9)
        name = us.next_default_name()
        term = spawn_terminal(self._shell, rows=rows, cols=cols)
        tab = TabState(tab_id=tab_id, name=name, terminal=term)
        tab.pump_task = asyncio.create_task(self._pump_output(sid, tab))
        us.tabs[tab_id] = tab
        return tab

    async def _pump_output(self, sid: str, tab: TabState) -> None:
        try:
            while True:
                chunk = await tab.terminal.read()
                tab.append_output(chunk)
                for q in list(tab.subscribers):
                    try:
                        q.put_nowait(("output", chunk))
                    except asyncio.QueueFull:
                        pass
        except EOFError:
            for q in list(tab.subscribers):
                try:
                    q.put_nowait(("exit", 0))
                except asyncio.QueueFull:
                    pass
            # Shell exited (Ctrl-D / exit / kill) — drop the tab from the session
            # so future reconnects don't revive a dead shell.
            us = self._sessions.get(sid)
            if us:
                us.tabs.pop(tab.tab_id, None)
            try:
                tab.terminal.close()
            except Exception:
                pass
        except asyncio.CancelledError:
            raise
        except Exception:
            pass

    def close_tab(self, sid: str, tab_id: str) -> bool:
        us = self._sessions.get(sid)
        if not us:
            return False
        tab = us.tabs.pop(tab_id, None)
        if not tab:
            return False
        self._teardown_tab(tab)
        return True

    def _teardown_tab(self, tab: TabState) -> None:
        try:
            tab.terminal.close()
        except Exception:
            pass
        if tab.pump_task:
            tab.pump_task.cancel()
        if tab.pending_close_task:
            tab.pending_close_task.cancel()
        # notify subscribers
        for q in tab.subscribers:
            try:
                q.put_nowait(("exit", 0))
            except Exception:
                pass
        tab.subscribers.clear()

    def rename_tab(self, sid: str, tab_id: str, name: str) -> bool:
        us = self._sessions.get(sid)
        if not us:
            return False
        tab = us.tabs.get(tab_id)
        if not tab:
            return False
        name = name.strip()[:80]
        if not name:
            return False
        tab.name = name
        return True

    def list_tabs(self, sid: str) -> list[dict]:
        us = self._sessions.get(sid)
        if not us:
            return []
        return [
            {
                "tab_id": t.tab_id,
                "name": t.name,
                "cwd_full": t.cwd_full,
                "cwd_display": t.cwd_display,
                "alive": t.terminal.alive,
            }
            for t in us.tabs.values()
        ]

    def get_tab(self, sid: str, tab_id: str) -> Optional[TabState]:
        us = self._sessions.get(sid)
        if not us:
            return None
        return us.tabs.get(tab_id)

    # ---- retention ----
    def on_ws_connect(self, tab: TabState) -> None:
        tab.active_ws += 1
        if tab.pending_close_task:
            tab.pending_close_task.cancel()
            tab.pending_close_task = None

    def on_ws_disconnect(self, sid: str, tab: TabState) -> None:
        tab.active_ws -= 1
        if tab.active_ws <= 0 and tab.terminal.alive:
            tab.pending_close_task = asyncio.create_task(
                self._delayed_cleanup(sid, tab)
            )

    async def _delayed_cleanup(self, sid: str, tab: TabState) -> None:
        try:
            await asyncio.sleep(RETENTION_SECONDS)
        except asyncio.CancelledError:
            return
        us = self._sessions.get(sid)
        if not us:
            return
        # only close if still no active ws
        if tab.active_ws <= 0 and tab.tab_id in us.tabs:
            us.tabs.pop(tab.tab_id, None)
            self._teardown_tab(tab)

    # ---- logout ----
    def terminate_session(self, sid: str) -> None:
        us = self._sessions.pop(sid, None)
        if not us:
            return
        for tab in list(us.tabs.values()):
            self._teardown_tab(tab)
        us.tabs.clear()
