"""M3 verification — multi-tab REST + WS + resume + logout.

Requires a running wcmux server:
    WCMUX_PASSWORD=pw wcmux --port 8022 --host 127.0.0.1

Run:
    python tests/test_m3_multitab.py

Notes:
  This test does NOT verify the 5-minute retention literally (too slow).
  Instead, it verifies:
    - disconnect → tab survives and can be resumed
    - logout → tab is terminated (no longer in list and PID gone)
"""
import asyncio
import http.client
import http.cookies
import json
import os
import sys
import time
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
        if self._scheme == "https":
            return http.client.HTTPSConnection(self._host, self._port, timeout=5)
        return http.client.HTTPConnection(self._host, self._port, timeout=5)

    def request(self, method: str, path: str, *, form=None, json_body=None) -> tuple[int, dict, bytes]:
        headers = {}
        body = None
        if form is not None:
            body = urllib.parse.urlencode(form).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        elif json_body is not None:
            body = json.dumps(json_body).encode()
            headers["Content-Type"] = "application/json"
        if self.cookies:
            headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in self.cookies.items())
        c = self._conn()
        try:
            c.request(method, path, body=body, headers=headers)
            resp = c.getresponse()
            raw = resp.read()
            hdrs_flat: dict[str, str] = {}
            for k, v in resp.getheaders():
                if k.lower() == "set-cookie":
                    try:
                        ck = http.cookies.SimpleCookie()
                        ck.load(v)
                        for name, morsel in ck.items():
                            self.cookies[name] = morsel.value
                    except Exception:
                        pass
                hdrs_flat[k.lower()] = v
            return resp.status, hdrs_flat, raw
        finally:
            c.close()

    def cookie_header(self) -> str:
        return "; ".join(f"{k}={v}" for k, v in self.cookies.items())


def login(client: Client):
    code, _, _ = client.request("POST", "/login", form={"password": PW})
    assert code in (302, 303), f"login failed: {code}"


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

    results: list[tuple[str, bool, str]] = []

    client = Client(BASE)
    login(client)

    # Use isolated workspaces — spec §4.10 / §4.11 changed semantics so state
    # now persists across logouts. Fresh workspace gives clean state.
    WS = "m3a"
    WS_Q = f"?workspace={WS}"

    # 1) list initially empty for a fresh workspace
    code, _, body = client.request("GET", f"/api/tabs{WS_Q}")
    data = json.loads(body)
    results.append(("initial tabs empty", code == 200 and data["tabs"] == [], str(data)))

    # 2) create two tabs
    code, _, body = client.request("POST", f"/api/tabs{WS_Q}")
    t1 = json.loads(body)
    code2, _, body2 = client.request("POST", f"/api/tabs{WS_Q}")
    t2 = json.loads(body2)
    results.append(("create 2 tabs", code == 200 and code2 == 200 and t1["tab_id"] != t2["tab_id"],
                    f"t1={t1} t2={t2}"))
    results.append(("tab names sequential",
                    t1["name"] == "Shell 1" and t2["name"] == "Shell 2",
                    f"{t1['name']!r}/{t2['name']!r}"))

    # 3) GET /api/tabs shows 2 in order
    code, _, body = client.request("GET", f"/api/tabs{WS_Q}")
    data = json.loads(body)
    ids = [t["tab_id"] for t in data["tabs"]]
    results.append(("list has 2 tabs", code == 200 and ids == [t1["tab_id"], t2["tab_id"]], str(ids)))

    # 4) Drive tab 1 over WS, then disconnect, reconnect -> replay
    ws_url = BASE.replace("http", "ws") + "/ws/" + t1["tab_id"]
    cookie = client.cookie_header()

    pid_marker = f"wcmux-pid-{int(time.time())}"
    async with websockets.connect(ws_url, additional_headers={"Cookie": cookie}) as ws:
        await ws.send(json.dumps({"type": "resize", "rows": 24, "cols": 80}))
        await drain(ws, 0.8)
        await ws.send(json.dumps({"type": "input", "data": f"echo {pid_marker}\n"}))
        frames = await drain(ws, 1.2)
        text = "".join(f.get("data", "") for f in frames if f.get("type") in ("output","replay"))
        results.append(("tab1 echo", pid_marker in text, text[:120]))
        # print PID for later kill verification on logout
        await ws.send(json.dumps({"type": "input", "data": "echo PID-$$\n"}))
        frames = await drain(ws, 1.0)
        text = "".join(f.get("data", "") for f in frames if f.get("type") in ("output","replay"))
        # parse "PID-<n>"
        import re
        m = re.search(r"PID-(\d+)", text)
        pid1 = int(m.group(1)) if m else None

    # Reconnect -> should get replay containing prior marker
    async with websockets.connect(ws_url, additional_headers={"Cookie": cookie}) as ws:
        frames = await drain(ws, 1.0)
        replay_text = "".join(f.get("data", "") for f in frames if f.get("type") == "replay")
        results.append(("replay after reconnect", pid_marker in replay_text, replay_text[:120]))

    # 5) rename tab 1
    code, _, _ = client.request("PATCH", f"/api/tabs/{t1['tab_id']}", json_body={"name": "hello"})
    results.append(("rename 200", code == 200, f"got {code}"))
    _, _, body = client.request("GET", f"/api/tabs{WS_Q}")
    data = json.loads(body)
    name_after = next((t["name"] for t in data["tabs"] if t["tab_id"] == t1["tab_id"]), None)
    results.append(("rename took effect", name_after == "hello", f"name={name_after!r}"))

    # 6) close tab 2
    code, _, _ = client.request("DELETE", f"/api/tabs/{t2['tab_id']}")
    results.append(("delete 200", code == 200, f"got {code}"))
    _, _, body = client.request("GET", f"/api/tabs{WS_Q}")
    data = json.loads(body)
    ids_after = [t["tab_id"] for t in data["tabs"]]
    results.append(("after delete only t1", ids_after == [t1["tab_id"]], str(ids_after)))

    # 7) close non-existent -> 404
    code, _, _ = client.request("DELETE", f"/api/tabs/no-such-id")
    results.append(("delete non-existent -> 404", code == 404, f"got {code}"))

    # 8) spec §4.11: logout only clears the cookie; workspace terminals persist
    code, _, _ = client.request("POST", "/logout")
    results.append(("logout 303", code in (302, 303), f"got {code}"))
    c2 = Client(BASE); login(c2)
    _, _, body = c2.request("GET", f"/api/tabs{WS_Q}")
    data = json.loads(body)
    still_there = [t["tab_id"] for t in data["tabs"]] == [t1["tab_id"]]
    results.append(("tab persists across logout", still_there, str(data)))

    # Verify pid1 survived logout (new model)
    if pid1:
        try:
            os.kill(pid1, 0)
            results.append(("pid1 survives logout", True, ""))
        except OSError:
            results.append(("pid1 survives logout", False, f"pid {pid1} gone"))
    else:
        results.append(("pid1 survives logout", False, "could not parse pid"))

    # Explicit cleanup of the lingering tab (since logout no longer kills)
    c2.request("DELETE", f"/api/tabs/{t1['tab_id']}")

    # 9) cap at MAX_TABS (20) — fresh workspace `m3b` so count starts at 0
    WS_CAP = "m3b"
    WS_CAP_Q = f"?workspace={WS_CAP}"
    c3 = Client(BASE); login(c3)
    last_code = None
    for i in range(20):
        code, _, _ = c3.request("POST", f"/api/tabs{WS_CAP_Q}")
        last_code = code
        if code != 200: break
    results.append(("create 20 all 200", last_code == 200, f"last={last_code}"))
    code, _, _ = c3.request("POST", f"/api/tabs{WS_CAP_Q}")
    results.append(("21st -> 409", code == 409, f"got {code}"))

    failed = 0
    for name, ok, hint in results:
        flag = "PASS" if ok else "FAIL"
        print(f"{flag}  {name}", "" if ok else f"  ← {hint}")
        if not ok: failed += 1
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
