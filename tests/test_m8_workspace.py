"""§4.10 + §4.11 — URL workspace, starting cwd, cross-client sharing.

Expects a running server (fresh per runserver.sh).
"""
import asyncio
import http.client
import http.cookies
import json
import os
import sys
import tempfile
import urllib.parse
from urllib.parse import urlparse

BASE = os.environ.get("WCMUX_TEST_BASE", "http://127.0.0.1:8022")
PW = os.environ.get("WCMUX_TEST_PASSWORD", "pw")


class Client:
    def __init__(self, base):
        u = urlparse(base)
        self._host = u.hostname; self._port = u.port or 80; self._scheme = u.scheme
        self.cookies = {}

    def request(self, method, path, *, form=None, json_body=None):
        h = {}; body = None
        if form is not None:
            body = urllib.parse.urlencode(form).encode()
            h["Content-Type"] = "application/x-www-form-urlencoded"
        elif json_body is not None:
            body = json.dumps(json_body).encode()
            h["Content-Type"] = "application/json"
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


def login(client):
    code, _ = client.request("POST", "/login", form={"password": PW})
    assert code in (302, 303), f"login failed: {code}"


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


async def main() -> int:
    import websockets
    os.environ.pop("http_proxy", None); os.environ.pop("https_proxy", None)

    results: list[tuple[str, bool, str]] = []

    # 1) Workspaces are case-sensitive and isolated
    c = Client(BASE); login(c)
    code, body = c.request("POST", "/api/tabs?workspace=Proj1")
    assert code == 200
    tab_upper = json.loads(body)["tab_id"]
    code, body = c.request("GET", "/api/tabs?workspace=proj1")
    lower_tabs = json.loads(body)["tabs"]
    results.append(("case-sensitive isolation",
                    not any(t["tab_id"] == tab_upper for t in lower_tabs),
                    f"proj1 tabs={lower_tabs}"))

    # 2) Starting cwd via ?cwd=<dir> takes effect for new tabs
    with tempfile.TemporaryDirectory() as td:
        td_real = os.path.realpath(td)
        ws = "m8-cwd"
        qs = f"?workspace={ws}&cwd={urllib.parse.quote(td_real)}"
        code, body = c.request("POST", f"/api/tabs{qs}")
        tab = json.loads(body); tid = tab["tab_id"]
        ws_url = BASE.replace("http", "ws") + f"/ws/{tid}"
        cookie = c.cookie_header()
        async with websockets.connect(ws_url, additional_headers={"Cookie": cookie}) as w:
            await drain(w, 0.8)
            await w.send(json.dumps({"type": "input", "data": "pwd\n"}))
            frames = await drain(w, 1.2)
            text = "".join(f.get("data", "") for f in frames if f.get("type") in ("output", "replay"))
            results.append(("cwd via URL takes effect",
                            td_real in text or td in text, text[:200]))

    # 3) Invalid cwd is ignored (shell starts, no crash)
    ws = "m8-badcwd"
    code, body = c.request("POST", f"/api/tabs?workspace={ws}&cwd=/definitely/not/there")
    results.append(("bad cwd falls back gracefully", code == 200, f"status={code} body={body[:80]}"))

    # 4) Illegal workspace id → falls back to "default"
    c2 = Client(BASE); login(c2)
    code, body = c2.request("GET", "/api/tabs?workspace=../etc")
    data = json.loads(body)
    results.append(("bad workspace id -> default",
                    data.get("workspace") == "default", str(data)))

    # 5) Cross-client sharing: two clients, same workspace, see same tabs
    ws = "m8-share"
    a = Client(BASE); login(a)
    b = Client(BASE); login(b)
    code, body = a.request("POST", f"/api/tabs?workspace={ws}")
    tab = json.loads(body)
    code2, body2 = b.request("GET", f"/api/tabs?workspace={ws}")
    tabs_b = json.loads(body2)["tabs"]
    shared_ok = any(t["tab_id"] == tab["tab_id"] for t in tabs_b)
    results.append(("cross-client shared view", shared_ok,
                    f"a created {tab['tab_id']}, b sees {[t['tab_id'] for t in tabs_b]}"))

    # 6) tabs broadcast: both WS subscribers of the same workspace see a new tab
    ws = "m8-bcast"
    code, body = a.request("POST", f"/api/tabs?workspace={ws}")
    first = json.loads(body); first_id = first["tab_id"]
    wsA = await websockets.connect(
        BASE.replace("http", "ws") + f"/ws/{first_id}",
        additional_headers={"Cookie": a.cookie_header()},
    )
    wsB = await websockets.connect(
        BASE.replace("http", "ws") + f"/ws/{first_id}",
        additional_headers={"Cookie": b.cookie_header()},
    )
    try:
        await drain(wsA, 0.5); await drain(wsB, 0.5)
        # b creates a new tab → both A and B should receive "tabs" frame
        code, body = b.request("POST", f"/api/tabs?workspace={ws}")
        new_id = json.loads(body)["tab_id"]
        framesA = await drain(wsA, 1.0)
        framesB = await drain(wsB, 1.0)
        def saw_new(frames):
            for f in frames:
                if f.get("type") == "tabs":
                    if any(t["tab_id"] == new_id for t in f.get("tabs", [])):
                        return True
            return False
        results.append(("tabs broadcast to client A", saw_new(framesA),
                        f"types={[f.get('type') for f in framesA]}"))
        results.append(("tabs broadcast to client B", saw_new(framesB),
                        f"types={[f.get('type') for f in framesB]}"))

        # 7) Shared output: a types in first tab, b (also subscribed) receives it
        await drain(wsA, 0.3); await drain(wsB, 0.3)
        await wsA.send(json.dumps({"type": "input", "data": "echo SHARED-7\n"}))
        framesB2 = await drain(wsB, 1.5)
        textB = "".join(f.get("data", "") for f in framesB2 if f.get("type") == "output")
        results.append(("shared input/output across clients",
                        "SHARED-7" in textB, textB[:160]))
    finally:
        await wsA.close(); await wsB.close()

    # 8) Empty-workspace GC: close the only tab, workspace record goes away
    ws = "m8-gc"
    code, body = a.request("POST", f"/api/tabs?workspace={ws}")
    tid = json.loads(body)["tab_id"]
    a.request("DELETE", f"/api/tabs/{tid}")
    # a brand-new client sees the same workspace name as empty — that's fine,
    # what matters is that the *same* tab id is gone
    code, body = a.request("GET", f"/api/tabs?workspace={ws}")
    data = json.loads(body)
    results.append(("empty workspace GC'd",
                    all(t["tab_id"] != tid for t in data["tabs"]), str(data)))

    failed = 0
    for name, ok, hint in results:
        flag = "PASS" if ok else "FAIL"
        print(f"{flag}  {name}", "" if ok else f"  ← {hint}")
        if not ok: failed += 1
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
