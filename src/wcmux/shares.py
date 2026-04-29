"""Public share registry (spec §4.23).

A share is a long-lived signed-by-randomness pointer to a single file under
the preview roots. The owner (any authed wcmux user) creates one, gets back a
URL of the shape /share/<YYYY-MM-DD>/<slug>-<id12>, and hands that out to
recipients who can view the rendered file (and any inline image assets) with
no login.

Persisted to ~/.local/share/wcmux/shares.json so revocation survives restart.
WCMUX_SHARES_FILE overrides the path (used by tests).
"""
from __future__ import annotations

import json
import os
import re
import secrets
import string
import threading
import time
from pathlib import Path
from typing import Optional

ID_LEN = 12
# alphanum (no `-` / `_`) → an id at the end of "<slug>-<id12>" can be
# unambiguously parsed by rsplit('-', 1).
ID_ALPHABET = string.ascii_letters + string.digits

# Per spec §4.23: presets in seconds. 0 = never expires.
EXPIRY_PRESETS = {
    "1d":   86400,
    "7d":   7 * 86400,
    "1mo":  30 * 86400,
    "3mo":  90 * 86400,
    "1y":   365 * 86400,
    "3y":   3 * 365 * 86400,
    "never": 0,
}

SLUG_MAX = 200
MAX_ASSETS = 50
MAX_ASSETS_BYTES = 100 * 1024 * 1024  # 100 MiB total per share

# Image src patterns we extract from markdown source. We don't try to be
# bulletproof here (a fully correct CommonMark scanner is a separate library);
# we hit the common forms.
_MD_IMG = re.compile(r'!\[([^\]]*)\]\(\s*([^)\s"\']+)(?:\s+"[^"]*")?\s*\)')
_HTML_IMG = re.compile(r'(<img[^>]+\bsrc=["\'])([^"\']+)(["\'])', re.IGNORECASE)
_NON_LOCAL_PREFIX = ("http://", "https://", "data:", "#", "mailto:", "javascript:")


def slugify(name: str) -> str:
    """Filename → URL-safe kebab slug, capped at SLUG_MAX. Empty → 'share'."""
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    s = re.sub(r"-{2,}", "-", s)
    return s[:SLUG_MAX] if s else "share"


def new_id() -> str:
    return "".join(secrets.choice(ID_ALPHABET) for _ in range(ID_LEN))


def parse_id_from_segment(seg: str) -> Optional[str]:
    """Extract the trailing 12-char id from a `slug-id12` URL segment.
    Returns None when the segment doesn't end in a valid id."""
    if not seg:
        return None
    last = seg.rsplit("-", 1)[-1]
    if len(last) == ID_LEN and all(c in ID_ALPHABET for c in last):
        return last
    return None


def expiry_seconds(label: str) -> Optional[int]:
    """Map preset label to seconds; None for unknown."""
    return EXPIRY_PRESETS.get(label)


def _default_path() -> Path:
    override = os.environ.get("WCMUX_SHARES_FILE")
    if override:
        return Path(override)
    xdg = os.environ.get("XDG_DATA_HOME")
    root = Path(xdg) if xdg else (Path.home() / ".local" / "share")
    return root / "wcmux" / "shares.json"


class ShareRegistry:
    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = path or _default_path()
        self._lock = threading.Lock()
        self._shares: dict[str, dict] = {}
        self._load()

    def _load(self) -> None:
        try:
            data = json.loads(self.path.read_text())
        except FileNotFoundError:
            return
        except (OSError, json.JSONDecodeError):
            return
        for s in data.get("shares", []):
            if isinstance(s, dict) and "id" in s:
                self._shares[s["id"]] = s

    def _save_locked(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(
            {"shares": list(self._shares.values())},
            indent=2, ensure_ascii=False,
        ))
        os.replace(tmp, self.path)

    def create(self, *,
               source_url_path: str,
               source_real_path: str,
               slug: str,
               label: str,
               expires_in: int,
               assets: list[dict],
               assets_skipped: list[dict]) -> dict:
        now = int(time.time())
        # Try a few times in case of (vanishingly unlikely) id collision.
        with self._lock:
            for _ in range(8):
                sid = new_id()
                if sid not in self._shares:
                    break
            else:
                raise RuntimeError("could not allocate share id")
            entry = {
                "id": sid,
                "date": time.strftime("%Y-%m-%d", time.localtime(now)),
                "slug": slug,
                "label": (label or "")[:200],
                "source_url_path": source_url_path,
                "source_real_path": source_real_path,
                "created_at": now,
                "expires_at": (now + expires_in) if expires_in > 0 else 0,
                "view_count": 0,
                "last_viewed": 0,
                "assets": assets,
                "assets_skipped": assets_skipped,
            }
            self._shares[sid] = entry
            self._save_locked()
            return dict(entry)

    def get(self, share_id: str) -> Optional[dict]:
        with self._lock:
            s = self._shares.get(share_id)
            return dict(s) if s else None

    def touch(self, share_id: str) -> None:
        with self._lock:
            s = self._shares.get(share_id)
            if not s:
                return
            s["view_count"] = int(s.get("view_count", 0)) + 1
            s["last_viewed"] = int(time.time())
            self._save_locked()

    def list(self) -> list[dict]:
        with self._lock:
            return [dict(s) for s in self._shares.values()]

    def revoke(self, share_id: str) -> bool:
        with self._lock:
            if share_id not in self._shares:
                return False
            del self._shares[share_id]
            self._save_locked()
            return True

    def gc_expired(self) -> int:
        """Drop entries whose expires_at is in the past. Returns count."""
        now = int(time.time())
        n = 0
        with self._lock:
            for sid in list(self._shares.keys()):
                s = self._shares[sid]
                exp = int(s.get("expires_at", 0))
                if exp != 0 and now > exp:
                    del self._shares[sid]
                    n += 1
            if n:
                self._save_locked()
        return n


# ---- markdown image scanning + URL rewriting ----

def iter_markdown_image_urls(md_text: str):
    """Yield raw image URLs found in markdown source (both `![](url)` and
    `<img src="url">` forms). Skips http/https/data:/anchor URLs."""
    seen: set[str] = set()
    for m in _MD_IMG.finditer(md_text):
        url = m.group(2)
        if url and not url.startswith(_NON_LOCAL_PREFIX) and url not in seen:
            seen.add(url)
            yield url
    for m in _HTML_IMG.finditer(md_text):
        url = m.group(2)
        if url and not url.startswith(_NON_LOCAL_PREFIX) and url not in seen:
            seen.add(url)
            yield url


def rewrite_markdown_images(md_text: str, replace_fn) -> str:
    """Replace each local `<img>` URL via `replace_fn(url) -> new_url | None`.
    None leaves the original in place (broken image), so the recipient can at
    least see something went wrong instead of a silent omission."""
    def md_sub(m: re.Match) -> str:
        alt = m.group(1)
        url = m.group(2)
        if url.startswith(_NON_LOCAL_PREFIX):
            return m.group(0)
        new = replace_fn(url)
        if new is None:
            return m.group(0)
        return f"![{alt}]({new})"

    def html_sub(m: re.Match) -> str:
        prefix, url, suffix = m.group(1), m.group(2), m.group(3)
        if url.startswith(_NON_LOCAL_PREFIX):
            return m.group(0)
        new = replace_fn(url)
        if new is None:
            return m.group(0)
        return f"{prefix}{new}{suffix}"

    md_text = _MD_IMG.sub(md_sub, md_text)
    md_text = _HTML_IMG.sub(html_sub, md_text)
    return md_text
