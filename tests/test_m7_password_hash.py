"""§3.4 — password is never stored in plaintext.

Covers hash-password subcommand, --password-hash, argon2 + bcrypt verification,
and login flow over HTTP against a freshly started server (per-test).
"""
import http.client
import http.cookies
import os
import signal
import subprocess
import sys
import time
import urllib.parse
from urllib.parse import urlparse

VENV = os.environ.get("WCMUX_TEST_VENV", "/tmp/wcmux-venv")
HOST = "127.0.0.1"
PORT = int(os.environ.get("WCMUX_TEST_PORT", "8024"))
BASE = f"http://{HOST}:{PORT}"


def start_server(env_extra: dict, extra_args: list[str]) -> subprocess.Popen:
    env = os.environ.copy()
    env.update(env_extra)
    env.pop("http_proxy", None); env.pop("https_proxy", None)
    cmd = [f"{VENV}/bin/wcmux", "--port", str(PORT), "--host", HOST] + extra_args
    p = subprocess.Popen(cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    # wait for /healthz
    for _ in range(30):
        try:
            c = http.client.HTTPConnection(HOST, PORT, timeout=1)
            c.request("GET", "/healthz"); r = c.getresponse(); r.read(); c.close()
            if r.status == 200:
                return p
        except Exception:
            pass
        time.sleep(0.2)
    out, err = p.communicate(timeout=2)
    raise RuntimeError(f"server did not start; stderr={err!r}")


def stop_server(p: subprocess.Popen) -> tuple[str, str]:
    p.send_signal(signal.SIGTERM)
    try:
        out, err = p.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        p.kill(); out, err = p.communicate()
    return out.decode(errors="replace"), err.decode(errors="replace")


def post_login(password: str) -> int:
    conn = http.client.HTTPConnection(HOST, PORT, timeout=3)
    try:
        conn.request("POST", "/login",
                     body=urllib.parse.urlencode({"password": password}),
                     headers={"Content-Type": "application/x-www-form-urlencoded"})
        r = conn.getresponse(); r.read()
        return r.status
    finally:
        conn.close()


def run_cmd(args: list[str], env: dict = None) -> tuple[int, str, str]:
    p = subprocess.run(args, capture_output=True, text=True, timeout=15,
                       env={**os.environ, **(env or {})})
    return p.returncode, p.stdout, p.stderr


def main() -> int:
    results: list[tuple[str, bool, str]] = []

    # 1. hash-password subcommand (non-tty path via stdin pipe)
    rc, out, err = run_cmd([f"{VENV}/bin/wcmux", "hash-password"],
                           env={"PYTHONUNBUFFERED": "1"})
    # stdin pipe closed immediately → readline returns "" → "empty password"
    results.append(("hash-password empty stdin -> error", rc != 0 and "empty" in err, err[:80]))

    p = subprocess.run([f"{VENV}/bin/wcmux", "hash-password"],
                       input="tttt2209\n", capture_output=True, text=True, timeout=15)
    HASH = p.stdout.strip()
    results.append(("hash-password argon2id output",
                    p.returncode == 0 and HASH.startswith("$argon2id$"),
                    HASH[:20] + f" rc={p.returncode}"))

    # 2. start server with --password-hash and log in with the original plaintext
    srv = start_server({}, ["--password-hash", HASH])
    try:
        results.append(("hash login correct -> 303", post_login("tttt2209") == 303, ""))
        results.append(("hash login wrong -> 401", post_login("nope") == 401, ""))
    finally:
        stop_server(srv)

    # 3. start with bcrypt hash
    import bcrypt
    BC = bcrypt.hashpw(b"tttt2209", bcrypt.gensalt(4)).decode()
    srv = start_server({}, ["--password-hash", BC])
    try:
        results.append(("bcrypt login correct -> 303", post_login("tttt2209") == 303, BC[:8]))
        results.append(("bcrypt login wrong -> 401", post_login("nope") == 401, ""))
    finally:
        stop_server(srv)

    # 4. unsupported hash prefix -> startup failure
    rc, out, err = run_cmd([f"{VENV}/bin/wcmux",
                            "--port", str(PORT), "--host", HOST,
                            "--password-hash", "plaintext-not-a-hash"])
    results.append(("bogus hash -> exit nonzero", rc != 0 and "unsupported" in err.lower(),
                    err[:100]))

    # 5. plaintext fallback: server boots, warning logged, login works
    srv = start_server({"WCMUX_PASSWORD": "tttt2209"}, [])
    try:
        ok = post_login("tttt2209") == 303
        results.append(("plaintext fallback login", ok, ""))
    finally:
        out, err = stop_server(srv)
        has_warn = "consider providing --password-hash" in err
        results.append(("plaintext warning logged", has_warn, err[:120]))

    # 6. hash + plaintext both set -> hash wins, plaintext ignored with warn
    other_hash = subprocess.run([f"{VENV}/bin/wcmux", "hash-password"],
                                input="bb\n", capture_output=True, text=True).stdout.strip()
    srv = start_server({"WCMUX_PASSWORD": "aa", "WCMUX_PASSWORD_HASH": other_hash}, [])
    try:
        r_bb = post_login("bb"); r_aa = post_login("aa")
        results.append(("both set: bb wins", r_bb == 303 and r_aa == 401, f"bb={r_bb} aa={r_aa}"))
    finally:
        out, err = stop_server(srv)
        warned = "plaintext password ignored" in err
        results.append(("both-set warning logged", warned, err[:160]))

    # 7. no password at all -> startup failure
    env = {k: v for k, v in os.environ.items() if not k.startswith("WCMUX_")}
    env.pop("http_proxy", None); env.pop("https_proxy", None)
    p = subprocess.run([f"{VENV}/bin/wcmux", "--port", str(PORT), "--host", HOST],
                       capture_output=True, text=True, env=env, timeout=5)
    results.append(("no password -> exit nonzero",
                    p.returncode != 0 and "no password" in p.stderr.lower(),
                    p.stderr[:120]))

    failed = 0
    for name, ok, hint in results:
        flag = "PASS" if ok else "FAIL"
        print(f"{flag}  {name}", "" if ok else f"  ← {hint}")
        if not ok: failed += 1
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
