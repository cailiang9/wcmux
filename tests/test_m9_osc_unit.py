"""Unit tests for OSC 7 / OSC 0/2 parsing in sessions.py.

Runs without a server. Exercises the regex + tail-buffer logic by creating
a minimal TabState-like shim and calling _consume_osc directly.
"""
import sys
from dataclasses import dataclass, field

from wcmux.sessions import SessionRegistry, TabState, _decode_osc7


def _fake_terminal():
    class T:
        pid = 0
        alive = True
        def write(self, d): pass
        async def read(self): raise EOFError
        def resize(self, r, c): pass
        def close(self): pass
    return T()


def make_tab() -> TabState:
    return TabState(tab_id="t", name="Shell 1", terminal=_fake_terminal(),
                    workspace_id="ws")


def run() -> int:
    reg = SessionRegistry(shell="/bin/bash")
    failed = 0

    def check(desc, got, want):
        nonlocal failed
        ok = got == want
        print(("PASS" if ok else "FAIL") + "  " + desc)
        if not ok:
            print(f"        got  {got!r}")
            print(f"        want {want!r}")
            failed += 1

    # OSC 7 with BEL terminator, URL-encoded
    tab = make_tab()
    reg._consume_osc(tab, "\x1b]7;file://host/tmp/with%20space\x07")
    check("osc7 bel decodes url", tab.cwd_full, "/tmp/with space")

    # OSC 7 with ST terminator (ESC \)
    tab = make_tab()
    reg._consume_osc(tab, "\x1b]7;file://host/tmp\x1b\\")
    check("osc7 st terminator", tab.cwd_full, "/tmp")

    # OSC 2 title while not user-set → name takes title
    tab = make_tab()
    reg._consume_osc(tab, "\x1b]2;my project\x07")
    check("osc2 sets title", tab.name, "my project")

    # OSC 0 title also accepted
    tab = make_tab()
    reg._consume_osc(tab, "\x1b]0;bash\x07")
    check("osc0 sets title", tab.name, "bash")

    # OSC 2 ignored after user rename
    tab = make_tab()
    tab.name_user_set = True
    tab.name = "user-picked"
    reg._consume_osc(tab, "\x1b]2;shell-reports\x07")
    check("osc2 ignored after user rename", tab.name, "user-picked")

    # Cross-chunk split: ESC ] arrives at tail of first chunk; rest in second
    tab = make_tab()
    reg._consume_osc(tab, "prompt\x1b]7;file://h/us")
    check("partial osc7 not yet applied", tab.cwd_full, "")
    reg._consume_osc(tab, "r/local/bin\x07done")
    check("partial osc7 resumed across chunks", tab.cwd_full, "/usr/local/bin")

    # Buffer doesn't grow unbounded when noise piles up before a real OSC
    tab = make_tab()
    reg._consume_osc(tab, "\x1b]7;file://h" + "x" * 10000 + "\x07")
    # chunk contained a complete malformed OSC7 (no /path after host) → cwd unchanged
    check("malformed osc7 leaves cwd empty", tab.cwd_full, "")
    # buffer should have been emptied (no trailing partial)
    check("osc_buf reset after complete match", tab.osc_buf, "")

    # OSC 7 followed by OSC 2 in the same chunk, both applied
    tab = make_tab()
    reg._consume_osc(tab,
        "\x1b]7;file://h/srv\x07some output\x1b]2;srv\x07tail")
    check("two OSCs in one chunk: cwd", tab.cwd_full, "/srv")
    check("two OSCs in one chunk: name", tab.name, "srv")

    # _decode_osc7 edge cases
    check("no file:// prefix", _decode_osc7("http://h/tmp"), None)
    check("no hostname slash", _decode_osc7("file://"), None)
    check("url-encoded chinese",
          _decode_osc7("file://h/%E4%B8%AD%E6%96%87"), "/中文")

    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(run())
