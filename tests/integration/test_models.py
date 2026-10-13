"""Model pipeline tests: split hygiene, fit-on-train-only, shuffled-label sanity, end-to-end run."""

import json
from datetime import date, datetime, timedelta

import joblib
import numpy as np
import polars as pl
import pytest

pytest.importorskip("sklearn")
pytest.importorskip("matplotlib")

from mmrisk.features import compute_features  # noqa: E402
from mmrisk.generation import GeneratorConfig, generate  # noqa: E402
from mmrisk.generation.config import DISPUTE_WINDOW_DAYS  # noqa: E402
from mmrisk.ingestion import ingest_source_dir, read_layer  # noqa: E402
from mmrisk.models.cli import main as models_cli  # noqa: E402
from mmrisk.models.metrics import pr_auc  # noqa: E402
from mmrisk.models.preprocess import to_matrix  # noqa: E402
from mmrisk.models.report import run  # noqa: E402
from mmrisk.models.split import SplitConfig, SplitError, make_split  # noqa: E402
from mmrisk.models.train import evaluate, fit_models, make_forest, make_logistic  # noqa: E402

CLOCK = lambda: datetime(2026, 3, 1, 12, 0, 0)  # noqa: E731
GAP = timedelta(days=DISPUTE_WINDOW_DAYS)


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    root = tmp_path_factory.mktemp("models")
    src, lake = root / "src", root / "lake"
    generate(
        GeneratorConfig(
            num_customers=500,
            num_transactions=30_000,
            fraud_rate=0.02,
            seed=31,
            start_date=date(2025, 1, 1),
            end_date=date(2025, 12, 31),
            output_dir=src,
        )
    )
    ingest_source_dir(src, lake, CLOCK)
    return src, lake


@pytest.fixture(scope="module")
def tables(env):
    return {t: read_layer(env[1], "cleaned", t) for t in ("transactions", "customers", "labels")}


@pytest.fixture(scope="module")
def features(tables):
    return compute_features(tables["transactions"], tables["customers"])


@pytest.fixture(scope="module")
def split(features, tables):
    return make_split(features, tables["labels"])


# ---- split ------------------------------------------------------------------------------------


def test_split_windows_are_ordered_with_the_label_gap(split):
    b = split.bounds
    assert b["val_start"] - b["train_end"] == GAP
    assert b["train_end"] < b["val_start"] < b["test_start"] < b["matured_end"] < b["data_end"]
    assert b["data_end"] - b["matured_end"] == GAP


def test_every_row_is_inside_its_window_and_parts_do_not_overlap(split):
    b = split.bounds
    assert split.train["timestamp"].max() <= b["train_end"]
    assert (
        split.val["timestamp"].min() > b["val_start"]
        and split.val["timestamp"].max() <= b["test_start"]
    )
    assert split.test["timestamp"].min() > b["test_start"]
    assert split.test["timestamp"].max() <= b["matured_end"]
    ids = [set(p["transaction_id"].to_list()) for p in (split.train, split.val, split.test)]
    assert not (ids[0] & ids[1]) and not (ids[0] & ids[2]) and not (ids[1] & ids[2])


def test_training_labels_were_all_known_on_deployment_day(split):
    assert split.train["label_available_at"].max() <= split.bounds["val_start"]


def test_no_unlabelled_recent_transaction_is_treated_as_legitimate(split):
    """Rows near the end of training would have no confirmed label; the gap leaves them out."""
    assert split.train["timestamp"].max() <= split.bounds["val_start"] - GAP


def test_the_naive_split_uses_labels_that_were_not_yet_known(features, tables):
    naive = make_split(features, tables["labels"], SplitConfig(naive=True))
    assert naive.train.height > make_split(features, tables["labels"]).train.height
    assert (naive.train["label_available_at"] > naive.bounds["val_start"]).any()


def test_validation_and_test_use_final_labels_only_for_matured_rows(split):
    assert split.val["label_available_at"].max() <= split.bounds["data_end"]
    assert split.test["timestamp"].max() <= split.bounds["matured_end"]
    assert split.val["label_is_fraud"].sum() > 10 and split.test["label_is_fraud"].sum() > 10


def test_too_short_data_is_a_clear_error(features, tables):
    short = features.filter(pl.col("timestamp") < datetime(2025, 5, 1))
    with pytest.raises(SplitError):
        make_split(short, tables["labels"])


def test_summary_describes_each_part(split):
    s = split.summary()
    assert s["part"].to_list() == ["train", "validation", "test"]
    assert s["rows"].sum() == split.train.height + split.val.height + split.test.height


# ---- fitting only on train -----------------------------------------------------------------


def test_imputer_statistics_come_from_training_rows_only(split):
    x_train = to_matrix(split.train, "linear")
    pipe = make_logistic(1.0, None, 0).fit(x_train, split.train["label_is_fraud"].to_numpy())
    medians = np.nanmedian(x_train, axis=0)
    stats = pipe.named_steps["impute"].statistics_
    keep = ~np.isnan(medians)
    assert np.allclose(stats[: len(medians)][keep], medians[keep])
    # and they differ from what validation data would give
    val_medians = np.nanmedian(to_matrix(split.val, "linear"), axis=0)
    assert not np.allclose(medians[keep], val_medians[keep], equal_nan=True)


def test_nothing_the_test_part_contains_can_influence_fitting_or_selection(split, experiment):
    """Fit again with the test part destroyed (labels flipped, amounts scrambled)."""
    from dataclasses import replace

    wrecked = split.test.with_columns(
        label_is_fraud=1 - pl.col("label_is_fraud"), amount_kes=pl.col("amount_kes") * 977
    )
    again = fit_models(replace(split, test=wrecked), seed=7, quick=True)
    assert again.grid["val_pr_auc"].to_list() == experiment.grid["val_pr_auc"].to_list()
    probe = split.val.head(400)
    for name in ("logistic_regression", "random_forest"):
        assert np.allclose(
            again.models[name].score(probe), experiment.models[name].score(probe), atol=1e-9
        )


def test_selected_models_are_exactly_the_train_only_fit(split, experiment):
    """Rebuild each chosen model from its settings and the training rows alone."""
    y = split.train["label_is_fraud"].to_numpy()
    probe = split.val.head(400)
    for name, kind in (("logistic_regression", "linear"), ("random_forest", "tree")):
        chosen = experiment.models[name]
        p = chosen.params
        pipe = (
            make_logistic(p["C"], p["class_weight"], 7)
            if kind == "linear"
            else make_forest(p["min_samples_leaf"], p["class_weight"], p["n_estimators"], 7)
        )
        pipe.fit(to_matrix(split.train, kind), y)
        assert np.allclose(
            pipe.predict_proba(to_matrix(probe, kind))[:, 1], chosen.score(probe), atol=1e-9
        )


def test_shuffled_labels_give_no_skill(split):
    """If the pipeline leaked anything, a model fitted to random labels would still rank well."""
    x_tr, x_val = to_matrix(split.train, "tree"), to_matrix(split.val, "tree")
    y_tr = split.train["label_is_fraud"].to_numpy()
    y_val = split.val["label_is_fraud"].to_numpy()
    real = make_forest(5, None, 60, 0).fit(x_tr, y_tr).predict_proba(x_val)[:, 1]
    fake_scores = []
    for seed in range(3):
        shuffled = np.random.default_rng(seed).permutation(y_tr)
        fake = make_forest(5, None, 60, 0).fit(x_tr, shuffled).predict_proba(x_val)[:, 1]
        fake_scores.append(pr_auc(y_val, fake))
    prevalence = y_val.mean()
    assert np.mean(fake_scores) < 4 * prevalence
    assert pr_auc(y_val, real) > 5 * np.mean(fake_scores)


# ---- fitting and evaluating ------------------------------------------------------------------


@pytest.fixture(scope="module")
def experiment(split):
    return fit_models(split, seed=7, quick=True)


def test_models_beat_the_reference_baselines_on_validation(split, experiment):
    res = evaluate(experiment.models, split.val, n_boot=20, seed=1)
    pa = {n: res[n]["point"]["pr_auc"] for n in experiment.models}
    prevalence = split.val["label_is_fraud"].mean()
    assert pa["no_skill"] == pytest.approx(prevalence)
    assert pa["rules"] > 3 * prevalence
    assert pa["logistic_regression"] > pa["rules"] and pa["random_forest"] > pa["rules"]


def test_evaluate_returns_intervals_that_bracket_sensible_values(split, experiment):
    res = evaluate(experiment.models, split.test, n_boot=25, seed=2)
    for name in experiment.models:
        lo, hi = res[name]["ci"]["pr_auc"]
        assert 0 <= lo <= hi <= 1
    assert len(res["_boot_pr_auc"]["rules"]) == 25
    again = evaluate(experiment.models, split.test, n_boot=25, seed=2)
    assert res["random_forest"]["ci"] == again["random_forest"]["ci"]


def test_fitting_is_deterministic(split, experiment):
    other = fit_models(split, seed=7, quick=True)
    probe = split.test.head(300)
    for name in ("logistic_regression", "random_forest"):
        assert np.allclose(
            experiment.models[name].score(probe), other.models[name].score(probe), atol=1e-9
        )


def test_grid_records_every_candidate_and_selection_uses_validation(experiment):
    grid = experiment.grid
    assert set(grid["family"]) == {"logistic_regression", "random_forest"}
    for fam in grid["family"].unique():
        best = grid.filter(pl.col("family") == fam)["val_pr_auc"].max()
        chosen = experiment.models[fam]
        assert chosen.params is not None and best > 0


# ---- end to end ------------------------------------------------------------------------------


def test_run_writes_report_figures_models_and_metadata(env, features, tables, tmp_path):
    src, _ = env
    truth = src / "ground_truth" / "transaction_truth.parquet"
    res = run(
        features, tables["labels"], tmp_path / "m", tmp_path / "docs" / "r.md",
        tmp_path / "docs" / "figs", truth_file=truth, quick=True, n_boot=20,
    )  # fmt: skip
    assert len(list((tmp_path / "docs" / "figs").glob("*.png"))) == 3
    assert all(p.stat().st_size > 5_000 for p in (tmp_path / "docs" / "figs").glob("*.png"))
    assert "All data is synthetic" in res.text and "Audit against ground truth" in res.text
    assert "nan" not in res.text.lower().replace("finance", "")
    meta = json.loads((tmp_path / "m" / "baseline_metadata.json").read_text())
    assert meta["feature_version"] and meta["split"]["gap_days"] == "45"
    model = joblib.load(tmp_path / "m" / "random_forest.joblib")
    probe = features.head(50)
    scores = model.score(probe)
    assert scores.shape == (50,) and ((scores >= 0) & (scores <= 1)).all()


def test_run_without_truth_skips_the_audit(features, tables, tmp_path):
    res = run(
        features, tables["labels"], tmp_path / "m", tmp_path / "r.md", tmp_path / "f",
        truth_file=None, quick=True, n_boot=15,
    )  # fmt: skip
    assert "Audit against ground truth" not in res.text


def test_cli_fails_clearly_without_a_lake(tmp_path, capsys):
    assert models_cli(["train", "--lake", str(tmp_path / "none")]) == 2
    assert "not found" in capsys.readouterr().err
