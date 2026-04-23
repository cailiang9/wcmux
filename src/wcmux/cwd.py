from __future__ import annotations

import os
import sys
from typing import Optional

try:
    import psutil
except ImportError:  # pragma: no cover
    psutil = None  # type: ignore[assignment]

MAX_LEN = 40


def _home() -> str:
    if sys.platform.startswith("win"):
        return os.environ.get("USERPROFILE") or os.path.expanduser("~")
    return os.environ.get("HOME") or os.path.expanduser("~")


def read_cwd(pid: int) -> Optional[str]:
    if psutil is None:
        return None
    try:
        return psutil.Process(pid).cwd()
    except Exception:
        return None


def shorten(full: str, home: Optional[str] = None, max_len: int = MAX_LEN) -> str:
    if not full:
        return ""
    h = home if home is not None else _home()
    s = full
    if h and (s == h or s.startswith(h + os.sep) or s.startswith(h + "/")):
        tail = s[len(h):]
        # normalize separator for display
        tail = tail.replace(os.sep, "/")
        s = "~" + tail
    else:
        s = s.replace(os.sep, "/")
    if len(s) <= max_len:
        return s
    # keep head (first segment or ~) + "…" + last 2 segments
    parts = s.split("/")
    # remove leading empty from absolute paths: "/a/b" -> ["", "a", "b"]
    head = parts[0]
    if head == "":
        head = "/" + parts[1] if len(parts) > 1 else "/"
        body = parts[2:]
    else:
        body = parts[1:]
    if len(body) <= 2:
        return s  # can't really shorten further
    last2 = "/".join(body[-2:])
    shortened = f"{head}/…/{last2}"
    # If still too long, fall back to head + "…" + last1
    if len(shortened) > max_len and len(body) >= 1:
        last1 = body[-1]
        shortened = f"{head}/…/{last1}"
    return shortened
