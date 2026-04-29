"""M12 verification — preview sub-router (spec §4.22).

Run via tests/runserver.sh tests/test_m12_preview.py
"""
import http.client
import http.cookies
import json
import os
import sys
import tempfile
import urllib.parse
from pathlib import Path
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
        return http.client.HTTPConnection(self._host, self._port, timeout=5)

    def request(self, method: str, path: str, *, form=None, json_body=None):
        headers = {}
        if self._cookies:
            headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in self._cookies.items())
        data = None
        if form is not None:
            data = urllib.parse.urlencode(form).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        elif json_body is not None:
            data = json.dumps(json_body).encode()
            headers["Content-Type"] = "application/json"
        c = self._conn()
        try:
            c.request(method, path, body=data, headers=headers)
            r = c.getresponse()
            raw = r.read()
            for sc in r.getheader("set-cookie", "").split(", "):
                if not sc:
                    continue
                try:
                    ck = http.cookies.SimpleCookie()
                    ck.load(sc)
                    for n, m in ck.items():
                        self._cookies[n] = m.value
                except Exception:
                    pass
            return r.status, raw
        finally:
            c.close()


def main() -> int:
    os.environ.pop("http_proxy", None)
    os.environ.pop("https_proxy", None)

    # Server is started by runserver.sh with WCMUX_PREVIEW_ROOT pointing here
    # (see fixture setup below); we don't control the running server's env from
    # inside this script, so we set things up under whatever PREVIEW_ROOT is.
    root_env = os.environ.get("WCMUX_PREVIEW_ROOT")
    if not root_env:
        print("FAIL  WCMUX_PREVIEW_ROOT not set in test env", file=sys.stderr)
        return 2
    root = Path(root_env)
    root.mkdir(parents=True, exist_ok=True)

    # Layout fixtures: depth 0/1/2 hits, depth 3 miss, hidden dir miss, deny dir miss.
    (root / "alpha.md").write_text("# alpha\n\nHello", encoding="utf-8")
    (root / "code.py").write_text("def f(): return 1\n", encoding="utf-8")
    (root / "data.jsonl").write_text('{"a":1}\n{"a":2}\n', encoding="utf-8")
    (root / "dir1").mkdir(exist_ok=True)
    (root / "dir1" / "beta.md").write_text("# beta", encoding="utf-8")
    (root / "dir1" / "dir2").mkdir(exist_ok=True)
    (root / "dir1" / "dir2" / "gamma.md").write_text("# gamma", encoding="utf-8")
    (root / "dir1" / "dir2" / "dir3").mkdir(exist_ok=True)
    # too deep — should NOT appear in search
    (root / "dir1" / "dir2" / "dir3" / "delta.md").write_text("# delta", encoding="utf-8")
    # hidden dir — should be skipped
    (root / ".hidden").mkdir(exist_ok=True)
    (root / ".hidden" / "secret.md").write_text("# secret", encoding="utf-8")
    # deny-listed dir
    (root / "node_modules").mkdir(exist_ok=True)
    (root / "node_modules" / "lib.md").write_text("# lib", encoding="utf-8")

    results: list[tuple[str, bool, str]] = []

    # 1) Unauth: search → 401
    c_anon = Client(BASE)
    code, body = c_anon.request("GET", "/api/preview/search?q=alpha")
    results.append(("search unauth -> 401", code == 401, f"got {code}"))

    code, body = c_anon.request("GET", "/api/preview/list")
    results.append(("list unauth -> 401", code == 401, f"got {code}"))

    code, body = c_anon.request("GET", "/raw/preview?path=alpha.md")
    results.append(("raw unauth -> 401 or 307", code in (401, 307), f"got {code}"))

    # 2) Login
    c = Client(BASE)
    code, _ = c.request("POST", "/login", form={"password": PW})
    assert code in (302, 303), f"login failed: {code}"

    # 3) search basic hits
    code, body = c.request("GET", "/api/preview/search?q=alpha")
    payload = json.loads(body)
    paths = [r["path"] for r in payload.get("results", [])]
    results.append(("search 'alpha' returns alpha.md",
                    "alpha.md" in paths, f"paths={paths}"))

    code, body = c.request("GET", "/api/preview/search?q=beta")
    payload = json.loads(body)
    paths = [r["path"] for r in payload.get("results", [])]
    results.append(("search 'beta' returns dir1/beta.md",
                    "dir1/beta.md" in paths, f"paths={paths}"))

    code, body = c.request("GET", "/api/preview/search?q=gamma")
    payload = json.loads(body)
    paths = [r["path"] for r in payload.get("results", [])]
    results.append(("search 'gamma' returns dir1/dir2/gamma.md (depth 2 boundary)",
                    "dir1/dir2/gamma.md" in paths, f"paths={paths}"))

    # 4) depth limit + hidden + deny-list
    code, body = c.request("GET", "/api/preview/search?q=delta")
    payload = json.loads(body)
    paths = [r["path"] for r in payload.get("results", [])]
    results.append(("depth>2 (delta) NOT returned",
                    "dir1/dir2/dir3/delta.md" not in paths, f"paths={paths}"))

    code, body = c.request("GET", "/api/preview/search?q=secret")
    payload = json.loads(body)
    paths = [r["path"] for r in payload.get("results", [])]
    results.append(("hidden dir NOT searched",
                    not any("/secret" in p or p.startswith("secret") for p in paths)
                    or all(not p.startswith(".hidden") for p in paths),
                    f"paths={paths}"))

    code, body = c.request("GET", "/api/preview/search?q=lib")
    payload = json.loads(body)
    paths = [r["path"] for r in payload.get("results", [])]
    results.append(("node_modules NOT searched",
                    all(not p.startswith("node_modules") for p in paths),
                    f"paths={paths}"))

    # 4b) directory names also match (spec §4.22)
    code, body = c.request("GET", "/api/preview/search?q=dir1")
    payload = json.loads(body)
    rows = payload.get("results", [])
    dir_hit = any(r["path"] == "dir1" and r["type"] == "dir" for r in rows)
    results.append(("dir name 'dir1' matches with type=dir",
                    dir_hit, f"rows={rows}"))

    code, body = c.request("GET", "/api/preview/search?q=dir2")
    payload = json.loads(body)
    rows = payload.get("results", [])
    dir2_hit = any(r["path"] == "dir1/dir2" and r["type"] == "dir" for r in rows)
    results.append(("nested dir 'dir1/dir2' matches",
                    dir2_hit, f"rows={rows}"))

    # depth-3 dir 'dir3' (its parent is at depth 2, so its parent's children
    # iteration is suppressed): NOT returned
    code, body = c.request("GET", "/api/preview/search?q=dir3")
    payload = json.loads(body)
    paths = [r["path"] for r in payload.get("results", [])]
    results.append(("over-deep dir 'dir3' NOT returned",
                    "dir1/dir2/dir3" not in paths, f"paths={paths}"))

    # 5) empty q
    code, body = c.request("GET", "/api/preview/search?q=")
    payload = json.loads(body)
    results.append(("empty q -> empty results",
                    payload.get("results") == [],
                    f"got {payload}"))

    # 6) list root + breadcrumb
    code, body = c.request("GET", "/api/preview/list")
    payload = json.loads(body)
    names = [it["name"] for it in payload.get("items", [])]
    results.append(("list shows alpha.md and dir1",
                    "alpha.md" in names and "dir1" in names,
                    f"names={names}"))

    # 7) file: markdown
    code, body = c.request("GET", "/api/preview/file?path=alpha.md")
    payload = json.loads(body)
    results.append(("file alpha.md -> markdown HTML",
                    code == 200 and payload.get("type") == "markdown" and "<h1" in payload.get("html", ""),
                    f"code={code} got={payload!r}"))

    # 8) file: code
    code, body = c.request("GET", "/api/preview/file?path=code.py")
    payload = json.loads(body)
    results.append(("file code.py -> code python",
                    code == 200 and payload.get("type") == "code" and payload.get("lang") == "python",
                    f"got={payload!r}"))

    # 9) file: jsonl
    code, body = c.request("GET", "/api/preview/file?path=data.jsonl")
    payload = json.loads(body)
    results.append(("file data.jsonl -> jsonl with 2 records",
                    code == 200 and payload.get("type") == "jsonl"
                    and len(payload.get("records", [])) == 2,
                    f"got={payload!r}"))

    # 10) path traversal
    code, body = c.request("GET", "/api/preview/file?path=../etc/passwd")
    results.append(("traversal -> 403 or 404",
                    code in (403, 404), f"got {code}"))

    # 11) raw on a recognized file (query-param form)
    code, body = c.request("GET", "/raw/preview?path=alpha.md")
    results.append(("raw alpha.md -> 200",
                    code == 200, f"got {code}"))

    # 11b) Extra root (spec §4.22 multi-root). Drop a file under the extra
    # root (configured via WCMUX_PREVIEW_EXTRA_ROOTS in runserver.sh) and
    # verify search picks it up + absolute path round-trips through file/raw.
    extra_env = os.environ.get("WCMUX_PREVIEW_EXTRA_ROOTS", "")
    extra_root_str = extra_env.split(":")[0] if extra_env else ""
    if extra_root_str:
        extra_root = Path(extra_root_str)
        extra_root.mkdir(parents=True, exist_ok=True)
        (extra_root / "ext-note.md").write_text("# extra", encoding="utf-8")
        # search hits it
        code, body = c.request(
            "GET", f"/api/preview/search?q=ext-note")
        payload = json.loads(body)
        rows = payload.get("results", [])
        ext_path = f"{extra_root}/ext-note.md"
        results.append(("search finds extra-root file w/ absolute path",
                        any(r["path"] == ext_path for r in rows),
                        f"rows={rows}"))
        # file API accepts absolute path
        ep = urllib.parse.quote(ext_path, safe="")
        code, body = c.request("GET", f"/api/preview/file?path={ep}")
        payload = json.loads(body) if body else {}
        results.append(("file API serves absolute path under extra root",
                        code == 200 and payload.get("type") == "markdown",
                        f"code={code} body={body[:120]!r}"))
        # raw API accepts absolute path
        code, _ = c.request("GET", f"/raw/preview?path={ep}")
        results.append(("raw API serves absolute path under extra root",
                        code == 200, f"got {code}"))
        # absolute path NOT under any root -> 403
        bad = urllib.parse.quote("/etc/passwd", safe="")
        code, _ = c.request("GET", f"/api/preview/file?path={bad}")
        results.append(("absolute path outside roots -> 403",
                        code == 403, f"got {code}"))

    # 12) save: only drawio allowed (markdown gets 403)
    code, body = c.request("POST", "/api/preview/save",
                           json_body={"path": "alpha.md", "content": "# changed"})
    results.append(("save markdown -> 403",
                    code == 403, f"got {code}"))

    failed = 0
    for name, ok, hint in results:
        flag = "PASS" if ok else "FAIL"
        print(f"{flag}  {name}", "" if ok else f"  ← {hint}")
        if not ok:
            failed += 1
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
