"""M11 verification — device-token (spec §4.19).

Run via tests/runserver.sh tests/test_m11_device_token.py
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
    def __init__(self, base: str) -> None:
        u = urlparse(base)
        self._host = u.hostname
        self._port = u.port or 80
        self._scheme = u.scheme
        self._cookies: dict[str, str] = {}

    def _conn(self):
        if self._scheme == "https":
            return http.client.HTTPSConnection(self._host, self._port, timeout=3)
        return http.client.HTTPConnection(self._host, self._port, timeout=3)

    def request(self, method: str, path: str, *,
                form: dict | None = None,
                json_body: dict | None = None) -> tuple[int, dict, bytes]:
        headers: dict[str, str] = {}
        if self._cookies:
            headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in self._cookies.items())
        data: bytes | None = None
        if form is not None:
            data = urllib.parse.urlencode(form).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        elif json_body is not None:
            data = json.dumps(json_body).encode()
            headers["Content-Type"] = "application/json"
        c = self._conn()
        try:
            c.request(method, path, body=data, headers=headers)
            resp = c.getresponse()
            raw = resp.read()
            hdrs: dict = {}
            for k, v in resp.getheaders():
                hdrs.setdefault(k.lower(), []).append(v)
            for sc in hdrs.get("set-cookie", []):
                try:
                    ck = http.cookies.SimpleCookie()
                    ck.load(sc)
                    for name, morsel in ck.items():
                        ma = morsel["max-age"] if "max-age" in morsel else None
                        if ma and str(ma) == "0":
                            self._cookies.pop(name, None)
                        else:
                            self._cookies[name] = morsel.value
                except Exception:
                    pass
            flat = {k: ", ".join(v) for k, v in hdrs.items()}
            return resp.status, flat, raw
        finally:
            c.close()


def login(c: Client) -> int:
    code, _, _ = c.request("POST", "/login", form={"password": PW})
    return code


def main() -> int:
    os.environ.pop("http_proxy", None)
    os.environ.pop("https_proxy", None)

    results: list[tuple[str, bool, str]] = []

    # 1) Login + issue token
    c1 = Client(BASE)
    assert login(c1) in (302, 303), "precondition: login"
    code, _, body = c1.request(
        "POST", "/api/auth/issue-device-token",
        json_body={"label": "pytest-device"},
    )
    payload = {}
    try:
        payload = json.loads(body)
    except Exception:
        pass
    token = payload.get("token", "")
    dev_id = payload.get("id", "")
    results.append(("issue 200 with token+id", code == 200 and bool(token) and bool(dev_id),
                    f"code={code} body={body[:120]!r}"))

    # 2) Issue without auth -> 401
    c_anon = Client(BASE)
    code, _, _ = c_anon.request("POST", "/api/auth/issue-device-token", json_body={})
    results.append(("issue unauth -> 401", code == 401, f"got {code}"))

    # 3) Exchange the token (cookieless client) and access /api/tabs
    c2 = Client(BASE)
    code, hdr, _ = c2.request("POST", "/api/auth/exchange", json_body={"token": token})
    results.append(("exchange 200", code == 200, f"got {code}"))
    set_cookie = hdr.get("set-cookie", "")
    results.append(("exchange sets cookie", "wcmux_session" in set_cookie, set_cookie[:120]))
    code, _, _ = c2.request("GET", "/api/tabs")
    results.append(("authed via token cookie", code == 200, f"got {code}"))

    # 4) Bad token -> 401
    c_bad = Client(BASE)
    code, _, _ = c_bad.request("POST", "/api/auth/exchange",
                               json_body={"token": "totally.not.a.signed.value"})
    results.append(("bad token -> 401", code == 401, f"got {code}"))

    # 5) Empty / missing token -> 401
    c_em = Client(BASE)
    code, _, _ = c_em.request("POST", "/api/auth/exchange", json_body={})
    results.append(("empty token -> 401", code == 401, f"got {code}"))

    # 6) List devices includes ours
    code, _, body = c1.request("GET", "/api/auth/devices")
    listed = []
    try:
        listed = json.loads(body).get("devices", [])
    except Exception:
        pass
    results.append(("list contains issued device",
                    any(d.get("id") == dev_id for d in listed),
                    f"got code={code} list={listed!r}"))

    # 7) Revoke -> 200, then exchange same token -> 401
    code, _, _ = c1.request("DELETE", f"/api/auth/devices/{dev_id}")
    results.append(("revoke 200", code == 200, f"got {code}"))
    c3 = Client(BASE)
    code, _, _ = c3.request("POST", "/api/auth/exchange", json_body={"token": token})
    results.append(("exchange after revoke -> 401", code == 401, f"got {code}"))

    # 8) Revoke unknown id -> 404
    code, _, _ = c1.request("DELETE", "/api/auth/devices/does-not-exist")
    results.append(("revoke unknown -> 404", code == 404, f"got {code}"))

    failed = 0
    for name, ok, hint in results:
        flag = "PASS" if ok else "FAIL"
        print(f"{flag}  {name}", "" if ok else f"  ← {hint}")
        if not ok:
            failed += 1
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
