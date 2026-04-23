"""Integration: shell emits OSC 7 / OSC 2, wcmux pushes cwd/tabs frames."""
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

    c = Client(BASE)
    code, _ = c.request("POST", "/login", form={"password": PW})
    assert code in (302, 303), f"login {code}"
    code, body = c.request("POST", "/api/tabs?workspace=m9")
    assert code == 200
    tab = json.loads(body); tid = tab["tab_id"]

    ws_url = BASE.replace("http", "ws") + "/ws/" + tid
    cookie = c.cookie_header()

    results: list[tuple[str, bool, str]] = []

    async with websockets.connect(ws_url, additional_headers={"Cookie": cookie}) as ws:
        await drain(ws, 0.8)  # consume prompt

        # --- OSC 7 path: shell emits cwd → backend pushes "cwd" frame within a chunk --
        # Use printf with ST terminator, simpler than URL-encoding file://
        await ws.send(json.dumps({"type": "input",
                                  "data": "printf '\\e]7;file://host/tmp\\e\\\\'\n"}))
        frames = await drain(ws, 1.2)
        cwd_msgs = [f for f in frames if f.get("type") == "cwd" and f.get("full") == "/tmp"]
        results.append(("OSC 7 pushes cwd in-band (< 2s)", len(cwd_msgs) >= 1,
                        f"frames={[f.get('type') for f in frames]}"))

        # --- OSC 2 path: shell sets title → backend broadcasts "tabs" with new name.
        # Note bash's PROMPT_COMMAND on many distros sets its own OSC 0 right after,
        # which would overwrite ours. Accept success if OUR title appears in any
        # broadcast snapshot (our parser + broadcaster is what's under test).
        await ws.send(json.dumps({"type": "input",
                                  "data": "printf '\\e]2;hello-osc\\a'\n"}))
        frames = await drain(ws, 1.0)
        tab_msgs = [f for f in frames if f.get("type") == "tabs"]
        names_seen = []
        for f in tab_msgs:
            for t in f.get("tabs", []):
                if t["tab_id"] == tid:
                    names_seen.append(t.get("name", ""))
        results.append(("OSC 2 sets tab name via broadcast",
                        "hello-osc" in names_seen,
                        f"names_seen={names_seen}"))

        # --- User rename blocks further shell title overrides
        c.request("PATCH", f"/api/tabs/{tid}", json_body={"name": "user-pinned"})
        await drain(ws, 0.6)  # absorb broadcast
        await ws.send(json.dumps({"type": "input",
                                  "data": "printf '\\e]2;shell-tries-to-override\\a'\n"}))
        frames = await drain(ws, 1.0)
        name_after = None
        for f in frames:
            if f.get("type") == "tabs":
                for t in f.get("tabs", []):
                    if t["tab_id"] == tid:
                        name_after = t.get("name", "")
        # name should STAY "user-pinned"; either no tabs frame or frame still has user-pinned
        results.append(("OSC 2 ignored after user rename",
                        name_after in (None, "user-pinned"),
                        f"name_after={name_after!r}"))

        # --- confirm API snapshot agrees
        _, body = c.request("GET", "/api/tabs?workspace=m9")
        data = json.loads(body)
        snap_name = next((t["name"] for t in data["tabs"] if t["tab_id"] == tid), None)
        results.append(("snapshot reflects pinned name", snap_name == "user-pinned",
                        f"snap={snap_name!r}"))

    failed = 0
    for name, ok, hint in results:
        flag = "PASS" if ok else "FAIL"
        print(f"{flag}  {name}", "" if ok else f"  ← {hint}")
        if not ok: failed += 1
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
