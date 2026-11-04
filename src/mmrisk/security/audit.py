"""An append-only audit log of security events, one JSON object per line.

It records who (a key id, never a key), what (an event name), where (route, client address) and
the outcome. It never records request bodies, transaction or customer identifiers, keys, or
passwords. It is a local file, not a tamper-proof system: whoever can write the file can change it.
"""

from __future__ import annotations

import json
import os
import threading
from datetime import UTC, datetime
from pathlib import Path

MAX_BYTES = 5 * 1024 * 1024  # one backup file is kept, so at most about twice this
_FIELDS = ("event", "key_id", "role", "method", "route", "status", "client", "request_id", "detail")


class AuditLog:
    def __init__(self, path: Path, max_bytes: int = MAX_BYTES):
        self.path = Path(path)
        self.max_bytes = max_bytes
        self._lock = threading.Lock()

    def record(self, event: str, **fields) -> None:
        unknown = set(fields) - set(_FIELDS)
        if unknown:  # a typo must not silently drop (or smuggle in) a field
            raise ValueError(f"unknown audit fields: {sorted(unknown)}")
        entry = {"at": datetime.now(UTC).isoformat(timespec="seconds"), "event": event}
        entry |= {k: v for k, v in fields.items() if v is not None}
        line = json.dumps(entry, default=str, separators=(",", ":")) + "\n"
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if self.path.exists() and self.path.stat().st_size + len(line) > self.max_bytes:
                os.replace(self.path, self.path.with_name(self.path.name + ".1"))
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write(line)

    def read(self, last: int = 50) -> list[dict]:
        if not self.path.exists():
            return []
        rows = []
        for line in self.path.read_text(encoding="utf-8").splitlines()[-last:]:
            try:
                rows.append(json.loads(line))
            except ValueError:
                rows.append({"event": "unreadable_line"})
        return rows
