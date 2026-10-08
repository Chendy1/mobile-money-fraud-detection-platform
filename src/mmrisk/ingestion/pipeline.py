"""Batch ingestion: landing -> raw -> validated -> quarantine -> cleaned.

Guarantees
- Idempotent: a batch is identified by the SHA-256 of its source file; COMPLETE batches are skipped.
- Crash-safe: files are written atomically and the ledger is updated last.
- Nothing is dropped silently: rejected and conflicting rows go to quarantine with a reason.
- Duplicate policy: first write wins; identical later copies are dropped and counted;
  later copies with different content are quarantined as conflicts.
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import polars as pl

from mmrisk.ingestion.lake import LakePaths, atomic_write_parquet, file_sha256
from mmrisk.ingestion.ledger import COMPLETE, REJECTED, Ledger
from mmrisk.ingestion.reader import read_layer
from mmrisk.ingestion.specs import (
    INGEST_ORDER,
    SPECS,
    TableSpec,
    table_from_filename,
)
from mmrisk.ingestion.validate import schema_problems, validate_frame

INGESTED = "INGESTED"
SKIPPED = "SKIPPED_DUPLICATE_BATCH"
REJECTED_BATCH = "REJECTED"

Clock = Callable[[], datetime]


@dataclass
class BatchResult:
    table: str
    source_file: str
    batch_id: str
    status: str
    reason: str | None = None
    rows_in: int = 0
    rows_rejected: int = 0
    rows_duplicate: int = 0
    rows_conflict: int = 0
    rows_cleaned: int = 0


def _now(clock: Clock | None) -> datetime:
    ts = (clock or (lambda: datetime.now(UTC)))()
    if ts.tzinfo is not None:
        ts = ts.astimezone(UTC).replace(tzinfo=None)
    return ts


def _land_raw(path: Path, lake: LakePaths, table: str, batch_id: str, sha: str, ts: datetime):
    """Immutable verbatim copy of the source file; the audit trail for everything downstream."""
    found = (
        list((lake.raw / table).glob(f"ingest_date=*/{batch_id}.parquet"))
        if lake.raw.exists()
        else []
    )
    if found:
        return found[0]
    dest = lake.raw / table / f"ingest_date={ts.date()}" / f"{batch_id}.parquet"
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".tmp")
    shutil.copyfile(path, tmp)
    os.replace(tmp, dest)
    if file_sha256(dest) != sha:
        raise RuntimeError(f"raw copy of {path.name} does not match its source hash")
    return dest


def _standardize(df: pl.DataFrame, spec: TableSpec) -> pl.DataFrame:
    exprs = []
    for col, dtype in spec.schema.items():
        if dtype == pl.Utf8:
            e = pl.col(col).str.strip_chars()
            if col in spec.standardize_upper:
                e = e.str.to_uppercase()
            if col not in spec.required:
                e = pl.when(e.str.len_chars() == 0).then(None).otherwise(e)
            exprs.append(e.alias(col))
    return df.with_columns(exprs)


def _quarantine_rows(rows: pl.DataFrame, table, batch_id, source_file, ts, stage) -> pl.DataFrame:
    return rows.select(
        pl.lit(table).alias("_table"),
        pl.lit(batch_id).alias("_batch_id"),
        pl.lit(source_file).alias("_source_file"),
        pl.col("_row_number"),
        pl.lit(stage).alias("_stage"),
        pl.col("_reason"),
        pl.col("_row_json"),
        pl.lit(ts, dtype=pl.Datetime("us")).alias("_quarantined_at"),
    )


def _dedup(valid: pl.DataFrame, spec: TableSpec, existing: pl.DataFrame | None):
    """Return (new, duplicates, conflicts) for the valid rows of one batch."""
    key, cols = list(spec.key), spec.columns
    v = valid.sort("_row_number").with_columns(_h=pl.struct(cols).hash())
    v = v.with_columns(
        _occ=pl.int_range(pl.len()).over(key), _first_h=pl.col("_h").first().over(key)
    )
    later = v.filter(pl.col("_occ") > 0)
    dup_in = later.filter(pl.col("_h") == pl.col("_first_h"))
    conf_in = later.filter(pl.col("_h") != pl.col("_first_h")).with_columns(
        _reason=pl.lit("CONFLICTING_DUPLICATE_IN_BATCH")
    )
    cands = v.filter(pl.col("_occ") == 0)
    if existing is not None and existing.height:
        ex = existing.select(*key, pl.struct(cols).hash().alias("_eh"))
        j = cands.join(ex, on=key, how="left")
        new = j.filter(pl.col("_eh").is_null()).drop("_eh")
        dup_ex = j.filter(pl.col("_eh") == pl.col("_h")).drop("_eh")
        conf_ex = (
            j.filter(pl.col("_eh").is_not_null() & (pl.col("_eh") != pl.col("_h")))
            .with_columns(_reason=pl.lit("CONFLICTING_DUPLICATE_OF_EXISTING"))
            .drop("_eh")
        )
    else:
        new, dup_ex, conf_ex = (
            cands,
            cands.clear(),
            cands.clear().with_columns(_reason=pl.lit("", dtype=pl.Utf8)),
        )
    dups = pl.concat([dup_in, dup_ex], how="vertical_relaxed")
    conflicts = pl.concat([conf_in, conf_ex], how="vertical_relaxed")
    return new, dups, conflicts


def _write_partitioned(df: pl.DataFrame, base: Path, pname: str, batch_id: str) -> int:
    n = 0
    for key, part in df.partition_by(pname, as_dict=True, include_key=False).items():
        atomic_write_parquet(part, base / f"{pname}={key[0]}" / f"{batch_id}.parquet")
        n += 1
    return n


def _record(table, path, sha, batch_id, status, ts, **extra) -> dict:
    base = {
        "table": table,
        "source_file": path.name,
        "sha256": sha,
        "batch_id": batch_id,
        "status": status,
        "reason": None,
        "sequence": None,
        "rows_in": 0,
        "rows_rejected": 0,
        "rows_duplicate": 0,
        "rows_conflict": 0,
        "rows_cleaned": 0,
        "ingested_at": ts.isoformat(),
    }
    base.update(extra)
    return base


def ingest_file(path: Path | str, lake_root: Path | str, now: Clock | None = None) -> BatchResult:
    path, lake = Path(path), LakePaths(Path(lake_root))
    table = table_from_filename(path.name)
    spec = SPECS[table]
    lake.cleanup_tmp()
    sha = file_sha256(path)
    batch_id = sha[:16]
    ledger = Ledger.load(lake.ledger_path)
    prior = ledger.find(table, sha)
    if prior is not None and prior["status"] == COMPLETE:
        return BatchResult(table, path.name, batch_id, SKIPPED, rows_in=prior["rows_in"])
    ts = _now(now)

    def reject(reason: str, rows_in: int = 0) -> BatchResult:
        ledger.upsert(
            _record(table, path, sha, batch_id, REJECTED, ts, reason=reason, rows_in=rows_in)
        )
        return BatchResult(table, path.name, batch_id, REJECTED_BATCH, reason, rows_in)

    _land_raw(path, lake, table, batch_id, sha, ts)
    try:
        df = pl.read_parquet(path)
    except Exception as exc:  # unreadable or corrupt file
        return reject(f"UNREADABLE_FILE:{type(exc).__name__}")
    problems = schema_problems(df, spec)
    if problems:
        return reject(";".join(problems), df.height)

    result = validate_frame(df, spec)
    valid = _standardize(result.valid, spec)
    lineage = [
        pl.lit(batch_id).alias("_batch_id"),
        pl.lit(path.name).alias("_source_file"),
        pl.lit(ts, dtype=pl.Datetime("us")).alias("_ingested_at"),
    ]
    existing = (
        read_layer(lake.root, "cleaned", table, exclude_batch=batch_id) if spec.is_fact else None
    )
    new, dups, conflicts = _dedup(valid, spec, existing)

    if df.height != result.rejects.height + valid.height or valid.height != (
        new.height + dups.height + conflicts.height
    ):
        raise RuntimeError("row accounting mismatch; refusing to write")

    validated = valid.with_columns(lineage)
    cleaned = new.select("_row_number", *spec.columns).with_columns(lineage)
    q_validation = _quarantine_rows(result.rejects, table, batch_id, path.name, ts, "VALIDATION")
    q_conflict = _quarantine_rows(
        conflicts.select(
            "_row_number",
            "_reason",
            pl.struct(spec.columns).struct.json_encode().alias("_row_json"),
        ),
        table,
        batch_id,
        path.name,
        ts,
        "DEDUP",
    )
    quarantine = pl.concat([q_validation, q_conflict])

    order = ([spec.partition_source] if spec.is_fact else []) + list(spec.key) + ["_row_number"]
    if spec.is_fact:
        pname, src = spec.partition_name, spec.partition_source
        for frame_name, frame in (("validated", validated), ("cleaned", cleaned)):
            part = frame.with_columns(pl.col(src).dt.date().alias(pname)).sort(order)
            _write_partitioned(part, lake.layer_dir(frame_name) / table, pname, batch_id)
    else:
        for frame_name, frame in (("validated", validated), ("cleaned", cleaned)):
            atomic_write_parquet(
                frame.sort(order), lake.layer_dir(frame_name) / table / f"{batch_id}.parquet"
            )
    qpath = lake.layer_dir("quarantine") / table / f"{batch_id}.parquet"
    if quarantine.height:
        atomic_write_parquet(quarantine, qpath)
    elif qpath.exists():
        qpath.unlink()

    sequence = prior["sequence"] if prior and prior.get("sequence") else ledger.next_sequence()
    ledger.upsert(
        _record(
            table,
            path,
            sha,
            batch_id,
            COMPLETE,
            ts,
            sequence=sequence,
            rows_in=df.height,
            rows_rejected=result.rejects.height,
            rows_duplicate=dups.height,
            rows_conflict=conflicts.height,
            rows_cleaned=new.height,
        )
    )
    if not spec.is_fact:  # superseded dimension snapshots are removed only after the ledger commit
        for old in (lake.layer_dir("cleaned") / table).glob("*.parquet"):
            if old.stem != batch_id:
                old.unlink()
    return BatchResult(
        table,
        path.name,
        batch_id,
        INGESTED,
        None,
        df.height,
        result.rejects.height,
        dups.height,
        conflicts.height,
        new.height,
    )


def discover(source: Path) -> tuple[list[Path], list[str]]:
    """Source files for known tables (dependency order), and ignored top-level parquet files."""
    source = Path(source)
    files: list[Path] = []
    for table in INGEST_ORDER:
        files += sorted(source.glob(f"{table}.parquet")) + sorted(
            source.glob(f"{table}__*.parquet")
        )
    ignored = [p.name for p in sorted(source.glob("*.parquet")) if p not in files]
    return files, ignored


def ingest_source_dir(
    source: Path | str, lake_root: Path | str, now: Clock | None = None
) -> tuple[list[BatchResult], list[str]]:
    files, ignored = discover(Path(source))
    return [ingest_file(f, lake_root, now) for f in files], ignored
