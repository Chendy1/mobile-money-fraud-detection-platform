"""Integration tests: ingestion end to end on generated data, including failure paths."""

import json
import shutil
from datetime import date, datetime

import polars as pl
import pytest

from mmrisk.generation import GeneratorConfig, generate
from mmrisk.ingestion import ingest_file, ingest_source_dir, read_layer, reconcile
from mmrisk.ingestion import pipeline as pipeline_module
from mmrisk.ingestion.cli import main as cli_main
from mmrisk.ingestion.lake import file_sha256
from mmrisk.ingestion.ledger import Ledger
from mmrisk.ingestion.pipeline import INGESTED, REJECTED_BATCH, SKIPPED

CLOCK = lambda: datetime(2025, 7, 1, 12, 0, 0)  # noqa: E731  fixed clock => reproducible output


@pytest.fixture(scope="module")
def source_dir(tmp_path_factory):
    out = tmp_path_factory.mktemp("source")
    cfg = GeneratorConfig(
        num_customers=200,
        num_transactions=6_000,
        fraud_rate=0.02,
        seed=11,
        end_date=date(2025, 3, 1),
        output_dir=out,
    )
    generate(cfg)
    return out


@pytest.fixture
def lake(tmp_path):
    return tmp_path / "lake"


@pytest.fixture
def loaded(source_dir, lake):
    results, ignored = ingest_source_dir(source_dir, lake, CLOCK)
    return lake, results, ignored


def snapshot(root):
    return {
        str(p.relative_to(root)): file_sha256(p) for p in sorted(root.rglob("*")) if p.is_file()
    }


def by_table(results):
    return {r.table: r for r in results}


# ---------- happy path ----------------------------------------------------------------


def test_all_tables_are_ingested_and_reconcile(loaded):
    lake, results, _ = loaded
    assert {r.table for r in results} == {
        "counties", "customers", "merchants", "agents", "devices", "transactions", "labels",
    }  # fmt: skip
    assert all(r.status == INGESTED for r in results)
    assert all(r.rows_rejected == r.rows_duplicate == r.rows_conflict == 0 for r in results)
    assert reconcile(lake) == []


def test_ground_truth_never_enters_the_lake(source_dir, lake, tmp_path):
    src = tmp_path / "src"
    shutil.copytree(source_dir, src)
    truth = pl.read_parquet(src / "ground_truth" / "transaction_truth.parquet")
    truth.write_parquet(src / "transaction_truth.parquet")  # even if it sits at the top level
    results, ignored = ingest_source_dir(src, lake, CLOCK)
    assert "transaction_truth.parquet" in ignored
    rel = [str(p.relative_to(lake)).lower() for p in lake.rglob("*")]
    assert not any("truth" in r or "latent" in r for r in rel)
    for table in ("transactions", "labels"):
        cols = read_layer(lake, "cleaned", table).columns
        assert not any(w in c for c in cols for w in ("true_", "pattern", "episode", "mule"))


def test_cleaned_equals_source_and_is_partitioned_by_event_date(loaded, source_dir):
    lake, _, _ = loaded
    src = pl.read_parquet(source_dir / "transactions.parquet")
    cleaned = read_layer(lake, "cleaned", "transactions")
    assert cleaned.select(src.columns).sort("transaction_id").equals(src.sort("transaction_id"))
    assert (cleaned["event_date"] == cleaned["timestamp"].dt.date()).all()
    dirs = {p.name for p in (lake / "cleaned" / "transactions").iterdir()}
    assert dirs == {f"event_date={d}" for d in src["timestamp"].dt.date().unique()}


def test_labels_are_partitioned_by_availability_date(loaded):
    lake, _, _ = loaded
    labels = read_layer(lake, "cleaned", "labels")
    assert (labels["label_available_date"] == labels["label_available_at"].dt.date()).all()


def test_lineage_columns_trace_back_to_source_rows(loaded, source_dir):
    lake, _, _ = loaded
    src = pl.read_parquet(source_dir / "transactions.parquet")
    cleaned = read_layer(lake, "cleaned", "transactions")
    sample = cleaned.sample(200, seed=3)
    for row in sample.iter_rows(named=True):
        assert src["transaction_id"][row["_row_number"]] == row["transaction_id"]
    assert set(cleaned["_source_file"]) == {"transactions.parquet"}
    assert set(cleaned["_ingested_at"]) == {CLOCK()}
    assert cleaned["_batch_id"].n_unique() == 1


def test_raw_layer_is_a_verbatim_copy(loaded, source_dir):
    lake, _, _ = loaded
    copies = list((lake / "raw" / "transactions").glob("ingest_date=2025-07-01/*.parquet"))
    assert len(copies) == 1
    assert file_sha256(copies[0]) == file_sha256(source_dir / "transactions.parquet")


# ---------- idempotency, reproducibility, recovery --------------------------------------


def test_rerun_is_idempotent(source_dir, loaded):
    lake, _, _ = loaded
    before = snapshot(lake)
    results, _ = ingest_source_dir(source_dir, lake, lambda: datetime(2030, 1, 1))
    assert all(r.status == SKIPPED for r in results)
    assert snapshot(lake) == before


def test_same_input_gives_identical_lakes(source_dir, loaded, tmp_path):
    lake_a, _, _ = loaded
    lake_b = tmp_path / "other_lake"
    ingest_source_dir(source_dir, lake_b, CLOCK)
    assert snapshot(lake_a) == snapshot(lake_b)


def test_lost_ledger_rebuilds_identical_lake(source_dir, loaded):
    lake, _, _ = loaded
    before = snapshot(lake)
    (lake / "_meta" / "ledger.json").unlink()
    results, _ = ingest_source_dir(source_dir, lake, CLOCK)
    assert all(r.status == INGESTED for r in results)
    assert snapshot(lake) == before
    assert reconcile(lake) == []


def test_crash_mid_batch_is_recoverable_and_invisible(source_dir, lake, tmp_path, monkeypatch):
    real_write = pipeline_module.atomic_write_parquet
    calls = {"cleaned": 0}

    def flaky(df, path):
        if "cleaned" in path.parts and "transactions" in path.parts:
            calls["cleaned"] += 1
            if calls["cleaned"] == 3:
                raise OSError("disk full (simulated)")
        return real_write(df, path)

    monkeypatch.setattr(pipeline_module, "atomic_write_parquet", flaky)
    with pytest.raises(OSError, match="simulated"):
        ingest_source_dir(source_dir, lake, CLOCK)
    # the unfinished batch is not COMPLETE, so readers cannot see partial data
    done = {b["table"] for b in Ledger.load(lake / "_meta" / "ledger.json").complete()}
    assert "transactions" not in done
    assert read_layer(lake, "cleaned", "transactions").height == 0

    monkeypatch.setattr(pipeline_module, "atomic_write_parquet", real_write)
    ingest_source_dir(source_dir, lake, CLOCK)
    reference = tmp_path / "reference"
    ingest_source_dir(source_dir, reference, CLOCK)
    assert snapshot(lake) == snapshot(reference)
    assert reconcile(lake) == []


def test_leftover_tmp_files_are_cleaned_up(source_dir, lake):
    stale = lake / "cleaned" / "agents" / "half_written.parquet.tmp"
    stale.parent.mkdir(parents=True)
    stale.write_bytes(b"garbage")
    ingest_file(source_dir / "agents.parquet", lake, CLOCK)
    assert not stale.exists()


# ---------- bad batches -----------------------------------------------------------------


def make_overlap_batch(source_dir, path):
    src = pl.read_parquet(source_dir / "transactions.parquet").sample(
        fraction=1.0, shuffle=True, seed=1
    )
    as_text = lambda df: df.with_columns(pl.col("amount_kes").cast(pl.Utf8))  # noqa: E731
    dupes = src[:100]
    conflicts = src[100:120].with_columns(pl.col("amount_kes") + 1)
    new = src[200:230].with_columns(
        pl.format("TXN9{}", pl.int_range(pl.len()).cast(pl.Utf8).str.zfill(8)).alias(
            "transaction_id"
        )
    )
    null_key = src[300:305].with_columns(pl.lit(None, dtype=pl.Utf8).alias("transaction_id"))
    bad_amount = as_text(src[400:403]).with_columns(pl.lit("abc").alias("amount_kes"))
    pl.concat([as_text(x) for x in (dupes, conflicts, new, null_key)] + [bad_amount]).write_parquet(
        path
    )
    return src


def test_overlapping_batch_dedups_and_quarantines(source_dir, loaded, tmp_path):
    lake, _, _ = loaded
    batch = tmp_path / "transactions__overlap.parquet"
    src = make_overlap_batch(source_dir, batch)
    result = ingest_file(batch, lake, CLOCK)
    assert result.status == INGESTED
    assert (result.rows_in, result.rows_rejected, result.rows_duplicate) == (158, 8, 100)
    assert (result.rows_conflict, result.rows_cleaned) == (20, 30)

    cleaned = read_layer(lake, "cleaned", "transactions")
    assert cleaned.height == 6_000 + 30
    assert cleaned["transaction_id"].is_unique().all()
    original = dict(zip(src["transaction_id"], src["amount_kes"], strict=True))
    conflict_ids = src[100:120]["transaction_id"].to_list()
    kept = cleaned.filter(pl.col("transaction_id").is_in(conflict_ids))
    assert all(
        a == original[i] for i, a in zip(kept["transaction_id"], kept["amount_kes"], strict=True)
    )

    q = read_layer(lake, "quarantine", "transactions")
    assert q.height == 28
    assert q.filter(pl.col("_stage") == "DEDUP").height == 20
    reasons = q.group_by("_reason").len().sort("_reason")
    assert dict(reasons.iter_rows()) == {
        "CAST_FAILED:amount_kes": 3,
        "CONFLICTING_DUPLICATE_OF_EXISTING": 20,
        "NULL_REQUIRED:transaction_id": 5,
    }
    assert "abc" in " ".join(q.filter(pl.col("_reason") == "CAST_FAILED:amount_kes")["_row_json"])
    assert reconcile(lake) == []


def test_replayed_file_under_a_new_name_is_skipped(source_dir, loaded, tmp_path):
    lake, _, _ = loaded
    before = snapshot(lake)
    copy = tmp_path / "transactions__replay.parquet"
    shutil.copyfile(source_dir / "transactions.parquet", copy)
    assert ingest_file(copy, lake, CLOCK).status == SKIPPED
    assert snapshot(lake) == before


@pytest.mark.parametrize(
    ("mutate", "expected"),
    [
        (lambda df: df.drop("channel"), "MISSING_COLUMNS:channel"),
        (lambda df: df.with_columns(pl.lit(1).alias("surprise")), "UNEXPECTED_COLUMNS:surprise"),
    ],
)
def test_schema_violations_reject_the_whole_batch(source_dir, lake, tmp_path, mutate, expected):
    bad = tmp_path / "transactions__bad.parquet"
    mutate(pl.read_parquet(source_dir / "transactions.parquet")).write_parquet(bad)
    result = ingest_file(bad, lake, CLOCK)
    assert result.status == REJECTED_BATCH and expected in result.reason
    assert read_layer(lake, "cleaned", "transactions").height == 0
    assert len(list((lake / "raw" / "transactions").rglob("*.parquet"))) == 1  # audit trail kept
    ingest_file(bad, lake, CLOCK)  # retrying must not create a second raw copy
    assert len(list((lake / "raw" / "transactions").rglob("*.parquet"))) == 1
    ledger = Ledger.load(lake / "_meta" / "ledger.json")
    assert [b["status"] for b in ledger.batches] == ["REJECTED"]


def test_unreadable_file_is_rejected_without_crashing(lake, tmp_path):
    bad = tmp_path / "customers.parquet"
    bad.write_bytes(b"this is not a parquet file")
    result = ingest_file(bad, lake, CLOCK)
    assert result.status == REJECTED_BATCH and result.reason.startswith("UNREADABLE_FILE")
    assert read_layer(lake, "cleaned", "customers").height == 0


def test_corrected_batch_succeeds_after_rejection(source_dir, lake, tmp_path):
    good = pl.read_parquet(source_dir / "customers.parquet")
    bad = tmp_path / "customers__bad.parquet"
    good.drop("segment").write_parquet(bad)
    assert ingest_file(bad, lake, CLOCK).status == REJECTED_BATCH
    assert ingest_file(source_dir / "customers.parquet", lake, CLOCK).status == INGESTED
    assert read_layer(lake, "cleaned", "customers").height == 200


# ---------- dimensions ------------------------------------------------------------------


def test_new_dimension_snapshot_replaces_the_old_one(source_dir, lake, tmp_path):
    ingest_file(source_dir / "counties.parquet", lake, CLOCK)
    counties = pl.read_parquet(source_dir / "counties.parquet")
    extra = pl.DataFrame({"county": ["Test County"], "latitude": [0.0], "longitude": [37.0]})
    v2 = tmp_path / "counties__v2.parquet"
    pl.concat([counties, extra]).write_parquet(v2)
    ingest_file(v2, lake, CLOCK)
    assert read_layer(lake, "cleaned", "counties").height == 13
    assert read_layer(lake, "validated", "counties").height == 12 + 13  # history is kept
    assert len(list((lake / "cleaned" / "counties").glob("*.parquet"))) == 1
    assert reconcile(lake) == []


def test_conflicting_duplicate_keys_inside_a_snapshot_are_quarantined(source_dir, lake, tmp_path):
    counties = pl.read_parquet(source_dir / "counties.parquet")
    dup = counties[:1].with_columns(pl.col("latitude") + 1.0)
    batch = tmp_path / "counties.parquet"
    pl.concat([counties, dup]).write_parquet(batch)
    result = ingest_file(batch, lake, CLOCK)
    assert (result.rows_cleaned, result.rows_conflict) == (12, 1)
    assert read_layer(lake, "quarantine", "counties").height == 1


# ---------- reconciliation detects damage -------------------------------------------------


def test_reconcile_detects_a_deleted_partition(loaded):
    lake, _, _ = loaded
    victim = next((lake / "cleaned" / "transactions").rglob("*.parquet"))
    victim.unlink()
    assert any("cleaned" in p for p in reconcile(lake))


def test_reconcile_detects_a_tampered_raw_file(loaded):
    lake, _, _ = loaded
    raw = next((lake / "raw" / "transactions").rglob("*.parquet"))
    raw.write_bytes(raw.read_bytes() + b"x")
    assert any("hash mismatch" in p for p in reconcile(lake))


# ---------- CLI -----------------------------------------------------------------------


def test_cli_run_status_and_exit_codes(source_dir, lake, capsys):
    assert cli_main(["run", "--source", str(source_dir), "--lake", str(lake)]) == 0
    out = capsys.readouterr().out
    assert "reconciliation: OK" in out and "INGESTED" in out
    assert "manifest" not in out
    assert cli_main(["run", "--source", str(source_dir), "--lake", str(lake)]) == 0
    assert "SKIPPED_DUPLICATE_BATCH" in capsys.readouterr().out
    assert cli_main(["status", "--lake", str(lake)]) == 0
    assert "7/7 batches COMPLETE" in capsys.readouterr().out


def test_cli_reports_missing_source_and_rejections(source_dir, lake, tmp_path, capsys):
    assert cli_main(["run", "--source", str(tmp_path / "nope"), "--lake", str(lake)]) == 2
    assert "not found" in capsys.readouterr().err
    src = tmp_path / "src"
    src.mkdir()
    pl.read_parquet(source_dir / "agents.parquet").drop("county").write_parquet(
        src / "agents.parquet"
    )
    assert cli_main(["run", "--source", str(src), "--lake", str(lake)]) == 1
    assert "MISSING_COLUMNS:county" in capsys.readouterr().out
    assert cli_main(["status", "--lake", str(tmp_path / "empty")]) == 2


def test_ledger_is_valid_json_with_counts(loaded):
    lake, _, _ = loaded
    data = json.loads((lake / "_meta" / "ledger.json").read_text())
    txn = next(b for b in data["batches"] if b["table"] == "transactions")
    assert txn["status"] == "COMPLETE" and txn["rows_in"] == txn["rows_cleaned"] == 6_000
