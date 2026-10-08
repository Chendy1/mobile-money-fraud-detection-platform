"""Table specifications: expected schema, keys, required fields and partitioning."""

from __future__ import annotations

from dataclasses import dataclass, field

import polars as pl

U = pl.Utf8


@dataclass(frozen=True)
class TableSpec:
    name: str
    schema: dict[str, pl.DataType] = field(hash=False)
    key: tuple[str, ...] = ()
    required: tuple[str, ...] = ()
    partition_source: str | None = None  # timestamp column the partition date is derived from
    partition_name: str | None = None  # e.g. "event_date"
    standardize_upper: tuple[str, ...] = ()

    @property
    def columns(self) -> list[str]:
        return list(self.schema)

    @property
    def is_fact(self) -> bool:
        return self.partition_name is not None


SPECS: dict[str, TableSpec] = {
    "counties": TableSpec(
        "counties",
        {"county": U, "latitude": pl.Float64, "longitude": pl.Float64},
        key=("county",),
        required=("county", "latitude", "longitude"),
    ),
    "customers": TableSpec(
        "customers",
        {"customer_id": U, "segment": U, "home_county": U, "registration_date": pl.Date},
        key=("customer_id",),
        required=("customer_id", "segment", "home_county", "registration_date"),
        standardize_upper=("customer_id", "segment"),
    ),
    "merchants": TableSpec(
        "merchants",
        {"merchant_id": U, "merchant_type": U, "category": U, "county": U},
        key=("merchant_id",),
        required=("merchant_id", "merchant_type", "category", "county"),
        standardize_upper=("merchant_id", "merchant_type", "category"),
    ),
    "agents": TableSpec(
        "agents",
        {"agent_id": U, "county": U},
        key=("agent_id",),
        required=("agent_id", "county"),
        standardize_upper=("agent_id",),
    ),
    "devices": TableSpec(
        "devices",
        {"device_id": U, "device_type": U},
        key=("device_id",),
        required=("device_id", "device_type"),
        standardize_upper=("device_id", "device_type"),
    ),
    "transactions": TableSpec(
        "transactions",
        {
            "transaction_id": U,
            "customer_id": U,
            "timestamp": pl.Datetime("us"),
            "transaction_type": U,
            "amount_kes": pl.Int64,
            "counterparty_type": U,
            "counterparty_id": U,
            "device_id": U,
            "location_county": U,
            "channel": U,
        },
        key=("transaction_id",),
        required=(
            "transaction_id",
            "customer_id",
            "timestamp",
            "transaction_type",
            "amount_kes",
            "device_id",
            "location_county",
            "channel",
        ),
        partition_source="timestamp",
        partition_name="event_date",
        standardize_upper=(
            "transaction_id",
            "customer_id",
            "transaction_type",
            "counterparty_type",
            "counterparty_id",
            "device_id",
            "channel",
        ),
    ),
    "labels": TableSpec(
        "labels",
        {
            "transaction_id": U,
            "label_is_fraud": pl.Int8,
            "label_available_at": pl.Datetime("us"),
        },
        key=("transaction_id",),
        required=("transaction_id", "label_is_fraud", "label_available_at"),
        partition_source="label_available_at",
        partition_name="label_available_date",
        standardize_upper=("transaction_id",),
    ),
}

# Dimensions first, then facts (so later phases can check referential integrity).
INGEST_ORDER = ["counties", "customers", "merchants", "agents", "devices", "transactions", "labels"]

LINEAGE_COLUMNS = ["_batch_id", "_source_file", "_row_number", "_ingested_at"]
QUARANTINE_SCHEMA = {
    "_table": pl.Utf8,
    "_batch_id": pl.Utf8,
    "_source_file": pl.Utf8,
    "_row_number": pl.UInt32,
    "_stage": pl.Utf8,
    "_reason": pl.Utf8,
    "_row_json": pl.Utf8,
    "_quarantined_at": pl.Datetime("us"),
}


def table_from_filename(name: str) -> str:
    """`transactions.parquet` or `transactions__late.parquet` -> `transactions`."""
    stem = name[: -len(".parquet")] if name.endswith(".parquet") else name
    table = stem.split("__", 1)[0]
    if table not in SPECS:
        raise ValueError(f"unknown table for file {name!r}; known tables: {sorted(SPECS)}")
    return table
