"""M5 verification — base_url wiring.

Starts a server with --base-url /wcmux and verifies that generated URLs
(templates, static, WS) all include the prefix, while internal routes still
match the bare paths (since FastAPI's root_path is advisory).

Unlike other tests, this one manages its own server process so it can pass
the --base-url flag.
"""
import http.client
import os
import signal
import subprocess
import sys
import time
from urllib.parse import urlparse

BASE_HOST = "127.0.0.1"
BASE_PORT = int(os.environ.get("WCMUX_TEST_PORT", "8023"))
PW = os.environ.get("WCMUX_TEST_PASSWORD", "pw")
VENV = os.environ.get("WCMUX_TEST_VENV", "/tmp/wcmux-venv")


def start_server(extra_args: list[str]) -> subprocess.Popen:
    env = os.environ.copy()
    env["WCMUX_PASSWORD"] = PW
    for k in ("http_proxy", "https_proxy"):
        env.pop(k, None)
    cmd = [f"{VENV}/bin/wcmux", "--port", str(BASE_PORT), "--host", BASE_HOST] + extra_args
    p = subprocess.Popen(cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    # wait for up
    for _ in range(30):
        try:
            c = http.client.HTTPConnection(BASE_HOST, BASE_PORT, timeout=1)
            c.request("GET", "/healthz"); resp = c.getresponse(); resp.read(); c.close()
            if resp.status == 200:
                return p
        except Exception:
            time.sleep(0.2)
    raise RuntimeError("server did not start")


def fetch(path: str) -> tuple[int, dict, bytes]:
    c = http.client.HTTPConnection(BASE_HOST, BASE_PORT, timeout=3)
    try:
        c.request("GET", path)
        r = c.getresponse()
        raw = r.read()
        return r.status, {k.lower(): v for k, v in r.getheaders()}, raw
    finally:
        c.close()


def main() -> int:
    results: list[tuple[str, bool, str]] = []
    os.environ.pop("http_proxy", None)
    os.environ.pop("https_proxy", None)

    p = start_server(["--base-url", "/wcmux"])
    try:
        # Login page reached via bare path (FastAPI root_path is advisory)
        code, _, body = fetch("/login")
        html = body.decode("utf-8", errors="replace")
        results.append(("login page reachable at /login", code == 200, f"got {code}"))

        # Generated URLs in template include /wcmux prefix
        results.append(('form action="/wcmux/login"', 'action="/wcmux/login"' in html,
                        f"...{html[html.find('action'):html.find('action')+60]}..."
                        if "action" in html else html[:80]))

        # Terminal page (need auth): redirect location must include /wcmux prefix
        code, hdr, _ = fetch("/")
        loc = hdr.get("location", "")
        results.append(("/ -> redirect to /wcmux/login",
                        code in (302, 303, 307) and "/wcmux/login" in loc,
                        f"code={code} loc={loc!r}"))

        # Authenticated terminal page: template should reference /wcmux/static/*
        # (login first)
        c = http.client.HTTPConnection(BASE_HOST, BASE_PORT, timeout=3)
        c.request("POST", "/login", body="password=pw",
                  headers={"Content-Type": "application/x-www-form-urlencoded"})
        r = c.getresponse()
        set_cookie = None
        for k, v in r.getheaders():
            if k.lower() == "set-cookie" and "wcmux_session" in v:
                set_cookie = v.split(";", 1)[0]
                break
        r.read(); c.close()
        assert set_cookie, "no session cookie"

        c = http.client.HTTPConnection(BASE_HOST, BASE_PORT, timeout=3)
        c.request("GET", "/", headers={"Cookie": set_cookie})
        r = c.getresponse()
        html = r.read().decode("utf-8", errors="replace")
        c.close()
        results.append(("terminal page static URL has /wcmux prefix",
                        '/wcmux/static/app.js' in html or '/wcmux/static/style.css' in html,
                        "searching in page body"))
        results.append(("WCMUX_BASE_URL injected = '/wcmux'",
                        'WCMUX_BASE_URL = "/wcmux"' in html,
                        html[html.find("WCMUX_BASE_URL"):html.find("WCMUX_BASE_URL")+60]
                        if "WCMUX_BASE_URL" in html else "(not found)"))

    finally:
        p.send_signal(signal.SIGTERM)
        p.wait(timeout=5)

    # Second pass: base_url = "" (default) gives no prefix
    p = start_server([])
    try:
        code, _, body = fetch("/login")
        html = body.decode("utf-8", errors="replace")
        results.append(('default base_url: action="/login"',
                        'action="/login"' in html,
                        html[html.find("action"):html.find("action")+40]))
    finally:
        p.send_signal(signal.SIGTERM)
        p.wait(timeout=5)

    failed = 0
    for name, ok, hint in results:
        flag = "PASS" if ok else "FAIL"
        print(f"{flag}  {name}", "" if ok else f"  ← {hint}")
        if not ok: failed += 1
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
