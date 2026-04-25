from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Optional

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from .devices import issue_token as issue_device_token, verify_token as verify_device_token
from .passhash import verify as verify_hash

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

        if not verify_hash(config.password_hash, password):
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

    # ---------- device tokens (spec §4.19) ----------
    # Long-lived signed tokens stored in localStorage so the user doesn't have
    # to re-enter the password every time the cookie gets evicted by the
    # browser (mobile/HTTP cookie cleanup is the usual culprit).

    @router.post("/api/auth/issue-device-token")
    async def issue_token_route(request: Request, _=Depends(require_auth)):
        try:
            body = await request.json()
        except Exception:
            body = {}
        label = (body or {}).get("label", "") or ""
        if not isinstance(label, str):
            label = ""
        config = request.app.state.config
        dev = request.app.state.devices.create(label=label)
        token = issue_device_token(config.secret_key, dev["id"])
        return JSONResponse({"token": token, "id": dev["id"]})

    @router.post("/api/auth/exchange")
    async def exchange_token_route(request: Request):
        # Same lockout bucket as /login: brute-force on either path counts.
        lockout: LockoutRegistry = request.app.state.lockout
        ip = _client_ip(request)
        if lockout.is_locked(ip):
            raise HTTPException(status_code=429, detail="too many attempts")
        try:
            body = await request.json()
        except Exception:
            body = {}
        token = (body or {}).get("token", "")
        if not isinstance(token, str):
            token = ""
        config = request.app.state.config
        dev_id = verify_device_token(config.secret_key, token)
        if not dev_id or not request.app.state.devices.touch(dev_id):
            lockout.record_failure(ip)
            raise HTTPException(status_code=401, detail="invalid or revoked token")
        lockout.record_success(ip)
        request.session["authed"] = True
        request.session["sid"] = request.session.get("sid") or _new_sid()
        request.session["seen"] = int(time.time())
        return JSONResponse({"ok": True, "id": dev_id})

    @router.get("/api/auth/devices")
    async def list_devices_route(request: Request, _=Depends(require_auth)):
        return JSONResponse({"devices": request.app.state.devices.list()})

    @router.delete("/api/auth/devices/{device_id}")
    async def revoke_device_route(device_id: str, request: Request,
                                  _=Depends(require_auth)):
        ok = request.app.state.devices.revoke(device_id)
        if not ok:
            raise HTTPException(status_code=404, detail="not found")
        return JSONResponse({"ok": True})

    @router.post("/logout")
    async def logout(request: Request):
        # Spec §4.11: logout only clears the browser cookie;
        # workspace terminals persist. The optional on_logout hook is legacy.
        on_logout = getattr(request.app.state, "on_logout", None)
        sid = request.session.get("sid")
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


SESSION_REFRESH_INTERVAL = 6 * 3600  # refresh Set-Cookie at most every 6h


def require_auth(request: Request) -> None:
    if not request.session.get("authed"):
        config = request.app.state.config
        # API/XHR callers can't usefully follow a 307 to /login (fetch silently
        # re-issues the original method and produces 422). Return 401 JSON so
        # the client can window.location.href themselves.
        if request.url.path.startswith(f"{config.base_url}/api/") or \
           request.url.path.startswith("/api/"):
            raise HTTPException(status_code=401, detail="auth required")
        nxt = request.url.path
        if request.url.query:
            nxt = nxt + "?" + request.url.query
        raise HTTPException(
            status_code=status.HTTP_307_TEMPORARY_REDIRECT,
            headers={"Location": f"{config.base_url}/login?next={nxt}"},
        )
    # Authed — opportunistically touch the session so SessionMiddleware emits
    # a fresh Set-Cookie (rolling Expires). Some browsers drop long-untouched
    # cookies on plain HTTP; periodic refresh keeps the cookie "seen recently".
    now = int(time.time())
    if now - int(request.session.get("seen", 0)) >= SESSION_REFRESH_INTERVAL:
        request.session["seen"] = now
