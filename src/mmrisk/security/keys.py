"""API keys kept as hashes in a small JSON file.

A key looks like `mmk_<id>_<secret>`: the id is public (it goes in the audit log), the secret is 32
random bytes and is shown once when the key is made. The file holds only SHA-256 hashes. A hash is
enough here because the secret is random and long; passwords chosen by people are different (see
`passwords`). Comparison is constant time. A revoked key stays in the file, marked, so the audit
trail can still name it.

Roles: `score` may score transactions and read the model description, `metrics` may read
/metrics, `admin` may do both. `/health` and `/ready` need no key.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
from datetime import UTC, datetime
from pathlib import Path

ROLES = ("score", "metrics", "admin")
PREFIX = "mmk"
_TOKEN = re.compile(r"^mmk_([0-9a-f]{8})_([A-Za-z0-9_-]{43})$")
_DUMMY = hashlib.sha256(b"no such key").hexdigest()


class KeyFileError(ValueError):
    """The key file is unusable, or an operation on it is refused."""


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class KeyStore:
    def __init__(self, path: Path):
        self.path = Path(path)

    # ---- reading ------------------------------------------------------------------------
    def _load(self) -> dict:
        if not self.path.exists():
            return {"version": 1, "keys": []}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            ok = isinstance(data, dict) and isinstance(data.get("keys"), list)
            ok = ok and all(
                isinstance(k, dict) and {"id", "name", "role", "sha256"} <= k.keys()
                and k["role"] in ROLES for k in data["keys"]
            )  # fmt: skip
        except (OSError, ValueError):
            ok = False
        if not ok:
            raise KeyFileError(f"{self.path} is not a key file written by `mmrisk.security keys`")
        return data

    def keys(self) -> list[dict]:
        """Every key without its hash: id, name, role, created_at, revoked_at."""
        return [{k: v for k, v in key.items() if k != "sha256"} for key in self._load()["keys"]]

    def active_count(self) -> int:
        return sum(1 for k in self._load()["keys"] if not k.get("revoked_at"))

    def check(self, token: str) -> dict | None:
        """The key record (id, name, role) for a valid, unrevoked token, else None."""
        m = _TOKEN.match(token or "")
        data = self._load()
        record = next((k for k in data["keys"] if m and k["id"] == m.group(1)), None)
        expected = record["sha256"] if record else _DUMMY  # same work whether or not it exists
        same = hmac.compare_digest(_hash(token or ""), expected)
        if record is None or not same or record.get("revoked_at"):
            return None
        return {"id": record["id"], "name": record["name"], "role": record["role"]}

    # ---- writing ------------------------------------------------------------------------
    def _save(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(f".{self.path.name}.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(data, indent=2) + "\n")
        os.replace(tmp, self.path)

    def create(self, name: str, role: str) -> tuple[str, dict]:
        """Make a key. Returns (token, record); the token is shown once and not stored."""
        if role not in ROLES:
            raise KeyFileError(f"role must be one of {ROLES}")
        if not name.strip() or len(name) > 60:
            raise KeyFileError("a name of 1 to 60 characters is required")
        data = self._load()
        ids = {k["id"] for k in data["keys"]}
        while (kid := secrets.token_hex(4)) in ids:
            pass
        token = f"{PREFIX}_{kid}_{secrets.token_urlsafe(32)}"
        record = {"id": kid, "name": name.strip(), "role": role, "sha256": _hash(token),
                  "created_at": _now(), "revoked_at": None}  # fmt: skip
        data["keys"].append(record)
        self._save(data)
        return token, {k: v for k, v in record.items() if k != "sha256"}

    def revoke(self, key_id: str) -> dict:
        data = self._load()
        for k in data["keys"]:
            if k["id"] == key_id:
                if k.get("revoked_at"):
                    raise KeyFileError(f"key {key_id} is already revoked")
                k["revoked_at"] = _now()
                self._save(data)
                return {x: v for x, v in k.items() if x != "sha256"}
        raise KeyFileError(f"no key {key_id}")
