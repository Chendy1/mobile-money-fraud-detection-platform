"""Read lake layers. Only batches marked COMPLETE in the ledger are visible."""

from __future__ import annotations

from pathlib import Path

import polars as pl

from mmrisk.ingestion.lake import LakePaths
from mmrisk.ingestion.ledger import Ledger
from mmrisk.ingestion.specs import QUARANTINE_SCHEMA, SPECS


def empty_layer(table: str, layer: str) -> pl.DataFrame:
    if layer == "quarantine":
        return pl.DataFrame(schema=QUARANTINE_SCHEMA)
    spec = SPECS[table]
    schema = dict(spec.schema)
    if layer in ("validated", "cleaned"):
        schema.update(
            {
                "_batch_id": pl.Utf8,
                "_source_file": pl.Utf8,
                "_row_number": pl.UInt32,
                "_ingested_at": pl.Datetime("us"),
            }
        )
        if spec.is_fact:
            schema[spec.partition_name] = pl.Date
    return pl.DataFrame(schema=schema)


def visible_batch_ids(root: Path, layer: str, table: str) -> set[str]:
    """Batch ids that make up a layer. Dimension 'cleaned' is the latest snapshot only."""
    complete = Ledger.load(LakePaths(Path(root)).ledger_path).complete(table)
    if not SPECS[table].is_fact and layer == "cleaned" and complete:
        return {max(complete, key=lambda b: b["sequence"])["batch_id"]}
    return {b["batch_id"] for b in complete}


def layer_files(root: Path, layer: str, table: str, exclude_batch: str | None = None) -> list[Path]:
    """Parquet files that make up a layer for a table (COMPLETE batches only), sorted."""
    base = LakePaths(Path(root)).layer_dir(layer) / table
    visible = visible_batch_ids(root, layer, table) - {exclude_batch}
    if not base.exists():
        return []
    return [f for f in sorted(base.rglob("*.parquet")) if f.stem in visible]


def read_layer(
    root: Path, layer: str, table: str, exclude_batch: str | None = None
) -> pl.DataFrame:
    """Read a layer for a table; Hive-style `name=value` directories become columns."""
    base = LakePaths(Path(root)).layer_dir(layer) / table
    frames = []
    for f in layer_files(root, layer, table, exclude_batch):
        df = pl.read_parquet(f)
        for part in f.relative_to(base).parts[:-1]:
            name, _, value = part.partition("=")
            df = df.with_columns(pl.lit(value).str.to_date().alias(name))
        frames.append(df)
    if not frames:
        return empty_layer(table, layer)
    return pl.concat(frames, how="vertical_relaxed")
