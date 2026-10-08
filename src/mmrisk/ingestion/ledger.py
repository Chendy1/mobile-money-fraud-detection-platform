"""Ingestion ledger: the source of truth for which batches are COMPLETE.

A batch is identified by (table, sha256 of the source file). A batch is only marked
COMPLETE after every layer has been written, so a crash mid-batch is safe to re-run.
"""

from __future__ import annotations

import json
from pathlib import Path

from mmrisk.ingestion.lake import atomic_write_text

COMPLETE = "COMPLETE"
REJECTED = "REJECTED"


class Ledger:
    def __init__(self, path: Path, batches: list[dict] | None = None):
        self.path = Path(path)
        self.batches: list[dict] = batches or []

    @classmethod
    def load(cls, path: Path) -> Ledger:
        path = Path(path)
        if not path.exists():
            return cls(path)
        data = json.loads(path.read_text(encoding="utf-8"))
        return cls(path, data["batches"])

    def save(self) -> None:
        text = json.dumps({"version": 1, "batches": self.batches}, indent=2, sort_keys=True)
        atomic_write_text(text, self.path)

    def find(self, table: str, sha256: str) -> dict | None:
        for b in self.batches:
            if b["table"] == table and b["sha256"] == sha256:
                return b
        return None

    def complete(self, table: str | None = None) -> list[dict]:
        return [
            b
            for b in self.batches
            if b["status"] == COMPLETE and (table is None or b["table"] == table)
        ]

    def next_sequence(self) -> int:
        return len(self.complete()) + 1

    def upsert(self, record: dict) -> None:
        existing = self.find(record["table"], record["sha256"])
        if existing is None:
            self.batches.append(record)
        else:
            existing.clear()
            existing.update(record)
        self.save()
