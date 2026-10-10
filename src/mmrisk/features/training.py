"""Training table: features joined to the labels that were known at a chosen moment.

Features never depend on `as_of`; only which rows have a label does. A transaction is
labelled at time T only if its label had become available by T. This keeps two mistakes out:
using labels from the future, and treating recent unlabelled fraud as legitimate.
"""

from __future__ import annotations

from datetime import datetime

import polars as pl

from mmrisk.features.registry import KEY_COLUMNS, feature_names

LABEL_COLUMNS = ("transaction_id", "label_is_fraud", "label_available_at")


def _check(features: pl.DataFrame, labels: pl.DataFrame) -> None:
    missing = [c for c in LABEL_COLUMNS if c not in labels.columns]
    if missing:
        raise ValueError(f"labels is missing columns: {', '.join(missing)}")
    leaked = [c for c in ("label_is_fraud", "label_available_at") if c in features.columns]
    if leaked:
        raise ValueError(f"features must not contain label columns: {', '.join(leaked)}")


def training_table(features: pl.DataFrame, labels: pl.DataFrame, as_of: datetime) -> pl.DataFrame:
    """Rows whose label was known at `as_of`, with features unchanged."""
    _check(features, labels)
    lab = labels.select(LABEL_COLUMNS)
    return (
        features.join(lab, on="transaction_id", how="inner")
        .filter((pl.col("label_available_at") <= as_of) & (pl.col("timestamp") <= as_of))
        .select(*KEY_COLUMNS, *feature_names(), "label_is_fraud", "label_available_at")
        .sort("timestamp", "transaction_id")
    )


def label_status(features: pl.DataFrame, labels: pl.DataFrame, as_of: datetime) -> dict:
    """How many transactions up to `as_of` have a known label, and how many do not yet."""
    _check(features, labels)
    seen = features.filter(pl.col("timestamp") <= as_of).select("transaction_id")
    known = seen.join(
        labels.filter(pl.col("label_available_at") <= as_of).select("transaction_id"),
        on="transaction_id",
        how="inner",
    )
    return {
        "transactions": seen.height,
        "labelled": known.height,
        "unlabelled": seen.height - known.height,
    }
