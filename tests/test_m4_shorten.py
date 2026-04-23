"""M4 unit tests for wcmux.cwd.shorten — no server needed."""
import sys
from wcmux.cwd import shorten


def run() -> int:
    cases = [
        # (full, home, expected)
        ("/home/orangepi", "/home/orangepi", "~"),
        ("/home/orangepi/projects", "/home/orangepi", "~/projects"),
        ("/tmp", None, "/tmp"),
        ("/a/b", None, "/a/b"),
        # Exactly at 40 chars -> not shortened
        ("/" + "x" * 39, None, "/" + "x" * 39),
        # Over 40: shorten to head + … + last 2
        ("/var/log/application/daemon/2026/04/verylongpart", None,
         "/var/…/04/verylongpart"),
        # Home-replaced then still too long -> shorten
        ("/home/orangepi/projects/my-service-with-long-name/src/app/handlers.py",
         "/home/orangepi", "~/…/app/handlers.py"),
        # Single segment after ~ -> cannot shorten further (returned as-is)
        ("/home/orangepi/one-long-chunk-that-is-exactly-over-forty-characters",
         "/home/orangepi",
         "~/one-long-chunk-that-is-exactly-over-forty-characters"),
    ]
    failed = 0
    for full, home, expect in cases:
        got = shorten(full, home=home)
        ok = got == expect
        flag = "PASS" if ok else "FAIL"
        print(f"{flag}  shorten({full!r}, home={home!r})")
        print(f"        got   {got!r}")
        print(f"        want  {expect!r}")
        if not ok:
            failed += 1
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(run())
