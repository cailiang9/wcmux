"""§4.10 + §4.11 — cwd-keyed workspaces and cross-client sharing.

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


def q(path: str) -> str:
    return urllib.parse.quote(path)


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

    c = Client(BASE); login(c)

    # 1) ?cwd= takes effect: new tab's shell starts in that dir
    WS1 = tempfile.mkdtemp(prefix="wcmux-m8-cwd-")
    code, body = c.request("POST", f"/api/tabs?cwd={q(WS1)}")
    tab = json.loads(body); tid = tab["tab_id"]
    ws_url = BASE.replace("http", "ws") + "/ws/" + tid
    cookie = c.cookie_header()
    async with websockets.connect(ws_url, additional_headers={"Cookie": cookie}) as w:
        await drain(w, 0.8)
        await w.send(json.dumps({"type": "input", "data": "pwd\n"}))
        frames = await drain(w, 1.2)
        text = "".join(f.get("data", "") for f in frames if f.get("type") in ("output", "replay"))
        results.append(("cwd via URL takes effect", WS1 in text, text[:200]))

    # 2) Trailing slash normalizes: /tmp/ and /tmp are the same workspace
    WS2 = tempfile.mkdtemp(prefix="wcmux-m8-norm-")
    c.request("POST", f"/api/tabs?cwd={q(WS2)}")
    _, body = c.request("GET", f"/api/tabs?cwd={q(WS2 + '/')}")
    data = json.loads(body)
    results.append(("trailing slash normalized",
                    len(data["tabs"]) == 1 and data["workspace"] == WS2,
                    f"workspace={data.get('workspace')} tabs={len(data['tabs'])}"))

    # 3) Case sensitive (Linux semantics): different cases → different workspaces
    WS3_lower = tempfile.mkdtemp(prefix="wcmux-m8-case-", dir="/tmp")
    WS3_upper = WS3_lower.replace("case-", "CASE-")
    os.makedirs(WS3_upper, exist_ok=True)
    c.request("POST", f"/api/tabs?cwd={q(WS3_lower)}")
    _, body = c.request("GET", f"/api/tabs?cwd={q(WS3_upper)}")
    data = json.loads(body)
    results.append(("case-sensitive isolation",
                    data["tabs"] == [], f"upper tabs={data['tabs']}"))

    # 4) Invalid cwd → 400
    code, body = c.request("POST", f"/api/tabs?cwd=/definitely/does/not/exist")
    results.append(("bad cwd -> 400", code == 400, f"status={code} body={body[:80]}"))

    # 5) No ?cwd= → default workspace = $HOME
    HOME = os.environ.get("HOME") or "/"
    _, body = c.request("GET", "/api/tabs")
    data = json.loads(body)
    results.append(("no cwd -> HOME default",
                    data["workspace"] == HOME.rstrip("/") or data["workspace"] == HOME,
                    f"workspace={data['workspace']} expected HOME={HOME}"))

    # 6) Cross-client shared view: two clients same cwd see same tabs
    WS_SHARE = tempfile.mkdtemp(prefix="wcmux-m8-share-")
    a = Client(BASE); login(a)
    b = Client(BASE); login(b)
    code, body = a.request("POST", f"/api/tabs?cwd={q(WS_SHARE)}")
    tab_a = json.loads(body)
    _, body = b.request("GET", f"/api/tabs?cwd={q(WS_SHARE)}")
    tabs_b = json.loads(body)["tabs"]
    results.append(("cross-client shared view",
                    any(t["tab_id"] == tab_a["tab_id"] for t in tabs_b),
                    f"a={tab_a['tab_id']}, b sees {[t['tab_id'] for t in tabs_b]}"))

    # 7) Tab stays in its original workspace even if `cd` elsewhere in the shell
    WS7 = tempfile.mkdtemp(prefix="wcmux-m8-stay-")
    code, body = a.request("POST", f"/api/tabs?cwd={q(WS7)}")
    tab = json.loads(body); tid = tab["tab_id"]
    async with websockets.connect(
        BASE.replace("http", "ws") + "/ws/" + tid,
        additional_headers={"Cookie": a.cookie_header()},
    ) as w:
        await drain(w, 0.5)
        await w.send(json.dumps({"type": "input", "data": "cd /var\n"}))
        await drain(w, 1.0)
    # Look up the original workspace — tab should still be there, not migrated
    _, body = a.request("GET", f"/api/tabs?cwd={q(WS7)}")
    data = json.loads(body)
    results.append(("cd inside shell doesn't migrate workspace",
                    any(t["tab_id"] == tid for t in data["tabs"]),
                    f"ws={WS7} has {[t['tab_id'] for t in data['tabs']]}"))

    # 8) Old ?workspace= param is ignored; cwd decides
    WS_OLD = tempfile.mkdtemp(prefix="wcmux-m8-legacy-")
    code, body = a.request("POST", f"/api/tabs?workspace=old-name&cwd={q(WS_OLD)}")
    data = json.loads(body)
    results.append(("legacy workspace= is ignored",
                    data["workspace"] == WS_OLD, f"ws={data.get('workspace')}"))

    # 9) Empty workspace GC (close all → re-GET returns empty; creating a new tab
    #    again works — we don't verify internal GC, just that the state is clean)
    WS_GC = tempfile.mkdtemp(prefix="wcmux-m8-gc-")
    code, body = a.request("POST", f"/api/tabs?cwd={q(WS_GC)}")
    tid = json.loads(body)["tab_id"]
    a.request("DELETE", f"/api/tabs/{tid}")
    _, body = a.request("GET", f"/api/tabs?cwd={q(WS_GC)}")
    data = json.loads(body)
    results.append(("empty workspace has no tabs",
                    all(t["tab_id"] != tid for t in data["tabs"]), str(data)))

    failed = 0
    for name, ok, hint in results:
        flag = "PASS" if ok else "FAIL"
        print(f"{flag}  {name}", "" if ok else f"  ← {hint}")
        if not ok: failed += 1
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
