from __future__ import annotations

import hmac
import time
from dataclasses import dataclass, field
from typing import Optional

from fastapi import APIRouter, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

MAX_FAILS = 5
LOCK_SECONDS = 15 * 60


@dataclass
class LockoutEntry:
    fails: int = 0
    locked_until: float = 0.0


class LockoutRegistry:
    def __init__(self) -> None:
        self._by_ip: dict[str, LockoutEntry] = {}

    def is_locked(self, ip: str, now: Optional[float] = None) -> bool:
        now = now or time.time()
        e = self._by_ip.get(ip)
        return bool(e and e.locked_until > now)

    def record_failure(self, ip: str, now: Optional[float] = None) -> bool:
        """Returns True if this failure triggered a lock."""
        now = now or time.time()
        e = self._by_ip.setdefault(ip, LockoutEntry())
        e.fails += 1
        if e.fails >= MAX_FAILS:
            e.locked_until = now + LOCK_SECONDS
            e.fails = 0  # reset counter; lock drives behavior
            return True
        return False

    def record_success(self, ip: str) -> None:
        self._by_ip.pop(ip, None)


def _client_ip(request: Request) -> str:
    trust = getattr(request.app.state.config, "trust_proxy", False)
    if trust:
        fwd = request.headers.get("x-forwarded-for", "")
        if fwd:
            return fwd.split(",")[0].strip()
    if request.client:
        return request.client.host
    return "unknown"


def _safe_next(raw: Optional[str], base_url: str) -> str:
    """Only allow same-origin redirects on the form /path[?q], rooted at base_url."""
    if not raw:
        return base_url + "/"
    if not raw.startswith("/"):
        return base_url + "/"
    if raw.startswith("//"):
        return base_url + "/"
    if base_url and not raw.startswith(base_url + "/") and raw != base_url:
        return base_url + "/"
    return raw


def build_auth_router(templates: Jinja2Templates) -> APIRouter:
    router = APIRouter()

    @router.get("/login", response_class=HTMLResponse)
    async def login_page(request: Request, next: str = "") -> HTMLResponse:
        config = request.app.state.config
        return templates.TemplateResponse(
            request,
            "login.html",
            {"error": None, "next": next, "base_url": config.base_url},
        )

    @router.post("/login")
    async def login_submit(
        request: Request,
        password: str = Form(...),
        next: str = Form(""),
    ):
        config = request.app.state.config
        lockout: LockoutRegistry = request.app.state.lockout
        ip = _client_ip(request)

        if lockout.is_locked(ip):
            return templates.TemplateResponse(
                request,
                "login.html",
                {
                    "error": "Too many failed attempts. Try again later.",
                    "next": next,
                    "base_url": config.base_url,
                },
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            )

        if not hmac.compare_digest(password, config.password):
            lockout.record_failure(ip)
            return templates.TemplateResponse(
                request,
                "login.html",
                {
                    "error": "Incorrect password.",
                    "next": next,
                    "base_url": config.base_url,
                },
                status_code=status.HTTP_401_UNAUTHORIZED,
            )

        lockout.record_success(ip)
        request.session["authed"] = True
        request.session["sid"] = request.session.get("sid") or _new_sid()
        target = _safe_next(next, config.base_url)
        return RedirectResponse(target, status_code=status.HTTP_303_SEE_OTHER)

    @router.post("/logout")
    async def logout(request: Request):
        # Caller (later) should terminate tabs before we clear the session.
        sid = request.session.get("sid")
        on_logout = getattr(request.app.state, "on_logout", None)
        if on_logout and sid:
            try:
                on_logout(sid)
            except Exception:
                pass
        request.session.clear()
        config = request.app.state.config
        return RedirectResponse(config.base_url + "/login",
                                status_code=status.HTTP_303_SEE_OTHER)

    return router


def _new_sid() -> str:
    import secrets
    return secrets.token_urlsafe(16)


def require_auth(request: Request) -> None:
    if not request.session.get("authed"):
        config = request.app.state.config
        nxt = request.url.path
        # Append query string if present
        if request.url.query:
            nxt = nxt + "?" + request.url.query
        raise HTTPException(
            status_code=status.HTTP_307_TEMPORARY_REDIRECT,
            headers={"Location": f"{config.base_url}/login?next={nxt}"},
        )
