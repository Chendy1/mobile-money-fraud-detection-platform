"""Feature integration tests: leakage, offline/online parity, cross-check against the EDA SQL."""

import json
import random
from datetime import date, datetime, timedelta
from pathlib import Path

import polars as pl
import pytest

from mmrisk.features import (
    FEATURES,
    OnlineFeatureState,
    OutOfOrderError,
    compute_features,
    feature_names,
    training_table,
)
from mmrisk.features.audit import audit
from mmrisk.features.catalog import render
from mmrisk.features.cli import main as features_cli
from mmrisk.features.training import label_status
from mmrisk.generation import GeneratorConfig, generate
from mmrisk.generation.config import DISPUTE_WINDOW_DAYS
from mmrisk.ingestion import ingest_source_dir, read_layer

CLOCK = lambda: datetime(2025, 9, 1, 12, 0, 0)  # noqa: E731
CUSTOMER_ONLY = [f.name for f in FEATURES if f.history in ("none", "customer")]


@pytest.fixture(scope="module")
def lake(tmp_path_factory):
    root = tmp_path_factory.mktemp("feat")
    src, lk = root / "src", root / "lake"
    generate(
        GeneratorConfig(
            num_customers=300,
            num_transactions=12_000,
            fraud_rate=0.03,
            seed=21,
            end_date=date(2025, 6, 30),
            output_dir=src,
        )
    )
    ingest_source_dir(src, lk, CLOCK)
    return lk


@pytest.fixture(scope="module")
def tables(lake):
    return {t: read_layer(lake, "cleaned", t) for t in ("transactions", "customers", "labels")}


@pytest.fixture(scope="module")
def features(tables):
    return compute_features(tables["transactions"], tables["customers"])


def test_one_row_per_transaction_in_time_order(tables, features):
    assert features.height == tables["transactions"].height
    assert features["transaction_id"].n_unique() == features.height
    keys = features.select("timestamp", "transaction_id")
    assert keys.equals(keys.sort("timestamp", "transaction_id"))


def test_global_time_cut_does_not_change_earlier_rows(tables, features):
    """Every feature (including inbound transfers) ignores rows that come later in time."""
    t, c = tables["transactions"], tables["customers"]
    cut = t["timestamp"].sort()[t.height // 2]
    early = compute_features(t.filter(pl.col("timestamp") <= cut), c)
    expected = features.filter(pl.col("timestamp") <= cut)
    assert early.height == expected.height > 1000
    assert early.equals(expected)


def test_dropping_each_customers_future_does_not_change_their_earlier_rows(tables, features):
    t, c = tables["transactions"], tables["customers"]
    ranked = t.sort("timestamp", "transaction_id").with_columns(
        rank=pl.int_range(pl.len()).over("customer_id"), n=pl.len().over("customer_id")
    )
    kept = ranked.filter(pl.col("rank") < pl.col("n") * 0.5).drop("rank", "n")
    early = compute_features(kept, c).select("transaction_id", *CUSTOMER_ONLY)
    expected = features.join(kept.select("transaction_id"), on="transaction_id", how="semi")
    assert early.height == expected.height > 1000
    assert early.equals(expected.select("transaction_id", *CUSTOMER_ONLY))


def test_row_order_of_input_does_not_matter(tables, features):
    shuffled = tables["transactions"].sample(fraction=1.0, shuffle=True, seed=3)
    assert compute_features(shuffled, tables["customers"]).equals(features)


def test_labels_cannot_influence_features(tables, features):
    """compute_features has no label input; an extra label column on its input is ignored."""
    t = tables["transactions"].with_columns(label_is_fraud=pl.lit(1))
    assert compute_features(t, tables["customers"]).equals(features)


def test_offline_and_online_features_are_identical(tables, features):
    """Stream the transactions through the online path; every sampled row must match the batch."""
    state = OnlineFeatureState(tables["customers"])
    stream = tables["transactions"].sort("timestamp", "transaction_id").to_dicts()
    expected = {r["transaction_id"]: r for r in features.to_dicts()}
    sample = set(random.Random(1).sample(range(len(stream)), 500))
    checked = 0
    for i, row in enumerate(stream):
        if i in sample:
            got = state.features(row)
            want = expected[row["transaction_id"]]
            for name in feature_names():
                if isinstance(want[name], float):
                    assert got[name] == pytest.approx(want[name]), (row["transaction_id"], name)
                else:
                    assert got[name] == want[name], (row["transaction_id"], name)
            checked += 1
        state.observe(row)
    assert checked == 500


def test_online_state_rejects_out_of_order_and_unknown_customers(tables):
    state = OnlineFeatureState(tables["customers"])
    rows = tables["transactions"].sort("timestamp", "transaction_id").head(50).to_dicts()
    first = rows[0]
    state.observe(first)
    with pytest.raises(OutOfOrderError):
        state.observe(first)
    with pytest.raises(OutOfOrderError):
        state.features(first)
    with pytest.raises(KeyError):
        state.features({**rows[1], "customer_id": "C999999"})


def test_matches_the_independent_duckdb_definitions_from_phase_5(lake, features):
    """Two implementations (Polars here, DuckDB SQL in the EDA) must agree on shared features."""
    pytest.importorskip("duckdb")
    from mmrisk.eda.data import Analysis

    an = Analysis.from_lake(lake)
    sql = an.query(
        "SELECT transaction_id, n_10min, n_1h, n_24h, secs_since_prev, f_night, f_new_device, "
        "f_new_recipient, f_location_jump, f_cashout_after_inbound FROM a ORDER BY transaction_id"
    )
    ours = features.sort("transaction_id")
    assert sql["transaction_id"].to_list() == ours["transaction_id"].to_list()
    pairs = {
        "n_10min": "txn_count_10m", "n_1h": "txn_count_1h", "n_24h": "txn_count_24h",
        "f_new_device": "is_new_device", "f_new_recipient": "is_new_recipient",
        "f_location_jump": "location_jump_1h", "f_cashout_after_inbound": "cashout_after_inbound",
        "f_night": "is_night",
    }  # fmt: skip
    for sql_col, our_col in pairs.items():
        assert sql[sql_col].cast(pl.Int64).to_list() == ours[our_col].cast(pl.Int64).to_list(), (
            our_col
        )
    a = sql["secs_since_prev"].cast(pl.Float64)
    b = ours["secs_since_prev_txn"]
    assert a.is_null().to_list() == b.is_null().to_list()
    assert (a.fill_null(0) - b.fill_null(0)).abs().max() < 1e-6


def test_training_table_only_contains_labels_known_at_the_chosen_time(tables, features):
    labels = tables["labels"]
    last = features["timestamp"].max()
    early = last - timedelta(days=120)
    late = last - timedelta(days=30)
    a = training_table(features, labels, early)
    b = training_table(features, labels, late)
    assert a.height < b.height
    assert (a["label_available_at"] <= early).all() and (b["label_available_at"] <= late).all()
    # Legitimate labels only appear after the dispute window, so recent rows are never "clean".
    recent = b.filter(pl.col("timestamp") > late - timedelta(days=DISPUTE_WINDOW_DAYS))
    assert recent.filter(pl.col("label_is_fraud") == 0).is_empty()
    # Fraud already reported is visible even for recent rows.
    assert recent.filter(pl.col("label_is_fraud") == 1).height > 0


def test_features_do_not_change_with_the_label_time(tables, features):
    labels = tables["labels"]
    last = features["timestamp"].max()
    a = training_table(features, labels, last - timedelta(days=90))
    b = training_table(features, labels, last - timedelta(days=20))
    shared = a.join(b, on="transaction_id", suffix="_b")
    assert shared.height == a.height
    for name in feature_names():
        assert shared[name].equals(shared[f"{name}_b"].alias(name)), name


def test_fraud_labels_hidden_at_an_early_time_are_not_in_the_table(tables, features):
    labels = tables["labels"]
    as_of = features["timestamp"].max() - timedelta(days=60)
    table = training_table(features, labels, as_of)
    status = label_status(features, labels, as_of)
    assert status["labelled"] == table.height
    assert status["unlabelled"] > 0
    hidden = labels.filter(pl.col("label_available_at") > as_of)
    assert table.join(hidden, on="transaction_id", how="inner").is_empty()


def test_training_table_rejects_features_that_already_contain_labels(tables, features):
    with pytest.raises(ValueError, match="label"):
        training_table(
            features.with_columns(label_is_fraud=pl.lit(0)), tables["labels"], datetime(2025, 6, 1)
        )
    with pytest.raises(ValueError, match="missing"):
        training_table(features, tables["labels"].drop("label_available_at"), datetime(2025, 6, 1))


def test_no_single_feature_separates_the_classes_suspiciously_well(tables, features):
    as_of = features["timestamp"].max()
    matured = as_of - timedelta(days=DISPUTE_WINDOW_DAYS)
    table = training_table(features, tables["labels"], as_of).filter(pl.col("timestamp") <= matured)
    result = audit(table)
    assert table["label_is_fraud"].sum() > 100
    assert result.filter(pl.col("suspicious")).is_empty()
    assert result["auc"].drop_nulls().max() < 0.9  # the best single feature stays well below 0.9


def test_cli_build_writes_features_and_a_manifest(lake, features, tmp_path, capsys):
    out = tmp_path / "f" / "features.parquet"
    assert features_cli(["build", "--lake", str(lake), "--out", str(out)]) == 0
    assert pl.read_parquet(out).equals(features)
    manifest = json.loads((out.parent / "features_manifest.json").read_text())
    assert manifest["rows"] == features.height and manifest["features"] == feature_names()
    assert manifest["input_batches"]["transactions"]
    first = out.read_bytes()
    assert features_cli(["build", "--lake", str(lake), "--out", str(out)]) == 0
    assert out.read_bytes() == first  # deterministic
    assert "28 features" in capsys.readouterr().out


def test_cli_fails_clearly_without_a_lake(tmp_path):
    with pytest.raises(SystemExit, match="not found"):
        features_cli(
            ["build", "--lake", str(tmp_path / "none"), "--out", str(tmp_path / "o.parquet")]
        )


def test_cli_audit_passes_on_clean_data(lake, capsys):
    assert features_cli(["audit", "--lake", str(lake)]) == 0
    assert "no feature separates" in capsys.readouterr().out


def test_committed_catalog_is_up_to_date():
    path = Path(__file__).resolve().parents[2] / "docs" / "feature-catalog.md"
    assert path.read_text(encoding="utf-8") == render(), "run: python -m mmrisk.features catalog"
