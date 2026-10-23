"""The reference a later window is compared with: a few numbers per feature, no rows."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path

import polars as pl

from mmrisk.drift import stats

FORMAT = 1


class ReferenceFileError(ValueError):
    """The reference file is missing, unreadable or not one this code wrote."""


def build(
    features: pl.DataFrame,
    scores: pl.Series,
    names: list[str],
    window: tuple[datetime, datetime],
    feature_version: str,
) -> dict:
    if features.is_empty():
        raise stats.DriftStatsError("the reference window has no transactions")
    if scores.len() != features.height:
        raise stats.DriftStatsError("one score per reference row is needed")
    missing = [n for n in names if n not in features.columns]
    if missing:
        raise stats.DriftStatsError(f"features not found: {', '.join(missing)}")
    return {
        "format": FORMAT,
        "feature_version": feature_version,
        "created_at": datetime.now(UTC).replace(tzinfo=None).isoformat(timespec="seconds"),
        "window": {"from": window[0].isoformat(), "to": window[1].isoformat()},
        "rows": features.height,
        "features": {n: stats.summarise(features[n]) for n in names},
        "score": stats.summarise(scores),
        "note": "Summary statistics of a reference window. Synthetic data only; no rows are kept.",
    }


def save(ref: dict, path: Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(ref, indent=1) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def load(path: Path) -> dict:
    try:
        ref = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ReferenceFileError(f"cannot read the reference {path}: {exc}") from None
    ok = isinstance(ref, dict) and ref.get("format") == FORMAT
    ok = ok and isinstance(ref.get("features"), dict) and ref["features"] and "score" in ref
    if not ok:
        raise ReferenceFileError(f"{path} is not a reference written by `reference`")
    for name, summary in [*ref["features"].items(), ("score", ref["score"])]:
        need = {"kind", "shares", "missing_share", "n"}
        if not isinstance(summary, dict) or not need <= summary.keys():
            raise ReferenceFileError(f"{path}: the summary of {name} is incomplete")
        if summary["kind"] == "numeric" and "edges" not in summary:
            raise ReferenceFileError(f"{path}: {name} has no bin edges")
        if summary["kind"] == "discrete" and "labels" not in summary:
            raise ReferenceFileError(f"{path}: {name} has no labels")
    return ref
