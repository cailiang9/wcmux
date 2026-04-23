"""When a bookmarked ?cwd= points at a workspace/session that no longer
exists on the server (deleted dir, server restart, etc.), the server
returns 400 and the client is expected to fall back to the default
workspace. This verifies the two server-side contracts the client relies on:

  - GET /api/tabs with an invalid cwd is lenient: 200 + empty list (so the
    page can load at all).
  - POST /api/tabs without cwd creates a tab in $HOME and works.

Combined with the 400 response documented in test_m8_workspace.py, this
is enough for the frontend's one-shot fallback (app.js) to work.
"""
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


def main() -> int:
    os.environ.pop("http_proxy", None); os.environ.pop("https_proxy", None)
    c = Client(BASE)
    code, _ = c.request("POST", "/login", form={"password": PW})
    assert code in (302, 303)

    results: list[tuple[str, bool, str]] = []

    BAD = "/this/path/definitely/does/not/exist"

    # GET is lenient — empty list, so the page can render.
    code, body = c.request("GET", f"/api/tabs?cwd={urllib.parse.quote(BAD)}")
    data = json.loads(body)
    results.append(("GET with bad cwd -> 200 empty",
                    code == 200 and data["tabs"] == [],
                    f"status={code} body={body[:80]}"))

    # POST with bad cwd is strict — 400.
    code, body = c.request("POST", f"/api/tabs?cwd={urllib.parse.quote(BAD)}")
    results.append(("POST with bad cwd -> 400", code == 400, f"status={code}"))

    # Fallback: POST without cwd succeeds (default workspace = HOME).
    code, body = c.request("POST", "/api/tabs")
    results.append(("POST without cwd succeeds",
                    code == 200, f"status={code} body={body[:100]}"))
    data = json.loads(body) if code == 200 else {}
    home = os.environ.get("HOME") or "/"
    results.append(("default workspace is HOME",
                    data.get("workspace") in (home.rstrip("/"), home, "/"),
                    f"got {data.get('workspace')!r}"))

    failed = 0
    for name, ok, hint in results:
        flag = "PASS" if ok else "FAIL"
        print(f"{flag}  {name}", "" if ok else f"  ← {hint}")
        if not ok: failed += 1
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
