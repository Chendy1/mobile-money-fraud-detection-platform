"""What a run was made from: the data, the code and the libraries. No user or machine names."""

from __future__ import annotations

import hashlib
import platform
import subprocess
from importlib import metadata
from pathlib import Path

from mmrisk.ingestion.lake import LakePaths
from mmrisk.ingestion.ledger import Ledger

LIBRARIES = (
    "polars",
    "numpy",
    "scikit-learn",
    "lightgbm",
    "matplotlib",
    "duckdb",
    "mlflow",
    "shap",
)


def data_fingerprint(lake: Path) -> dict:
    """A hash of the completed ingestion batches (table and content hash of every source file).

    Two lakes built from the same source files give the same fingerprint, whatever the dates they
    were ingested on, and any changed, added or removed batch changes it.
    """
    ledger = Ledger.load(LakePaths(Path(lake)).ledger_path)
    batches = ledger.complete()
    lines = sorted(f"{b['table']}:{b['sha256']}" for b in batches)
    tables: dict[str, dict] = {}
    for b in batches:
        t = tables.setdefault(b["table"], {"batches": 0, "rows_cleaned": 0})
        t["batches"] += 1
        t["rows_cleaned"] += int(b.get("rows_cleaned") or 0)
    return {
        "sha256": hashlib.sha256("\n".join(lines).encode()).hexdigest(),
        "batches": len(batches),
        "tables": dict(sorted(tables.items())),
    }


def _git(args: list[str], cwd: Path) -> str | None:
    try:
        out = subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True, text=True, timeout=10, check=True
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip()


def code_version(root: Path | None = None) -> dict[str, str]:
    """The git commit the code was run from, and whether it had uncommitted changes."""
    root = Path(root) if root else Path.cwd()
    commit = _git(["rev-parse", "HEAD"], root)
    if not commit:
        return {"git_commit": "unknown", "git_dirty": "unknown"}
    status = _git(["status", "--porcelain"], root)
    return {
        "git_commit": commit,
        "git_dirty": "unknown" if status is None else str(bool(status)).lower(),
    }


def environment() -> dict[str, str]:
    out = {"python": platform.python_version(), "os": f"{platform.system()} {platform.machine()}"}
    for lib in LIBRARIES:
        try:
            out[lib] = metadata.version(lib)
        except metadata.PackageNotFoundError:
            continue
    return out
