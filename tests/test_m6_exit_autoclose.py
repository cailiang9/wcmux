"""§4.8 — when the shell exits (Ctrl-D / exit), backend drops the tab
from its session. Frontend auto-close is visual and not covered here.
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
    def __init__(self, base):
        u = urlparse(base)
        self._host = u.hostname; self._port = u.port or 80; self._scheme = u.scheme
        self.cookies = {}

    def request(self, method, path, *, form=None):
        h = {}; body = None
        if form is not None:
            body = urllib.parse.urlencode(form).encode()
            h["Content-Type"] = "application/x-www-form-urlencoded"
        if self.cookies:
            h["Cookie"] = "; ".join(f"{k}={v}" for k, v in self.cookies.items())
        cls = http.client.HTTPSConnection if self._scheme == "https" else http.client.HTTPConnection
        c = cls(self._host, self._port, timeout=5)
        try:
            c.request(method, path, body=body, headers=h)
            r = c.getresponse(); raw = r.read()
            for k, v in r.getheaders():
                if k.lower() == "set-cookie":
                    try:
                        ck = http.cookies.SimpleCookie(); ck.load(v)
                        for name, m in ck.items(): self.cookies[name] = m.value
                    except Exception: pass
            return r.status, raw
        finally:
            c.close()

    def cookie_header(self):
        return "; ".join(f"{k}={v}" for k, v in self.cookies.items())


async def drain(ws, timeout):
    buf = []
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


async def main():
    import websockets
    os.environ.pop("http_proxy", None); os.environ.pop("https_proxy", None)
    c = Client(BASE)
    code, _ = c.request("POST", "/login", form={"password": PW})
    assert code in (302, 303)
    code, body = c.request("POST", "/api/tabs"); assert code == 200
    tab = json.loads(body); tid = tab["tab_id"]

    results = []

    ws_url = BASE.replace("http", "ws") + "/ws/" + tid
    async with websockets.connect(ws_url, additional_headers={"Cookie": c.cookie_header()}) as ws:
        await drain(ws, 0.8)
        # send `exit` to shell — bash will exit → PTY EOF → backend exit frame
        await ws.send(json.dumps({"type": "input", "data": "exit\n"}))
        frames = await drain(ws, 2.5)
        got_exit = any(f.get("type") == "exit" for f in frames)
        results.append(("got exit frame", got_exit, f"frames types: {[f.get('type') for f in frames]}"))

    # after shell exits, /api/tabs should no longer contain this tab
    code, body = c.request("GET", "/api/tabs")
    ids = [t["tab_id"] for t in json.loads(body)["tabs"]]
    results.append(("tab removed from registry", tid not in ids, f"ids={ids}"))

    # reconnecting to the now-gone tab should be 4404 (or 403)
    try:
        async with websockets.connect(ws_url, additional_headers={"Cookie": c.cookie_header()}) as ws2:
            results.append(("reconnect to dead tab fails", False, "connected unexpectedly"))
    except Exception as e:
        msg = str(e)
        ok = "4404" in msg or "404" in msg or "403" in msg or "reject" in msg.lower()
        results.append(("reconnect to dead tab fails", ok, f"{type(e).__name__}: {msg[:80]}"))

    failed = 0
    for name, ok, hint in results:
        flag = "PASS" if ok else "FAIL"
        print(f"{flag}  {name}", "" if ok else f"  ← {hint}")
        if not ok: failed += 1
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
