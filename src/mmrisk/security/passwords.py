"""Passwords for the dashboard: scrypt with a random salt, compared in constant time.

Format: scrypt$N$r$p$salt(base64)$hash(base64). Only the hash is ever stored or put in an
environment variable. Standard library only.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets

N, R, P = 2**14, 8, 1  # about 16 MB and 50 ms on a laptop: slow enough to make guessing costly
MIN_LENGTH = 12
_KEYLEN = 32


class PasswordError(ValueError):
    """A password that is refused, or a stored hash that cannot be used."""


def _scrypt(password: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    return hashlib.scrypt(password.encode("utf-8"), salt=salt, n=n, r=r, p=p,
                          maxmem=128 * n * r * 2, dklen=_KEYLEN)  # fmt: skip


def hash_password(password: str) -> str:
    if len(password) < MIN_LENGTH:
        raise PasswordError(f"use at least {MIN_LENGTH} characters")
    salt = secrets.token_bytes(16)
    digest = _scrypt(password, salt, N, R, P)
    b64 = lambda b: base64.b64encode(b).decode("ascii")  # noqa: E731
    return f"scrypt${N}${R}${P}${b64(salt)}${b64(digest)}"


def parse(stored: str) -> tuple[int, int, int, bytes, bytes]:
    try:
        kind, n, r, p, salt, digest = stored.strip().split("$")
        if kind != "scrypt":
            raise ValueError
        n, r, p = int(n), int(r), int(p)
        if not (2**10 <= n <= 2**20 and n & (n - 1) == 0 and 1 <= r <= 32 and 1 <= p <= 8):
            raise ValueError
        return (
            n,
            r,
            p,
            base64.b64decode(salt, validate=True),
            base64.b64decode(digest, validate=True),
        )
    except (ValueError, TypeError):
        raise PasswordError("not a hash made by `mmrisk.security dash-password`") from None


def verify(password: str, stored: str) -> bool:
    """True if `password` matches `stored`. A malformed hash raises, so a typo is not 'wrong'."""
    n, r, p, salt, digest = parse(stored)
    return hmac.compare_digest(_scrypt(password, salt, n, r, p), digest)
