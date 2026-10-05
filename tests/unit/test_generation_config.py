"""Unit tests: configuration validation, reference constants, CLI behaviour."""

import json
from datetime import date

import numpy as np
import pytest

from mmrisk.generation import GeneratorConfig
from mmrisk.generation.cli import main
from mmrisk.generation.legit import finalize_amount
from mmrisk.generation.reference import (
    EPISODE_P,
    HOUR_P,
    SEGMENT_MIX,
    SEGMENT_P,
)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"num_customers": 5},
        {"num_transactions": 100},
        {"fraud_rate": -0.01},
        {"fraud_rate": 0.5},
        {"start_date": date(2025, 2, 1), "end_date": date(2025, 1, 1)},
        {"start_date": date(2025, 1, 1), "end_date": date(2025, 1, 10)},
    ],
)
def test_invalid_config_is_rejected(kwargs):
    with pytest.raises(ValueError):
        GeneratorConfig(**kwargs).validate()


def test_default_config_is_valid():
    GeneratorConfig().validate()


def test_probability_tables_sum_to_one():
    assert SEGMENT_P.sum() == pytest.approx(1.0)
    assert EPISODE_P.sum() == pytest.approx(1.0)
    assert SEGMENT_MIX.sum(axis=1) == pytest.approx(np.ones(len(SEGMENT_MIX)))
    for p in HOUR_P:
        assert p.sum() == pytest.approx(1.0)
        assert len(p) == 24


def test_night_share_is_small_for_every_type():
    for p in HOUR_P:
        assert p[:5].sum() < 0.05


def test_finalize_amount_clips_and_rounds():
    rng = np.random.default_rng(0)
    out = finalize_amount(np.array([-5.0, 3.2, 1e9]), rng, 1)
    assert out.min() >= 1
    assert out.max() == 250_000
    assert out.dtype == np.int64


def test_cli_rejects_bad_arguments(tmp_path, capsys):
    rc = main(["--fraud-rate", "0.9", "--output-dir", str(tmp_path)])
    assert rc == 2
    assert "fraud_rate" in capsys.readouterr().err
    assert not (tmp_path / "transactions.parquet").exists()


def test_cli_writes_files_and_manifest(tmp_path):
    rc = main(
        [
            "--num-customers",
            "100",
            "--num-transactions",
            "2000",
            "--fraud-rate",
            "0.02",
            "--seed",
            "3",
            "--end-date",
            "2025-02-15",
            "--output-dir",
            str(tmp_path),
        ]
    )
    assert rc == 0
    manifest = json.loads((tmp_path / "manifest.json").read_text())
    assert manifest["synthetic_data_only"] is True
    assert manifest["true_fraud_rows"] == 40
    assert manifest["config"]["seed"] == 3
    for name in ("transactions", "labels", "customers", "devices"):
        assert (tmp_path / f"{name}.parquet").exists()
    assert (tmp_path / "ground_truth" / "transaction_truth.parquet").exists()
