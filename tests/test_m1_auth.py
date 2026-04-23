"""M1 verification — config & auth smoke tests.

Requires a running wcmux server:
    WCMUX_PASSWORD=pw wcmux --port 8022 --host 127.0.0.1

Run:
    python tests/test_m1_auth.py
"""
import http.client
import http.cookiejar
import http.cookies
import os
import sys
import urllib.parse
from urllib.parse import urlparse

BASE = os.environ.get("WCMUX_TEST_BASE", "http://127.0.0.1:8022")
PW = os.environ.get("WCMUX_TEST_PASSWORD", "pw")


class Client:
    """Minimal HTTP client that does NOT follow redirects; tracks cookies."""

    def __init__(self, base: str) -> None:
        u = urlparse(base)
        self._host = u.hostname
        self._port = u.port or 80
        self._scheme = u.scheme
        self._cookies: dict[str, str] = {}

    def _conn(self) -> http.client.HTTPConnection:
        if self._scheme == "https":
            return http.client.HTTPSConnection(self._host, self._port, timeout=3)
        return http.client.HTTPConnection(self._host, self._port, timeout=3)

    def request(self, method: str, path: str, body: dict | None = None) -> tuple[int, dict, bytes]:
        headers = {}
        if self._cookies:
            headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in self._cookies.items())
        data: bytes | None = None
        if body is not None:
            data = urllib.parse.urlencode(body).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        c = self._conn()
        try:
            c.request(method, path, body=data, headers=headers)
            resp = c.getresponse()
            raw = resp.read()
            hdrs: dict = {}
            for k, v in resp.getheaders():
                hdrs.setdefault(k.lower(), []).append(v)
            # update cookies
            for sc in hdrs.get("set-cookie", []):
                try:
                    ck = http.cookies.SimpleCookie()
                    ck.load(sc)
                    for name, morsel in ck.items():
                        # treat Max-Age=0 as deletion
                        max_age = morsel["max-age"] if "max-age" in morsel else None
                        if max_age and str(max_age) == "0":
                            self._cookies.pop(name, None)
                        else:
                            self._cookies[name] = morsel.value
                except Exception:
                    pass
            flat = {k: ", ".join(v) for k, v in hdrs.items()}
            return resp.status, flat, raw
        finally:
            c.close()


def main() -> int:
    os.environ.pop("http_proxy", None)
    os.environ.pop("https_proxy", None)

    results: list[tuple[str, bool, str]] = []
    client = Client(BASE)

    code, _, _ = client.request("GET", "/login")
    results.append(("login page 200", code == 200, f"got {code}"))

    code, hdr, _ = client.request("GET", "/")
    loc = hdr.get("location", "")
    results.append((
        "/ unauth redirects",
        code in (302, 303, 307) and "/login" in loc,
        f"code={code} loc={loc!r}",
    ))

    c2 = Client(BASE)  # fresh client so redirect test above didn't leak state
    code, _, _ = c2.request("POST", "/login", body={"password": "wrong"})
    results.append(("wrong pw -> 401", code == 401, f"got {code}"))

    c3 = Client(BASE)
    code, hdr, _ = c3.request("POST", "/login", body={"password": PW})
    set_cookie = hdr.get("set-cookie", "")
    results.append(("right pw -> 303", code in (302, 303), f"got {code}"))
    results.append(("cookie HttpOnly", "httponly" in set_cookie.lower(), set_cookie[:120]))
    results.append((
        "cookie SameSite=Lax",
        "samesite=lax" in set_cookie.lower(),
        set_cookie[:120],
    ))

    code, _, _ = c3.request("GET", "/")
    results.append(("/ authed -> 200", code == 200, f"got {code}"))

    code, _, _ = c3.request("POST", "/logout")
    results.append(("logout -> 303", code in (302, 303), f"got {code}"))

    code, _, _ = c3.request("GET", "/")
    results.append((
        "/ after logout -> redirect",
        code in (302, 303, 307),
        f"got {code}",
    ))

    # lockout: 5 wrong then correct should be blocked
    c4 = Client(BASE)
    codes = []
    for _ in range(5):
        cd, _, _ = c4.request("POST", "/login", body={"password": "bad"})
        codes.append(cd)
    cd, _, _ = c4.request("POST", "/login", body={"password": PW})
    results.append((
        "lockout after 5 fails",
        all(c == 401 for c in codes) and cd == 429,
        f"fail-codes={codes} final={cd}",
    ))

    failed = 0
    for name, ok, hint in results:
        flag = "PASS" if ok else "FAIL"
        print(f"{flag}  {name}", "" if ok else f"  ← {hint}")
        if not ok:
            failed += 1
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
