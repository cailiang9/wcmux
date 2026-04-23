from __future__ import annotations

import asyncio
import collections
import os
import re
import secrets
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Optional

from .cwd import read_cwd, shorten as shorten_cwd
from .terminal import Terminal, spawn as spawn_terminal

# OSC sequence: ESC ] N ; payload ST (ST = BEL or ESC \)
_OSC_RE = re.compile(r"\x1b\](\d+);([^\x07\x1b]*?)(?:\x07|\x1b\\)")
_OSC_BUF_MAX = 4096
_MAX_CWD_LEN = 4096


def default_cwd() -> str:
    """Fallback workspace id when the URL has no ?cwd= parameter."""
    h = os.environ.get("HOME") or os.path.expanduser("~") or "/"
    return h.rstrip("/") or "/"


def normalize_cwd(raw: Optional[str]) -> str:
    """Canonical workspace id from a URL ?cwd= value (spec §4.10).
    Absolute + abspath; trailing '/' stripped (except for '/'); no symlink resolve."""
    if not raw:
        return default_cwd()
    s = os.path.abspath(raw)
    if s != "/":
        s = s.rstrip("/") or "/"
    return s[:_MAX_CWD_LEN]


def cwd_is_valid(normalized: str) -> bool:
    return os.path.isdir(normalized) and os.access(normalized, os.R_OK)


def _decode_osc7(payload: str) -> Optional[str]:
    """Parse `file://HOST/encoded-path` from an OSC 7 payload."""
    if not payload.startswith("file://"):
        return None
    rest = payload[len("file://"):]
    slash = rest.find("/")
    if slash < 0:
        return None
    try:
        return urllib.parse.unquote(rest[slash:])
    except Exception:
        return None

MAX_TABS = 20
BUFFER_BYTES = 256 * 1024
CWD_POLL_SECONDS = 2.0


@dataclass
class TabState:
    tab_id: str
    name: str
    terminal: Terminal
    workspace_id: str
    buffer: collections.deque = field(default_factory=collections.deque)
    buffer_size: int = 0
    cwd_full: str = ""
    cwd_display: str = ""
    created_at: float = field(default_factory=time.time)
    pump_task: Optional[asyncio.Task] = None
    subscribers: list[asyncio.Queue] = field(default_factory=list)
    # spec §4.11: resize merges across subscribers by taking element-wise min
    viewports: dict[int, tuple[int, int]] = field(default_factory=dict)
    # Shell-reported name via OSC 0/2 is ignored once the user has renamed manually.
    name_user_set: bool = False
    # Rolling buffer for OSC parsing across PTY read boundaries.
    osc_buf: str = ""

    def append_output(self, chunk: str) -> None:
        self.buffer.append(chunk)
        self.buffer_size += len(chunk.encode("utf-8", errors="ignore"))
        while self.buffer_size > BUFFER_BYTES and len(self.buffer) > 1:
            old = self.buffer.popleft()
            self.buffer_size -= len(old.encode("utf-8", errors="ignore"))

    def replay_text(self) -> str:
        return "".join(self.buffer)


@dataclass
class Workspace:
    """A named group of shared terminals (spec §4.10 / §4.11)."""
    workspace_id: str
    tabs: "collections.OrderedDict[str, TabState]" = field(
        default_factory=collections.OrderedDict
    )
    seq: int = 0

    def next_default_name(self) -> str:
        self.seq += 1
        return f"Shell {self.seq}"


class SessionRegistry:
    def __init__(self, shell: str) -> None:
        self._workspaces: dict[str, Workspace] = {}
        self._shell = shell
        self._cwd_poll_task: Optional[asyncio.Task] = None
        # Fast path: tab_id → (workspace_id) for global tab lookup
        self._tab_to_ws: dict[str, str] = {}

    def start_background_tasks(self) -> None:
        if self._cwd_poll_task is None or self._cwd_poll_task.done():
            self._cwd_poll_task = asyncio.create_task(self._cwd_poller())

    async def _cwd_poller(self) -> None:
        try:
            while True:
                await asyncio.sleep(CWD_POLL_SECONDS)
                for ws in list(self._workspaces.values()):
                    for tab in list(ws.tabs.values()):
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

    # ---- workspace access ----
    def get_workspace(self, ws_id: str) -> Optional[Workspace]:
        return self._workspaces.get(ws_id)

    def get_or_create_workspace(self, ws_id: str) -> Workspace:
        ws = self._workspaces.get(ws_id)
        if ws is None:
            ws = Workspace(workspace_id=ws_id)
            self._workspaces[ws_id] = ws
        return ws

    # ---- tab lifecycle ----
    def create_tab(self, workspace_id: str, *, rows: int = 24, cols: int = 80) -> TabState:
        """Workspace id is the canonical cwd; new tabs start in that directory."""
        ws = self.get_or_create_workspace(workspace_id)
        if len(ws.tabs) >= MAX_TABS:
            raise ValueError(f"tab limit reached ({MAX_TABS})")
        tab_id = secrets.token_urlsafe(9)
        name = ws.next_default_name()
        term = spawn_terminal(self._shell, rows=rows, cols=cols, cwd=workspace_id)
        tab = TabState(tab_id=tab_id, name=name, terminal=term, workspace_id=workspace_id)
        tab.pump_task = asyncio.create_task(self._pump_output(tab))
        ws.tabs[tab_id] = tab
        self._tab_to_ws[tab_id] = workspace_id
        self._broadcast_tabs_changed(ws)
        return tab

    async def _pump_output(self, tab: TabState) -> None:
        try:
            while True:
                chunk = await tab.terminal.read()
                tab.append_output(chunk)
                for q in list(tab.subscribers):
                    try:
                        q.put_nowait(("output", chunk))
                    except asyncio.QueueFull:
                        pass
                self._consume_osc(tab, chunk)
        except EOFError:
            for q in list(tab.subscribers):
                try:
                    q.put_nowait(("exit", 0))
                except asyncio.QueueFull:
                    pass
            self._remove_tab(tab, teardown_terminal=True)
        except asyncio.CancelledError:
            raise
        except Exception:
            pass

    def _remove_tab(self, tab: TabState, *, teardown_terminal: bool) -> None:
        ws = self._workspaces.get(tab.workspace_id)
        if ws is not None:
            ws.tabs.pop(tab.tab_id, None)
        self._tab_to_ws.pop(tab.tab_id, None)
        if teardown_terminal:
            try:
                tab.terminal.close()
            except Exception:
                pass
        if ws is not None:
            if not ws.tabs:
                # Empty workspace — drop the record so a later visit starts fresh
                self._workspaces.pop(ws.workspace_id, None)
            else:
                self._broadcast_tabs_changed(ws)

    def close_tab(self, tab_id: str) -> bool:
        tab = self.find_tab(tab_id)
        if not tab:
            return False
        # notify subscribers first so they send an "exit" before WS closes
        for q in list(tab.subscribers):
            try:
                q.put_nowait(("exit", 0))
            except Exception:
                pass
        if tab.pump_task:
            tab.pump_task.cancel()
        self._remove_tab(tab, teardown_terminal=True)
        return True

    def rename_tab(self, tab_id: str, name: str) -> bool:
        tab = self.find_tab(tab_id)
        if not tab:
            return False
        name = name.strip()[:80]
        if not name:
            return False
        tab.name = name
        tab.name_user_set = True
        ws = self._workspaces.get(tab.workspace_id)
        if ws:
            self._broadcast_tabs_changed(ws)
        return True

    def _consume_osc(self, tab: TabState, chunk: str) -> None:
        """Parse OSC 7 (cwd) / OSC 0/2 (title) from the shell's output stream.
        Matches may span chunk boundaries; we keep a bounded tail buffer."""
        buf = tab.osc_buf + chunk
        cwd_changed = False
        title_changed = False
        last_end = 0
        for m in _OSC_RE.finditer(buf):
            ps = int(m.group(1))
            payload = m.group(2)
            last_end = m.end()
            if ps == 7:
                cwd = _decode_osc7(payload)
                if cwd and cwd != tab.cwd_full:
                    tab.cwd_full = cwd
                    tab.cwd_display = shorten_cwd(cwd)
                    cwd_changed = True
            elif ps in (0, 2):
                title = payload.strip()[:80]
                if title and not tab.name_user_set and title != tab.name:
                    tab.name = title
                    title_changed = True

        # Keep only a possibly-incomplete trailing OSC for the next chunk.
        tail = buf[last_end:]
        idx = tail.rfind("\x1b]")
        tab.osc_buf = tail[idx:][:_OSC_BUF_MAX] if idx >= 0 else ""

        if cwd_changed:
            for q in list(tab.subscribers):
                try:
                    q.put_nowait(("cwd", {"full": tab.cwd_full,
                                          "display": tab.cwd_display}))
                except asyncio.QueueFull:
                    pass
        if title_changed:
            ws = self._workspaces.get(tab.workspace_id)
            if ws:
                self._broadcast_tabs_changed(ws)

    def list_tabs(self, workspace_id: str) -> list[dict]:
        ws = self._workspaces.get(workspace_id)
        if not ws:
            return []
        return [self._tab_summary(t) for t in ws.tabs.values()]

    def _tab_summary(self, t: TabState) -> dict:
        return {
            "tab_id": t.tab_id,
            "name": t.name,
            "cwd_full": t.cwd_full,
            "cwd_display": t.cwd_display,
            "alive": t.terminal.alive,
        }

    def find_tab(self, tab_id: str) -> Optional[TabState]:
        ws_id = self._tab_to_ws.get(tab_id)
        if not ws_id:
            return None
        ws = self._workspaces.get(ws_id)
        return ws.tabs.get(tab_id) if ws else None

    # ---- subscription (per-WS viewport tracking) ----
    def on_ws_connect(self, tab: TabState) -> None:
        # Nothing to do yet; viewport will be registered when the client sends resize.
        pass

    def on_ws_disconnect(self, tab: TabState, q: asyncio.Queue) -> None:
        tab.viewports.pop(id(q), None)
        self._apply_min_viewport(tab)
        # spec §4.11: closing the browser does NOT terminate the terminal.
        # Only explicit close_tab / shell EOF remove the tab from the registry.

    def update_viewport(self, tab: TabState, q: asyncio.Queue, rows: int, cols: int) -> None:
        if rows <= 0 or cols <= 0:
            return
        tab.viewports[id(q)] = (rows, cols)
        self._apply_min_viewport(tab)

    def _apply_min_viewport(self, tab: TabState) -> None:
        if not tab.viewports:
            return
        rows = min(v[0] for v in tab.viewports.values())
        cols = min(v[1] for v in tab.viewports.values())
        try:
            tab.terminal.resize(rows, cols)
        except Exception:
            pass

    # ---- broadcast tab-list changes to all subscribers in workspace ----
    def _broadcast_tabs_changed(self, ws: Workspace) -> None:
        payload = [self._tab_summary(t) for t in ws.tabs.values()]
        for tab in ws.tabs.values():
            for q in list(tab.subscribers):
                try:
                    q.put_nowait(("tabs", payload))
                except asyncio.QueueFull:
                    pass

    # ---- Hard-reset a workspace (for tests / admin) ----
    def terminate_workspace(self, workspace_id: str) -> None:
        ws = self._workspaces.pop(workspace_id, None)
        if not ws:
            return
        for tab in list(ws.tabs.values()):
            self._tab_to_ws.pop(tab.tab_id, None)
            if tab.pump_task:
                tab.pump_task.cancel()
            try:
                tab.terminal.close()
            except Exception:
                pass
            for q in tab.subscribers:
                try:
                    q.put_nowait(("exit", 0))
                except Exception:
                    pass
        ws.tabs.clear()
