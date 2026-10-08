"""Cross-layer reconciliation: do the files on disk agree with the ledger?"""

from __future__ import annotations

from pathlib import Path

import polars as pl

from mmrisk.ingestion.lake import LakePaths, file_sha256
from mmrisk.ingestion.ledger import Ledger
from mmrisk.ingestion.reader import read_layer, visible_batch_ids
from mmrisk.ingestion.specs import SPECS


def _counts(df: pl.DataFrame) -> dict[str, int]:
    if df.height == 0:
        return {}
    return dict(df.group_by("_batch_id").len().iter_rows())


def reconcile(lake_root: Path | str) -> list[str]:
    """Return a list of problems (empty means the lake is consistent)."""
    root = Path(lake_root)
    lake = LakePaths(root)
    ledger = Ledger.load(lake.ledger_path)
    problems: list[str] = []
    for table in sorted({b["table"] for b in ledger.complete()}):
        spec = SPECS[table]
        validated = _counts(read_layer(root, "validated", table))
        cleaned = _counts(read_layer(root, "cleaned", table))
        quarantined = _counts(read_layer(root, "quarantine", table))
        cleaned_ids = visible_batch_ids(root, "cleaned", table)
        for b in ledger.complete(table):
            bid = b["batch_id"]
            tag = f"{table}/{bid}"
            raw = list((lake.raw / table).glob(f"ingest_date=*/{bid}.parquet"))
            if len(raw) != 1:
                problems.append(f"{tag}: expected exactly one raw copy, found {len(raw)}")
            elif file_sha256(raw[0]) != b["sha256"]:
                problems.append(f"{tag}: raw copy was modified (hash mismatch)")
            if (
                b["rows_in"]
                != b["rows_rejected"] + b["rows_duplicate"] + b["rows_conflict"] + b["rows_cleaned"]
            ):
                problems.append(f"{tag}: ledger row counts do not add up")
            expected_valid = b["rows_in"] - b["rows_rejected"]
            if validated.get(bid, 0) != expected_valid:
                problems.append(
                    f"{tag}: validated has {validated.get(bid, 0)} rows, expected {expected_valid}"
                )
            expected_q = b["rows_rejected"] + b["rows_conflict"]
            if quarantined.get(bid, 0) != expected_q:
                problems.append(
                    f"{tag}: quarantine has {quarantined.get(bid, 0)} rows, expected {expected_q}"
                )
            if bid in cleaned_ids and cleaned.get(bid, 0) != b["rows_cleaned"]:
                problems.append(
                    f"{tag}: cleaned has {cleaned.get(bid, 0)} rows, expected {b['rows_cleaned']}"
                )
        if spec.is_fact:
            keys = read_layer(root, "cleaned", table).select(list(spec.key))
            if keys.is_duplicated().any():
                problems.append(f"{table}: duplicate keys in cleaned layer")
    return problems
