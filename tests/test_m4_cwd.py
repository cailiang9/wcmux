"""M4 verification — cwd tracking + shorten + push.

Hotkeys are browser-side only and need a real DOM to verify — covered in
manual testing checklist (testing.md §4.7) rather than here.
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
HOME = os.environ.get("HOME", "/tmp")


class Client:
    def __init__(self, base: str) -> None:
        u = urlparse(base)
        self._host = u.hostname
        self._port = u.port or 80
        self._scheme = u.scheme
        self.cookies: dict[str, str] = {}

    def request(self, method: str, path: str, *, form=None):
        headers = {}
        body = None
        if form is not None:
            body = urllib.parse.urlencode(form).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        if self.cookies:
            headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in self.cookies.items())
        cls = http.client.HTTPSConnection if self._scheme == "https" else http.client.HTTPConnection
        c = cls(self._host, self._port, timeout=5)
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
            return resp.status, raw
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
    code, _ = client.request("POST", "/login", form={"password": PW})
    assert code in (302, 303), f"login got {code}"
    code, body = client.request("POST", "/api/tabs")
    assert code == 200
    tab = json.loads(body)
    tab_id = tab["tab_id"]

    ws_url = BASE.replace("http", "ws") + "/ws/" + tab_id
    cookie = client.cookie_header()

    results: list[tuple[str, bool, str]] = []

    async with websockets.connect(ws_url, additional_headers={"Cookie": cookie}) as ws:
        # initial burst (prompt + maybe first cwd)
        initial = await drain(ws, 3.0)  # poll is 2s
        cwd_msgs = [f for f in initial if f.get("type") == "cwd"]
        results.append(("initial cwd message", len(cwd_msgs) >= 1,
                        f"got {len(cwd_msgs)}: {cwd_msgs[:3]}"))
        if cwd_msgs:
            first = cwd_msgs[0]
            # shell may start in HOME or in the wcmux cwd; both are valid
            results.append(("cwd.full is non-empty", bool(first.get("full")),
                            str(first)))
            results.append(("cwd has display", "display" in first, str(first)))

        # cd to /tmp and wait for a cwd update
        await ws.send(json.dumps({"type": "input", "data": "cd /tmp\n"}))
        # poller runs every 2s; wait up to 4s for the update
        frames = await drain(ws, 4.5)
        cwd_msgs = [f for f in frames if f.get("type") == "cwd"]
        hit_tmp = any(m.get("full") == "/tmp" for m in cwd_msgs)
        results.append(("cwd update after `cd /tmp`", hit_tmp,
                        f"got={cwd_msgs}"))

        # cd to HOME -> display should become "~"
        await ws.send(json.dumps({"type": "input", "data": f"cd {HOME}\n"}))
        frames = await drain(ws, 4.5)
        cwd_msgs = [f for f in frames if f.get("type") == "cwd"]
        hit_home = any(m.get("full") == HOME and m.get("display") == "~" for m in cwd_msgs)
        results.append(("cwd=HOME display is '~'", hit_home, f"got={cwd_msgs}"))

        # no-op: stay put, ensure no duplicate cwd frames for 3s
        frames = await drain(ws, 3.0)
        dup = [f for f in frames if f.get("type") == "cwd"]
        results.append(("no duplicate cwd when unchanged", len(dup) == 0,
                        f"got {len(dup)}: {dup[:3]}"))

    failed = 0
    for name, ok, hint in results:
        flag = "PASS" if ok else "FAIL"
        print(f"{flag}  {name}", "" if ok else f"  ← {hint}")
        if not ok: failed += 1
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
