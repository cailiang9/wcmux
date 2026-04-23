from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket, status
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from .auth import LockoutRegistry, build_auth_router, require_auth
from .config import Config
from .sessions import SessionRegistry

PKG_DIR = Path(__file__).parent
TEMPLATES_DIR = PKG_DIR / "templates"
STATIC_DIR = PKG_DIR / "static"


def create_app(config: Config) -> FastAPI:
    app = FastAPI(root_path=config.base_url, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.config = config
    app.state.lockout = LockoutRegistry()
    app.state.registry = SessionRegistry(shell=config.shell)

    app.add_middleware(
        SessionMiddleware,
        secret_key=config.secret_key,
        session_cookie="wcmux_session",
        max_age=7 * 86400,
        same_site="lax",
        https_only=False,
    )

    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    app.state.templates = templates

    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    # logout hook: terminate tabs in-process
    def _on_logout(sid: str) -> None:
        app.state.registry.terminate_session(sid)

    app.state.on_logout = _on_logout

    app.include_router(build_auth_router(templates))

    @app.on_event("startup")
    async def _startup() -> None:
        app.state.registry.start_background_tasks()

    @app.get("/healthz", response_class=PlainTextResponse)
    async def healthz() -> str:
        return "ok"

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request, _=Depends(require_auth)) -> HTMLResponse:
        return templates.TemplateResponse(
            request, "terminal.html", {"base_url": config.base_url}
        )

    # ---------- REST: tabs ----------

    def _sid(request: Request) -> str:
        sid = request.session.get("sid")
        if not sid:
            # should have been set on login, but tolerate
            import secrets
            sid = secrets.token_urlsafe(16)
            request.session["sid"] = sid
        return sid

    @app.get("/api/tabs")
    async def list_tabs(request: Request, _=Depends(require_auth)) -> JSONResponse:
        sid = _sid(request)
        return JSONResponse({"tabs": app.state.registry.list_tabs(sid)})

    @app.post("/api/tabs")
    async def create_tab(request: Request, _=Depends(require_auth)) -> JSONResponse:
        sid = _sid(request)
        try:
            tab = app.state.registry.create_tab(sid)
        except ValueError as e:
            raise HTTPException(status_code=409, detail=str(e))
        return JSONResponse({
            "tab_id": tab.tab_id,
            "name": tab.name,
            "cwd_full": tab.cwd_full,
            "cwd_display": tab.cwd_display,
            "alive": tab.terminal.alive,
        })

    @app.delete("/api/tabs/{tab_id}")
    async def delete_tab(tab_id: str, request: Request, _=Depends(require_auth)) -> JSONResponse:
        sid = _sid(request)
        ok = app.state.registry.close_tab(sid, tab_id)
        if not ok:
            raise HTTPException(status_code=404, detail="tab not found")
        return JSONResponse({"ok": True})

    @app.patch("/api/tabs/{tab_id}")
    async def patch_tab(tab_id: str, request: Request, _=Depends(require_auth)) -> JSONResponse:
        sid = _sid(request)
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(status_code=400, detail="invalid json")
        name = (body or {}).get("name", "")
        ok = app.state.registry.rename_tab(sid, tab_id, name)
        if not ok:
            raise HTTPException(status_code=400, detail="cannot rename")
        return JSONResponse({"ok": True})

    # ---------- WebSocket per tab ----------

    @app.websocket("/ws/{tab_id}")
    async def ws_tab(ws: WebSocket, tab_id: str) -> None:
        if not ws.session.get("authed"):
            await ws.close(code=4401)
            return
        sid = ws.session.get("sid")
        if not sid:
            await ws.close(code=4401)
            return
        registry: SessionRegistry = ws.app.state.registry
        tab = registry.get_tab(sid, tab_id)
        if not tab:
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
                    tab.terminal.resize(int(msg.get("rows", 24)), int(msg.get("cols", 80)))
        except Exception:
            pass
        finally:
            try:
                tab.subscribers.remove(q)
            except ValueError:
                pass
            pump_task.cancel()
            registry.on_ws_disconnect(sid, tab)

    return app
