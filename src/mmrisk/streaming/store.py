"""An append-only store of predictions: Parquet part files that are never rewritten.

Each part is written under a temporary name and renamed when complete, so a reader (the dashboard
or drift checks in later phases) never sees half a file. Opening a folder that already has parts
continues the numbering instead of overwriting.
"""

from __future__ import annotations

import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path

import polars as pl

SCHEMA = {
    "seq": pl.Int64,
    "transaction_id": pl.Utf8,
    "customer_id": pl.Utf8,
    "event_time": pl.Datetime("us"),
    "transaction_type": pl.Utf8,
    "amount_kes": pl.Int64,
    "channel": pl.Utf8,
    "location_county": pl.Utf8,
    "risk_probability": pl.Float64,
    "action": pl.Utf8,
    "risk_level": pl.Utf8,
    "reasons": pl.Utf8,  # JSON list of {rank, feature, effect, text}
    "feature_version": pl.Utf8,
    "processed_at": pl.Datetime("us"),  # naive UTC
}
_PART = re.compile(r"^part-(\d{6})\.parquet$")


class StoreError(ValueError):
    """The prediction folder cannot be used."""


def parts(directory: Path) -> list[Path]:
    d = Path(directory)
    return sorted(p for p in d.glob("part-*.parquet") if _PART.match(p.name)) if d.is_dir() else []


def read(directory: Path) -> pl.DataFrame:
    files = parts(directory)
    if not files:
        return pl.DataFrame(schema=SCHEMA)
    return pl.concat([pl.read_parquet(f) for f in files]).sort("seq")


class PredictionStore:
    def __init__(self, directory: Path, flush_rows: int = 5000):
        if flush_rows < 1:
            raise StoreError("flush_rows must be at least 1")
        self.dir = Path(directory)
        if self.dir.exists() and not self.dir.is_dir():
            raise StoreError(f"{self.dir} is a file, not a folder")
        self.dir.mkdir(parents=True, exist_ok=True)
        existing = [int(_PART.match(p.name).group(1)) for p in parts(self.dir)]
        self._next = max(existing, default=0) + 1
        self._rows: list[dict] = []
        self._flush_rows = flush_rows
        self.rows_written = 0

    @staticmethod
    def has_parts(directory: Path) -> bool:
        return bool(parts(directory))

    def add(self, seq: int, txn: dict, response: dict, processed_at: datetime | None = None):
        self._rows.append(
            {
                "seq": seq,
                "transaction_id": txn["transaction_id"],
                "customer_id": txn["customer_id"],
                "event_time": txn["timestamp"],
                "transaction_type": txn["transaction_type"],
                "amount_kes": txn["amount_kes"],
                "channel": txn["channel"],
                "location_county": txn["location_county"],
                "risk_probability": float(response["risk_probability"]),
                "action": response["action"],
                "risk_level": response["risk_level"],
                "reasons": json.dumps(response.get("reasons", [])),
                "feature_version": response["feature_version"],
                "processed_at": processed_at or datetime.now(UTC).replace(tzinfo=None),
            }
        )
        if len(self._rows) >= self._flush_rows:
            self.flush()

    def flush(self) -> None:
        if not self._rows:
            return
        name = f"part-{self._next:06d}.parquet"
        tmp = self.dir / f".{name}.tmp"
        pl.DataFrame(self._rows, schema=SCHEMA).write_parquet(tmp)
        os.replace(tmp, self.dir / name)
        self._next += 1
        self.rows_written += len(self._rows)
        self._rows = []

    def close(self) -> None:
        self.flush()
