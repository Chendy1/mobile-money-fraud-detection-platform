"""Unit tests for ingestion building blocks."""

from datetime import datetime

import polars as pl
import pytest

from mmrisk.ingestion.lake import atomic_write_parquet, file_sha256
from mmrisk.ingestion.ledger import COMPLETE, Ledger
from mmrisk.ingestion.pipeline import _dedup, _standardize
from mmrisk.ingestion.specs import SPECS, table_from_filename
from mmrisk.ingestion.validate import schema_problems, validate_frame


def test_specs_are_internally_consistent():
    for spec in SPECS.values():
        assert set(spec.key) <= set(spec.schema)
        assert set(spec.required) <= set(spec.schema)
        assert set(spec.key) <= set(spec.required)
        assert set(spec.standardize_upper) <= set(spec.schema)
        if spec.is_fact:
            assert spec.partition_source in spec.schema


@pytest.mark.parametrize(
    ("name", "table"),
    [
        ("transactions.parquet", "transactions"),
        ("transactions__late_batch.parquet", "transactions"),
        ("labels__2025-02.parquet", "labels"),
    ],
)
def test_table_from_filename(name, table):
    assert table_from_filename(name) == table


def test_unknown_table_name_is_rejected():
    with pytest.raises(ValueError, match="unknown table"):
        table_from_filename("ground_truth.parquet")


def test_schema_problems_reports_missing_and_extra():
    spec = SPECS["agents"]
    assert schema_problems(pl.DataFrame({"agent_id": ["A1"], "county": ["X"]}), spec) == []
    problems = schema_problems(pl.DataFrame({"agent_id": ["A1"], "surprise": [1]}), spec)
    assert any(p.startswith("MISSING_COLUMNS:county") for p in problems)
    assert any(p.startswith("UNEXPECTED_COLUMNS:surprise") for p in problems)


def test_validate_frame_flags_each_failure_type():
    spec = SPECS["labels"]
    df = pl.DataFrame(
        {
            "transaction_id": ["T1", None, "  ", "T4", "T5"],
            "label_is_fraud": [0, 1, 1, 300, 1],  # 300 overflows Int8
            "label_available_at": [
                "2025-01-02 00:00:00",
                "2025-01-02 00:00:00",
                "2025-01-02 00:00:00",
                "2025-01-02 00:00:00",
                "not a timestamp",
            ],
        }
    )
    out = validate_frame(df, spec)
    assert out.valid["transaction_id"].to_list() == ["T1"]
    reasons = dict(zip(out.rejects["_row_number"], out.rejects["_reason"], strict=True))
    assert reasons == {
        1: "NULL_REQUIRED:transaction_id",
        2: "EMPTY_REQUIRED:transaction_id",
        3: "CAST_FAILED:label_is_fraud",
        4: "CAST_FAILED:label_available_at",
    }
    assert out.valid.schema["label_is_fraud"] == pl.Int8
    assert out.valid.schema["label_available_at"] == pl.Datetime("us")


def test_validate_frame_does_not_truncate_fractional_amounts():
    spec = SPECS["transactions"]
    row = {
        "transaction_id": "T1",
        "customer_id": "C1",
        "timestamp": datetime(2025, 1, 1, 10, 0),
        "transaction_type": "TILL",
        "amount_kes": 100.5,
        "counterparty_type": None,
        "counterparty_id": None,
        "device_id": "D1",
        "location_county": "Nairobi",
        "channel": "APP",
    }
    ok = {**row, "transaction_id": "T2", "amount_kes": 100.0}
    out = validate_frame(pl.DataFrame([row, ok]), spec)
    assert out.valid["transaction_id"].to_list() == ["T2"]
    assert out.rejects["_reason"].to_list() == ["CAST_FAILED:amount_kes"]
    assert "100.5" in out.rejects["_row_json"][0]


def test_standardize_trims_uppercases_and_nulls_blank_optionals():
    spec = SPECS["transactions"]
    df = pl.DataFrame(
        {
            "transaction_id": [" txn1 "],
            "customer_id": ["c1"],
            "timestamp": [datetime(2025, 1, 1)],
            "transaction_type": ["till "],
            "amount_kes": [5],
            "counterparty_type": ["  "],
            "counterparty_id": [""],
            "device_id": ["d1"],
            "location_county": [" Nairobi "],
            "channel": ["app"],
        },
        schema=spec.schema,
    )
    out = _standardize(df, spec).row(0, named=True)
    assert out["transaction_id"] == "TXN1"
    assert out["transaction_type"] == "TILL"
    assert out["counterparty_type"] is None
    assert out["counterparty_id"] is None
    assert out["location_county"] == "Nairobi"  # trimmed but case preserved
    assert out["channel"] == "APP"


def _labels(rows):
    df = pl.DataFrame(
        rows,
        schema={
            "transaction_id": pl.Utf8,
            "label_is_fraud": pl.Int8,
            "label_available_at": pl.Datetime("us"),
        },
        orient="row",
    )
    return df.with_row_index("_row_number")


def test_dedup_separates_new_duplicates_and_conflicts():
    spec = SPECS["labels"]
    t = datetime(2025, 1, 5)
    existing = _labels([("T1", 1, t), ("T2", 0, t)]).drop("_row_number")
    batch = _labels(
        [
            ("T1", 1, t),  # identical to existing -> duplicate
            ("T2", 1, t),  # differs from existing -> conflict
            ("T3", 0, t),  # new
            ("T3", 0, t),  # repeated inside the batch -> duplicate
            ("T4", 0, t),
            ("T4", 1, t),  # conflicting repeat inside the batch -> conflict
        ]
    )
    new, dups, conflicts = _dedup(batch, spec, existing)
    assert sorted(new["transaction_id"].to_list()) == ["T3", "T4"]
    assert dups.height == 2
    assert sorted(conflicts["transaction_id"].to_list()) == ["T2", "T4"]
    assert set(conflicts["_reason"]) == {
        "CONFLICTING_DUPLICATE_OF_EXISTING",
        "CONFLICTING_DUPLICATE_IN_BATCH",
    }


def test_atomic_write_overwrites_and_leaves_no_tmp(tmp_path):
    target = tmp_path / "a" / "b.parquet"
    atomic_write_parquet(pl.DataFrame({"x": [1]}), target)
    first = file_sha256(target)
    atomic_write_parquet(pl.DataFrame({"x": [2]}), target)
    assert file_sha256(target) != first
    assert pl.read_parquet(target)["x"].to_list() == [2]
    assert not list(tmp_path.rglob("*.tmp"))


def test_ledger_roundtrip_and_sequence(tmp_path):
    path = tmp_path / "ledger.json"
    ledger = Ledger.load(path)
    assert ledger.batches == [] and ledger.next_sequence() == 1
    ledger.upsert({"table": "t", "sha256": "s1", "status": COMPLETE, "sequence": 1})
    ledger.upsert({"table": "t", "sha256": "s1", "status": COMPLETE, "sequence": 1, "x": 1})
    reloaded = Ledger.load(path)
    assert len(reloaded.batches) == 1  # upsert replaces, never duplicates
    assert reloaded.find("t", "s1")["x"] == 1
    assert reloaded.next_sequence() == 2
