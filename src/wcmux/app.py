from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket, status
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from .auth import LockoutRegistry, build_auth_router, require_auth
from .config import Config
from .devices import DeviceRegistry
from .preview import router as preview_router
from .share_routes import router as share_router
from .shares import ShareRegistry
from .sessions import (
    SessionRegistry,
    cwd_is_valid,
    normalize_cwd,
)

PKG_DIR = Path(__file__).parent
TEMPLATES_DIR = PKG_DIR / "templates"
STATIC_DIR = PKG_DIR / "static"


def _workspace_of(request: Request, *, require_valid: bool = True) -> str:
    """Resolve workspace id from the URL's ?cwd= (spec §4.10).
    On an explicitly-provided but invalid cwd we raise 400.
    Missing ?cwd= falls back to the server's HOME — always considered valid
    for look-up, so unauth'd clients listing an empty workspace still works."""
    raw = request.query_params.get("cwd")
    ws_id = normalize_cwd(raw)
    if require_valid and raw and not cwd_is_valid(ws_id):
        raise HTTPException(status_code=400, detail="invalid cwd")
    return ws_id


def create_app(config: Config) -> FastAPI:
    app = FastAPI(root_path=config.base_url, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.config = config
    app.state.lockout = LockoutRegistry()
    app.state.registry = SessionRegistry(shell=config.shell)
    app.state.devices = DeviceRegistry()
    # spec §4.22: where the file-preview sub-router roots its filesystem view.
    # `WCMUX_PREVIEW_ROOT` is the primary root (relative paths anchor here).
    # `WCMUX_PREVIEW_EXTRA_ROOTS` is a colon-separated list of additional
    # absolute trees the user wants reachable (mounted USB drives, NAS, etc.);
    # absolute path= queries are accepted iff they fall under one of these.
    _primary_env = os.environ.get("WCMUX_PREVIEW_ROOT", "")
    _primary = (Path(_primary_env).expanduser().resolve()
                if _primary_env else Path.home().resolve())
    app.state.preview_root = _primary
    _extras_env = os.environ.get("WCMUX_PREVIEW_EXTRA_ROOTS", "")
    _extra_paths: list[Path] = []
    seen = {_primary}
    for raw in _extras_env.split(":"):
        raw = raw.strip()
        if not raw:
            continue
        try:
            p = Path(raw).expanduser().resolve()
        except (OSError, RuntimeError):
            continue
        if p in seen:
            continue
        seen.add(p)
        _extra_paths.append(p)
    app.state.preview_roots = [_primary, *_extra_paths]
    # spec §4.23: share registry. Persisted alongside devices.json.
    app.state.shares = ShareRegistry()

    app.add_middleware(
        SessionMiddleware,
        secret_key=config.secret_key,
        session_cookie="wcmux_session",
        max_age=30 * 86400,
        same_site="lax",
        https_only=False,
    )

    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    app.state.templates = templates

    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    # Spec §4.11 revision: logout only clears the browser session cookie.
    # It no longer terminates workspace terminals.
    app.state.on_logout = None

    app.include_router(build_auth_router(templates))
    app.include_router(preview_router)
    app.include_router(share_router)

    @app.on_event("startup")
    async def _startup() -> None:
        app.state.registry.start_background_tasks()

    @app.get("/healthz", response_class=PlainTextResponse)
    async def healthz() -> str:
        return "ok"

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request, _=Depends(require_auth)) -> HTMLResponse:
        # spec §4.25: cache-bust handled inside the template via a random
        # `?v=` query on app.js / style.css URLs (Jinja `range|random`); no
        # state needed in the route, no Python restart needed when static
        # files change.
        return templates.TemplateResponse(
            request, "terminal.html", {"base_url": config.base_url}
        )

    # ---------- REST: tabs (scoped to workspace) ----------

    @app.get("/api/tabs")
    async def list_tabs(request: Request, _=Depends(require_auth)) -> JSONResponse:
        # Listing never creates a workspace — accept even unreadable paths
        # (returns empty list) so clients can GET without side effects.
        ws_id = _workspace_of(request, require_valid=False)
        return JSONResponse({
            "workspace": ws_id,
            "tabs": app.state.registry.list_tabs(ws_id),
        })

    @app.post("/api/tabs")
    async def create_tab(request: Request, _=Depends(require_auth)) -> JSONResponse:
        ws_id = _workspace_of(request, require_valid=True)
        try:
            tab = app.state.registry.create_tab(ws_id)
        except ValueError as e:
            raise HTTPException(status_code=409, detail=str(e))
        return JSONResponse({
            "workspace": ws_id,
            "tab_id": tab.tab_id,
            "name": tab.name,
            "cwd_full": tab.cwd_full,
            "cwd_display": tab.cwd_display,
            "alive": tab.terminal.alive,
        })

    @app.delete("/api/tabs/{tab_id}")
    async def delete_tab(tab_id: str, request: Request, _=Depends(require_auth)) -> JSONResponse:
        ok = app.state.registry.close_tab(tab_id)
        if not ok:
            raise HTTPException(status_code=404, detail="tab not found")
        return JSONResponse({"ok": True})

    @app.patch("/api/tabs/{tab_id}")
    async def patch_tab(tab_id: str, request: Request, _=Depends(require_auth)) -> JSONResponse:
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(status_code=400, detail="invalid json")
        name = (body or {}).get("name", "")
        ok = app.state.registry.rename_tab(tab_id, name)
        if not ok:
            raise HTTPException(status_code=400, detail="cannot rename")
        return JSONResponse({"ok": True})

    # ---------- WebSocket per tab ----------

    @app.websocket("/ws/{tab_id}")
    async def ws_tab(ws: WebSocket, tab_id: str) -> None:
        if not ws.session.get("authed"):
            # accept() before close() so the close-frame (with our 4401 code)
            # actually reaches the browser. Without accept(), Starlette returns
            # an HTTP 403 handshake-failure and the browser only sees code=1006.
            await ws.accept()
            await ws.close(code=4401)
            return
        # Touch session occasionally so SessionMiddleware refreshes the cookie's
        # Expires on the WS handshake response — keeps long-lived terminal pages
        # from quietly drifting past max_age while the user just types.
        _now = int(time.time())
        if _now - int(ws.session.get("seen", 0)) >= 6 * 3600:
            ws.session["seen"] = _now
        registry: SessionRegistry = ws.app.state.registry
        tab = registry.find_tab(tab_id)
        if not tab:
            # see comment above on accept()+close() — same applies here so the
            # browser actually sees code=4404 instead of an opaque 1006.
            await ws.accept()
            await ws.close(code=4404)
            return

        await ws.accept()
        registry.on_ws_connect(tab)

        # 1) replay buffer on connect
        replay = tab.replay_text()
        if replay:
            await ws.send_text(json.dumps({"type": "replay", "data": replay}))

        # 2) subscribe to future output
        q: asyncio.Queue = asyncio.Queue(maxsize=1024)
        tab.subscribers.append(q)

        async def pump_to_ws() -> None:
            try:
                while True:
                    kind, payload = await q.get()
                    if kind == "output":
                        await ws.send_text(json.dumps({"type": "output", "data": payload}))
                    elif kind == "exit":
                        await ws.send_text(json.dumps({"type": "exit", "code": payload}))
                        return
                    elif kind == "cwd":
                        await ws.send_text(json.dumps({
                            "type": "cwd",
                            "display": payload.get("display", ""),
                            "full": payload.get("full", ""),
                        }))
                    elif kind == "tabs":
                        await ws.send_text(json.dumps({"type": "tabs", "tabs": payload}))
            except Exception:
                pass

        pump_task = asyncio.create_task(pump_to_ws())
        try:
            while True:
                raw = await ws.receive_text()
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                t = msg.get("type")
                if t == "input":
                    tab.terminal.write(msg.get("data", ""))
                elif t == "resize":
                    rows = int(msg.get("rows", 24))
                    cols = int(msg.get("cols", 80))
                    registry.update_viewport(tab, q, rows, cols)
                elif t == "ping":
                    # spec §4.18: app-level heartbeat; do not forward to PTY
                    await ws.send_text(json.dumps({"type": "pong", "ts": msg.get("ts")}))
        except Exception:
            pass
        finally:
            try:
                tab.subscribers.remove(q)
            except ValueError:
                pass
            pump_task.cancel()
            registry.on_ws_disconnect(tab, q)

    return app
