from __future__ import annotations

import os
import secrets
import sys
from dataclasses import dataclass, field
from typing import Optional


def normalize_base_url(raw: str) -> str:
    """Normalize base_url: '', '/', 'wcmux', '/wcmux', '/wcmux/' -> '' or '/wcmux'."""
    s = (raw or "").strip()
    s = s.strip("/")
    if not s:
        return ""
    return "/" + s


def default_shell() -> str:
    if sys.platform.startswith("win"):
        return os.environ.get("COMSPEC") or "powershell.exe"
    return os.environ.get("SHELL") or "/bin/bash"


@dataclass
class Config:
    # password_hash is the only stored credential (spec §3.4)
    password_hash: str
    port: int = 8022
    host: str = "0.0.0.0"
    base_url: str = ""
    shell: str = field(default_factory=default_shell)
    secret_key: str = ""
    secret_key_generated: bool = False
    trust_proxy: bool = False

    def __post_init__(self) -> None:
        self.base_url = normalize_base_url(self.base_url)
        if not (1 <= self.port <= 65535):
            raise ValueError(f"port must be in 1..65535, got {self.port}")
        if not self.password_hash:
            raise ValueError("password_hash is required")
        if not self.shell:
            self.shell = default_shell()
        if not self.secret_key:
            self.secret_key = secrets.token_urlsafe(32)
            self.secret_key_generated = True
