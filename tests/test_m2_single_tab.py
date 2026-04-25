"""M2 verification — single-tab terminal over WebSocket.

Creates one tab through /api/tabs, then drives it over /ws/{tab_id}.
Verifies: echo, UTF-8, TIOCSWINSZ resize, exit propagation, WS auth gate.

Requires a running wcmux server:
    WCMUX_PASSWORD=pw wcmux --port 8022 --host 127.0.0.1
"""
import asyncio
import http.client
import http.cookies
import json
import os
import sys
import urllib.parse
from urllib.parse import urlparse

BASE = os.environ.get("WCMUX_TEST_BASE", "http://127.0.0.1:8022")
PW = os.environ.get("WCMUX_TEST_PASSWORD", "pw")


class Client:
    def __init__(self, base: str) -> None:
        u = urlparse(base)
        self._host = u.hostname
        self._port = u.port or 80
        self._scheme = u.scheme
        self.cookies: dict[str, str] = {}

    def _conn(self):
        cls = http.client.HTTPSConnection if self._scheme == "https" else http.client.HTTPConnection
        return cls(self._host, self._port, timeout=5)

    def request(self, method: str, path: str, *, form=None) -> tuple[int, dict, bytes]:
        headers = {}
        body = None
        if form is not None:
            body = urllib.parse.urlencode(form).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        if self.cookies:
            headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in self.cookies.items())
        c = self._conn()
        try:
            c.request(method, path, body=body, headers=headers)
            resp = c.getresponse()
            raw = resp.read()
            for k, v in resp.getheaders():
                if k.lower() == "set-cookie":
                    try:
                        ck = http.cookies.SimpleCookie(); ck.load(v)
                        for name, morsel in ck.items():
                            self.cookies[name] = morsel.value
                    except Exception: pass
            return resp.status, dict(resp.getheaders()), raw
        finally:
            c.close()

    def cookie_header(self) -> str:
        return "; ".join(f"{k}={v}" for k, v in self.cookies.items())


async def drain(ws, timeout: float) -> list[dict]:
    buf: list[dict] = []
    try:
        end = asyncio.get_event_loop().time() + timeout
        while True:
            rem = end - asyncio.get_event_loop().time()
            if rem <= 0: break
            raw = await asyncio.wait_for(ws.recv(), timeout=rem)
            buf.append(json.loads(raw))
    except asyncio.TimeoutError:
        pass
    return buf


async def main() -> int:
    import websockets
    os.environ.pop("http_proxy", None)
    os.environ.pop("https_proxy", None)

    client = Client(BASE)
    code, _, _ = client.request("POST", "/login", form={"password": PW})
    assert code in (302, 303), f"login got {code}"

    code, _, body = client.request("POST", "/api/tabs")
    assert code == 200, f"create tab got {code}"
    tab = json.loads(body)
    tab_id = tab["tab_id"]

    ws_url = BASE.replace("http", "ws") + "/ws/" + tab_id
    cookie = client.cookie_header()

    results: list[tuple[str, bool, str]] = []

    async with websockets.connect(ws_url, additional_headers={"Cookie": cookie}) as ws:
        await ws.send(json.dumps({"type": "resize", "rows": 24, "cols": 80}))
        await drain(ws, 0.8)

        await ws.send(json.dumps({"type": "input", "data": "echo wcmux-hello-123\n"}))
        frames = await drain(ws, 1.2)
        text = "".join(f.get("data", "") for f in frames if f.get("type") in ("output","replay"))
        results.append(("echo", "wcmux-hello-123" in text, text[:120]))

        await ws.send(json.dumps({"type": "input", "data": "echo 你好世界\n"}))
        frames = await drain(ws, 1.0)
        text = "".join(f.get("data", "") for f in frames if f.get("type") in ("output","replay"))
        results.append(("utf8", "你好世界" in text, text[:120]))

        await ws.send(json.dumps({"type": "resize", "rows": 40, "cols": 120}))
        await ws.send(json.dumps({"type": "input", "data": "tput cols; tput lines\n"}))
        frames = await drain(ws, 1.0)
        text = "".join(f.get("data", "") for f in frames if f.get("type") in ("output","replay"))
        results.append(("resize", "120" in text and "40" in text, text[:120]))

        await ws.send(json.dumps({"type": "input", "data": "exit\n"}))
        frames = await drain(ws, 1.5)
        got_exit = any(f.get("type") == "exit" for f in frames)
        results.append(("exit", got_exit, ""))

    # unauthenticated WS: server now accepts then sends close-frame 4401 so
    # the browser can read the code (vs. opaque 1006 from a bare 403).
    async def _expect_close_code(url: str, *, headers: dict | None = None) -> int | None:
        ws = await websockets.connect(url, additional_headers=headers or {})
        try:
            try:
                await ws.recv()
            except websockets.ConnectionClosed as e:
                return getattr(e, "code", None) or (e.rcvd.code if getattr(e, "rcvd", None) else None)
            return None
        finally:
            try: await ws.close()
            except Exception: pass

    try:
        code = await _expect_close_code(ws_url)
        results.append(("ws_auth_reject", code == 4401, f"close code={code}"))
    except Exception as e:
        # legacy fallback: handshake refused (close-before-accept) is also acceptable
        msg = str(e)
        ok = "401" in msg or "403" in msg or "4401" in msg or "reject" in msg.lower()
        results.append(("ws_auth_reject", ok, f"{type(e).__name__}: {msg[:80]}"))

    try:
        code = await _expect_close_code(ws_url + "-bad", headers={"Cookie": cookie})
        results.append(("ws_bad_tab_reject", code == 4404, f"close code={code}"))
    except Exception as e:
        msg = str(e)
        ok = "403" in msg or "404" in msg or "4404" in msg or "reject" in msg.lower()
        results.append(("ws_bad_tab_reject", ok, f"{type(e).__name__}: {msg[:80]}"))

    failed = 0
    for name, ok, hint in results:
        flag = "PASS" if ok else "FAIL"
        print(f"{flag}  {name}", "" if ok else f"  ← {hint!r}")
        if not ok: failed += 1
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
