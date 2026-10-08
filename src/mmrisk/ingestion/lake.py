"""Lake layout, atomic writes, hashing and layer readers."""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path

import polars as pl

LAYERS = ("raw", "validated", "cleaned", "quarantine")


@dataclass(frozen=True)
class LakePaths:
    root: Path

    def layer_dir(self, layer: str) -> Path:
        if layer not in LAYERS:
            raise ValueError(f"unknown layer {layer!r}")
        return self.root / layer

    @property
    def raw(self) -> Path:
        return self.layer_dir("raw")

    @property
    def meta(self) -> Path:
        return self.root / "_meta"

    @property
    def ledger_path(self) -> Path:
        return self.meta / "ledger.json"

    def cleanup_tmp(self) -> None:
        """Remove leftovers of interrupted writes (they never end in .parquet)."""
        if self.root.exists():
            for tmp in self.root.rglob("*.tmp"):
                tmp.unlink()


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def atomic_write_parquet(df: pl.DataFrame, path: Path) -> None:
    """Write to a temp file then rename, so readers never see a half-written file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    df.write_parquet(tmp)
    os.replace(tmp, path)


def atomic_write_text(text: str, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


__all__ = [
    "LAYERS",
    "LakePaths",
    "atomic_write_parquet",
    "atomic_write_text",
    "file_sha256",
]
