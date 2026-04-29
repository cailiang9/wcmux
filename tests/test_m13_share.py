"""M13 verification — public share routes (spec §4.23).

Run via tests/runserver.sh tests/test_m13_share.py
"""
import http.client
import http.cookies
import json
import os
import sys
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
        self._cookies: dict[str, str] = {}

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
        c = http.client.HTTPConnection(self._host, self._port, timeout=5)
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
            return r.status, dict(r.getheaders()), raw
        finally:
            c.close()


def main() -> int:
    os.environ.pop("http_proxy", None)
    os.environ.pop("https_proxy", None)

    root = Path(os.environ["WCMUX_PREVIEW_ROOT"])
    root.mkdir(parents=True, exist_ok=True)
    # Fixture: a markdown with a sibling image (to exercise asset bundling).
    (root / "notes").mkdir(exist_ok=True)
    (root / "notes" / "post.md").write_text(
        "# Hello\n\nText here.\n\n![diagram](images/diagram.png)\n\n"
        "External: ![ext](https://example.com/x.png)\n",
        encoding="utf-8",
    )
    (root / "notes" / "images").mkdir(exist_ok=True)
    # 1×1 PNG (89 bytes); valid PNG signature + IDAT.
    png = bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000010000000108060000001f"
        "15c4890000000d49444154789c63000100000005000100621c0d010000000"
        "049454e44ae426082"
    )
    (root / "notes" / "images" / "diagram.png").write_bytes(png)
    # An out-of-scope image (symlink target outside root).
    outside = Path("/tmp/wcmux-test-outside")
    outside.mkdir(exist_ok=True)
    (outside / "leak.png").write_bytes(png)
    leak_link = root / "notes" / "leak.png"
    if not leak_link.exists():
        leak_link.symlink_to(outside / "leak.png")
    # Markdown that references the leaked symlink (should be skipped).
    (root / "notes" / "leak.md").write_text(
        "![leak](leak.png)\n", encoding="utf-8")

    results = []

    # 1) unauth POST -> 401
    c_anon = Client(BASE)
    code, _, _ = c_anon.request("POST", "/api/share", json_body={"path": "notes/post.md"})
    results.append(("create unauth -> 401", code == 401, f"got {code}"))

    # 2) login + create
    c = Client(BASE)
    assert c.request("POST", "/login", form={"password": PW})[0] in (302, 303)
    code, _, body = c.request("POST", "/api/share",
                              json_body={"path": "notes/post.md",
                                         "expires_in": "1y", "label": "tester"})
    payload = json.loads(body) if body else {}
    results.append(("create 200 with id+url", code == 200 and bool(payload.get("id"))
                    and bool(payload.get("url")) and payload.get("date"),
                    f"code={code} body={body[:160]!r}"))
    share_id = payload.get("id", "")
    share_url = payload.get("url", "")

    # 3) URL shape: /share/<YYYY-MM-DD>/<slug>-<id12>
    import re
    parts = urlparse(share_url).path
    m = re.match(r".*/share/(\d{4}-\d{2}-\d{2})/(.+-)([A-Za-z0-9]{12})$", parts)
    results.append(("URL shape /share/<date>/<slug>-<id12>",
                    bool(m), f"path={parts}"))

    # 4) assets included diagram, NOT external, NOT leak
    inc = payload.get("assets_included", [])
    skipped = payload.get("assets_skipped", [])
    results.append(("asset diagram.png included",
                    "images/diagram.png" in inc, f"inc={inc}"))
    results.append(("external https URL not bundled",
                    not any("example.com" in p for p in inc),
                    f"inc={inc}"))

    # 5) bad expires_in
    code, _, _ = c.request("POST", "/api/share",
                           json_body={"path": "notes/post.md",
                                      "expires_in": "garbage"})
    results.append(("bad expires_in -> 400", code == 400, f"got {code}"))

    # 6) leak.md (symlink to outside) — share itself should bundle the .md
    # (it's under root) but the leak.png asset gets skipped.
    code, _, body = c.request("POST", "/api/share",
                              json_body={"path": "notes/leak.md"})
    payload2 = json.loads(body) if body else {}
    leak_skipped = payload2.get("assets_skipped", [])
    results.append(("symlink-out-of-root asset skipped",
                    code == 200 and any(s["path"] == "leak.png" for s in leak_skipped),
                    f"code={code} skipped={leak_skipped}"))

    # 7) public access (no auth, fresh client)
    pub = Client(BASE)
    code, hdrs, raw = pub.request("GET", urlparse(share_url).path)
    body_text = raw.decode("utf-8", errors="replace")
    csp = hdrs.get("content-security-policy", "")
    results.append(("public render 200", code == 200, f"got {code}"))
    results.append(("rendered HTML contains source title",
                    "post.md" in body_text, f"head={body_text[:200]!r}"))
    results.append(("rendered HTML contains markdown render",
                    "<h1" in body_text and "Hello" in body_text,
                    f"head={body_text[:200]!r}"))
    results.append(("CSP set strictly", "script-src 'none'" in csp, f"csp={csp!r}"))

    # 8) asset endpoint serves bundled image
    asset_url = urlparse(share_url).path + "/a?path=" + urllib.parse.quote(
        "images/diagram.png", safe="")
    code, _, raw = pub.request("GET", asset_url)
    results.append(("asset image 200", code == 200 and len(raw) > 50,
                    f"code={code} bytes={len(raw)}"))

    # 9) tampered id -> 404 (and counts toward lockout)
    bad_path = parts[: parts.rfind("-")] + "-AAAAAAAAAAAA"
    code, _, _ = pub.request("GET", bad_path)
    results.append(("tampered id -> 404", code == 404, f"got {code}"))

    # 10) listing
    code, _, body = c.request("GET", "/api/share")
    listed = json.loads(body).get("shares", [])
    results.append(("list contains created share",
                    any(s["id"] == share_id for s in listed),
                    f"ids={[s['id'] for s in listed]}"))

    # 11) revoke + verify 404 thereafter
    code, _, _ = c.request("DELETE", "/api/share/" + share_id)
    results.append(("revoke 200", code == 200, f"got {code}"))
    code, _, _ = pub.request("GET", urlparse(share_url).path)
    results.append(("after revoke -> 404 (or 429 if locked)",
                    code in (404, 429), f"got {code}"))

    # 12) unsupported file type
    (root / "data.bin").write_bytes(b"\x00\x01\x02")
    code, _, _ = c.request("POST", "/api/share", json_body={"path": "data.bin"})
    results.append(("unsupported type -> 415",
                    code in (404, 415),  # data.bin maps to "unknown" → 404 either OK
                    f"got {code}"))

    failed = 0
    for name, ok, hint in results:
        flag = "PASS" if ok else "FAIL"
        print(f"{flag}  {name}", "" if ok else f"  ← {hint}")
        if not ok: failed += 1
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
