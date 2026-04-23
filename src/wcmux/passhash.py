"""Password hashing — argon2id (generate + verify) and bcrypt (verify only)."""
from __future__ import annotations

from typing import Optional

import argon2

ARGON2_PREFIXES = ("$argon2id$", "$argon2i$", "$argon2d$")
BCRYPT_PREFIXES = ("$2a$", "$2b$", "$2y$")

# Spec §3.4: m=64 MiB, t=3, p=4
_hasher = argon2.PasswordHasher(
    memory_cost=64 * 1024,
    time_cost=3,
    parallelism=4,
)


def is_supported_hash(value: str) -> bool:
    if not value:
        return False
    return value.startswith(ARGON2_PREFIXES + BCRYPT_PREFIXES)


def hash_password(plaintext: str) -> str:
    """Return an argon2id hash string for the given plaintext."""
    return _hasher.hash(plaintext)


def verify(hash_str: str, plaintext: str) -> bool:
    """Constant-time verify. Returns False on any failure (including bad format)."""
    if not hash_str or plaintext is None:
        return False
    if hash_str.startswith(ARGON2_PREFIXES):
        try:
            _hasher.verify(hash_str, plaintext)
            return True
        except Exception:
            return False
    if hash_str.startswith(BCRYPT_PREFIXES):
        try:
            import bcrypt  # lazy import: optional dep
        except ImportError:
            return False
        try:
            return bcrypt.checkpw(plaintext.encode("utf-8"), hash_str.encode("utf-8"))
        except Exception:
            return False
    return False
