from __future__ import annotations

import json
import os
import secrets
import threading
import time
from pathlib import Path
from typing import Optional

from itsdangerous import BadSignature, SignatureExpired, TimestampSigner

TOKEN_SALT = "wcmux-device-token-v1"
TOKEN_MAX_AGE = 90 * 86400


def _default_data_path() -> Path:
    base = os.environ.get("WCMUX_DEVICES_FILE")
    if base:
        return Path(base)
    xdg = os.environ.get("XDG_DATA_HOME")
    root = Path(xdg) if xdg else (Path.home() / ".local" / "share")
    return root / "wcmux" / "devices.json"


class DeviceRegistry:
    """Allowlist of long-lived device tokens. Persisted to disk so revocation
    survives restart. Token signature is keyed by the app's secret_key, so
    rotating that key invalidates every device at once (emergency revoke)."""

    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = path or _default_data_path()
        self._lock = threading.Lock()
        self._devices: dict[str, dict] = {}
        self._load()

    def _load(self) -> None:
        try:
            data = json.loads(self.path.read_text())
        except FileNotFoundError:
            return
        except (OSError, json.JSONDecodeError):
            return
        for d in data.get("devices", []):
            if isinstance(d, dict) and "id" in d:
                self._devices[d["id"]] = d

    def _save_locked(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(
            {"devices": list(self._devices.values())},
            indent=2, ensure_ascii=False,
        ))
        os.replace(tmp, self.path)

    def create(self, label: str = "") -> dict:
        now = int(time.time())
        dev_id = secrets.token_urlsafe(12)
        dev = {"id": dev_id, "label": label[:128], "iat": now, "last_seen": now}
        with self._lock:
            self._devices[dev_id] = dev
            self._save_locked()
        return dict(dev)

    def touch(self, dev_id: str) -> bool:
        with self._lock:
            dev = self._devices.get(dev_id)
            if not dev:
                return False
            dev["last_seen"] = int(time.time())
            self._save_locked()
            return True

    def revoke(self, dev_id: str) -> bool:
        with self._lock:
            if dev_id not in self._devices:
                return False
            del self._devices[dev_id]
            self._save_locked()
            return True

    def list(self) -> list[dict]:
        with self._lock:
            return [dict(d) for d in self._devices.values()]


def issue_token(secret_key: str, dev_id: str) -> str:
    signer = TimestampSigner(secret_key, salt=TOKEN_SALT)
    return signer.sign(dev_id).decode("ascii")


def verify_token(secret_key: str, token: str) -> Optional[str]:
    """Returns the device id on success, None on bad/expired signature."""
    if not token:
        return None
    signer = TimestampSigner(secret_key, salt=TOKEN_SALT)
    try:
        raw = signer.unsign(token, max_age=TOKEN_MAX_AGE)
    except (BadSignature, SignatureExpired):
        return None
    return raw.decode("ascii", errors="replace")
