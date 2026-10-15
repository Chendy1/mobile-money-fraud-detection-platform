"""Phase 8 pipeline tests: LightGBM, calibration, decisions, saved model, CLI, leakage controls."""

import json
from dataclasses import replace
from datetime import date, datetime

import joblib
import numpy as np
import polars as pl
import pytest

pytest.importorskip("sklearn")
pytest.importorskip("lightgbm")
pytest.importorskip("matplotlib")

from mmrisk.features import compute_features  # noqa: E402
from mmrisk.generation import GeneratorConfig, generate  # noqa: E402
from mmrisk.ingestion import ingest_source_dir, read_layer  # noqa: E402
from mmrisk.models.advanced import BaselineError, load_baselines, run_advanced  # noqa: E402
from mmrisk.models.boosting import LGBMModel, fit_lightgbm  # noqa: E402
from mmrisk.models.bundle import BUNDLE_FILE, META_FILE, RiskModel  # noqa: E402
from mmrisk.models.calibration import PlattCalibrator  # noqa: E402
from mmrisk.models.cli import main as models_cli  # noqa: E402
from mmrisk.models.costs import CostModel  # noqa: E402
from mmrisk.models.metrics import pr_auc  # noqa: E402
from mmrisk.models.split import make_split  # noqa: E402

CLOCK = lambda: datetime(2026, 3, 1, 12, 0, 0)  # noqa: E731


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    root = tmp_path_factory.mktemp("adv")
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
    return root, src, lake


@pytest.fixture(scope="module")
def tables(env):
    return {t: read_layer(env[2], "cleaned", t) for t in ("transactions", "customers", "labels")}


@pytest.fixture(scope="module")
def features(tables):
    return compute_features(tables["transactions"], tables["customers"])


@pytest.fixture(scope="module")
def split(features, tables):
    return make_split(features, tables["labels"])


@pytest.fixture(scope="module")
def baselines_dir(env):
    """Phase 7 baselines, trained once with the real CLI (quick settings)."""
    root, src, lake = env
    out = root / "baselines"
    rc = models_cli(
        ["train", "--lake", str(lake), "--source", str(src), "--models-dir", str(out),
         "--report", str(root / "b.md"), "--figures", str(root / "bf"), "--quick",
         "--bootstrap", "20"]
    )  # fmt: skip
    assert rc == 0
    return out


@pytest.fixture(scope="module")
def lgbm(split):
    model, grid = fit_lightgbm(split, seed=0, quick=True)
    return model, grid


@pytest.fixture(scope="module")
def risk(split, lgbm):
    model, _ = lgbm
    y_val = split.val["label_is_fraud"].to_numpy()
    cal = PlattCalibrator.fit(model.score(split.val), y_val)
    return RiskModel(model, cal, CostModel())


# ---- LightGBM -------------------------------------------------------------------------------


def test_lightgbm_is_deterministic(split, lgbm):
    again, _ = fit_lightgbm(split, seed=0, quick=True)
    assert np.array_equal(lgbm[0].score(split.val), again.score(split.val))


def test_lightgbm_learns_something_real_and_is_chosen_on_validation(split, lgbm):
    model, grid = lgbm
    y_test = split.test["label_is_fraud"].to_numpy()
    assert pr_auc(y_test, model.score(split.test)) > 5 * y_test.mean()
    best_row = grid.sort("val_pr_auc", descending=True).row(0, named=True)
    assert model.n_rounds == best_row["rounds"]
    assert grid.height == 2 and (grid["rounds"] <= 100).all()


def test_shuffled_training_labels_give_a_model_with_no_skill(split):
    """Negative control: scrambled training labels must leave test PR-AUC near the fraud rate."""
    rng = np.random.default_rng(0)
    shuffled = split.train.with_columns(
        label_is_fraud=pl.Series(rng.permutation(split.train["label_is_fraud"].to_numpy()))
    )
    from dataclasses import replace as dc_replace

    control = dc_replace(split, train=shuffled)
    model, _ = fit_lightgbm(control, seed=0, quick=True)
    y = split.test["label_is_fraud"].to_numpy()
    assert pr_auc(y, model.score(split.test)) < 4 * y.mean()


def test_destroying_the_test_part_changes_nothing_that_was_fitted_or_chosen(split, lgbm):
    wrecked = split.test.with_columns(
        label_is_fraud=1 - pl.col("label_is_fraud"), amount_kes=pl.col("amount_kes") * 977
    )
    model, grid = fit_lightgbm(replace(split, test=wrecked), seed=0, quick=True)
    assert model.model_str == lgbm[0].model_str
    assert grid.drop("seconds").equals(lgbm[1].drop("seconds"))


def test_model_survives_pickling_and_text_round_trip(split, lgbm, tmp_path):
    model, _ = lgbm
    joblib.dump(model, tmp_path / "m.joblib")
    loaded = joblib.load(tmp_path / "m.joblib")
    assert isinstance(loaded, LGBMModel)
    assert np.array_equal(model.score(split.test), loaded.score(split.test))
    assert sum(loaded.gain_importance().values()) == pytest.approx(1.0)


# ---- risk model -----------------------------------------------------------------------------


def test_assess_returns_consistent_actions_and_needs_no_labels(split, risk):
    unlabelled = split.test.drop("label_is_fraud", "label_available_at")
    out = risk.assess(unlabelled)
    assert out.height == split.test.height
    levels = dict(zip(["ALLOW", "REVIEW", "BLOCK"], ["LOW", "MEDIUM", "HIGH"], strict=True))
    assert all(levels[a] == r for a, r in zip(out["action"], out["risk_level"], strict=True))
    assert out["risk_probability"].is_between(0, 1).all()
    cheapest = out.select(
        pl.concat_list("expected_cost_allow", "expected_cost_review", "expected_cost_block")
        .list.arg_min()
        .alias("i")
    )["i"].to_list()
    assert [["ALLOW", "REVIEW", "BLOCK"][i] for i in cheapest] == out["action"].to_list()
    assert out.equals(risk.assess(split.test))  # labels present or absent: same answer


def test_changing_test_labels_cannot_change_any_decision(split, risk):
    flipped = split.test.with_columns(label_is_fraud=1 - pl.col("label_is_fraud"))
    assert risk.assess(flipped).equals(risk.assess(split.test))


def test_saved_risk_model_reloads_with_identical_decisions(split, risk, tmp_path):
    risk.save(tmp_path / "m", extra={"note": "x"})
    assert (tmp_path / "m" / BUNDLE_FILE).exists()
    meta = json.loads((tmp_path / "m" / META_FILE).read_text())
    assert meta["cost_model_ASSUMED"]["review_cost"] == 25.0 and meta["note"] == "x"
    assert set(meta["calibrator"]) == {"a", "b", "link"}
    loaded = RiskModel.load(tmp_path / "m")
    assert loaded.assess(split.test).equals(risk.assess(split.test))


def test_riskier_transactions_get_milder_or_equal_action_only_if_cheaper(split, risk):
    """Whole-chain sanity: the top-probability decile is acted on more than the bottom one."""
    out = risk.assess(split.test).sort("risk_probability")
    n = out.height // 10
    low, high = out.head(n), out.tail(n)
    assert (high["action"] != "ALLOW").mean() > (low["action"] != "ALLOW").mean()


# ---- baselines loading ----------------------------------------------------------------------


def test_load_baselines_refuses_a_missing_or_foreign_set(split, baselines_dir, tmp_path):
    models = load_baselines(baselines_dir, split)
    assert set(models) == {"no_skill", "rules", "logistic_regression", "random_forest"}
    with pytest.raises(BaselineError, match="missing"):
        load_baselines(tmp_path, split)
    import shutil

    copy = tmp_path / "copy"
    shutil.copytree(baselines_dir, copy)
    meta_path = copy / "baseline_metadata.json"
    meta = json.loads(meta_path.read_text())
    meta["split"]["train_end"] = "1999-01-01 00:00:00"
    meta_path.write_text(json.dumps(meta))
    with pytest.raises(BaselineError, match="different split"):
        load_baselines(copy, split)


# ---- end to end -----------------------------------------------------------------------------


@pytest.fixture(scope="module")
def advanced_run(env, features, tables, baselines_dir):
    root, src, _ = env
    return run_advanced(
        features, tables["labels"], baselines_dir, root / "adv_models", root / "adv.md",
        root / "adv_figs", truth_file=src / "ground_truth" / "transaction_truth.parquet",
        quick=True, n_boot=30,
    )  # fmt: skip


def test_report_numbers_are_computed_not_typed(env, split, advanced_run):
    text = advanced_run.text
    assert "every cost in this report is an assumption" in text
    c = advanced_run.metrics["cost_per_10k"]
    y = split.test["label_is_fraud"].to_numpy()
    amount = split.test["amount_kes"].to_numpy().astype(float)
    expected_allow_all = float((y * amount).sum() / len(y) * 10_000)  # loss_rate 1.0
    assert c["allow_all"]["point"] == pytest.approx(expected_allow_all)
    assert f"{expected_allow_all:,.0f}" in text
    assert c["oracle"]["point"] == 0.0
    for key in ("rules", "logistic_regression", "random_forest", "lightgbm"):
        assert c[key]["ci"][0] <= c[key]["ci"][1]
        assert f"{c[key]['point']:,.0f}" in text
    assert "Audit against ground truth" in text


def test_calibrators_are_fitted_on_validation_with_the_right_link(
    split, baselines_dir, advanced_run
):
    models = load_baselines(baselines_dir, split)
    y_val = split.val["label_is_fraud"].to_numpy()
    cal = advanced_run.metrics["calibrators"]
    for name in ("rules", "logistic_regression", "random_forest"):
        link = "identity" if name == "rules" else "logit"
        expected = PlattCalibrator.fit(models[name].score(split.val), y_val, link)
        assert cal[name]["link"] == link
        assert cal[name]["a"] == pytest.approx(expected.a, rel=1e-6)
        assert cal[name]["b"] == pytest.approx(expected.b, rel=1e-6)
    assert cal["lightgbm"]["link"] == "logit"


def test_figures_and_model_files_are_written(env, advanced_run):
    root = env[0]
    figs = sorted((root / "adv_figs").glob("0[4-7]-*.png"))
    assert len(figs) == 4 and all(f.stat().st_size > 8_000 for f in figs)
    for name in (BUNDLE_FILE, META_FILE, "advanced_metrics.json"):
        assert (root / "adv_models" / name).exists()
    metrics = json.loads((root / "adv_models" / "advanced_metrics.json").read_text())
    assert metrics["cost_model_ASSUMED"]["block_cost"] == 150.0


def test_a_cost_rule_beats_a_fixed_cutoff_on_the_real_pipeline(advanced_run):
    c = advanced_run.metrics["cost_per_10k"]
    assert c["lightgbm"]["point"] < c["naive"]["point"] < c["allow_all"]["point"]


def test_sensitivity_covers_every_scorer_and_factor(advanced_run):
    s = advanced_run.metrics["sensitivity"]
    assert s["factors"] == [0.25, 0.5, 1.0, 2.0, 4.0]
    assert all(len(v) == 5 for v in s["savings"].values())
    # higher friction costs never make a policy better than allowing everything by more
    for vals in s["savings"].values():
        assert vals[0] >= vals[-1] - 1e-9


def test_without_ground_truth_the_audit_section_is_left_out(env, features, tables, baselines_dir):
    root = env[0]
    out = run_advanced(
        features, tables["labels"], baselines_dir, root / "m2", root / "adv2.md", root / "f2",
        truth_file=None, quick=True, n_boot=10,
    )  # fmt: skip
    assert "Audit against ground truth" not in out.text


# ---- CLI ------------------------------------------------------------------------------------


def test_cli_advanced_end_to_end(env, baselines_dir, capsys):
    root, src, lake = env
    rc = models_cli(
        ["advanced", "--lake", str(lake), "--source", str(src),
         "--baselines-dir", str(baselines_dir), "--models-dir", str(root / "cm"),
         "--report", str(root / "c.md"),
         "--figures", str(root / "cf"), "--quick", "--bootstrap", "10",
         "--review-cost", "40"]
    )  # fmt: skip
    assert rc == 0
    assert "ASSUMED costs" in capsys.readouterr().out
    meta = json.loads((root / "cm" / META_FILE).read_text())
    assert meta["cost_model_ASSUMED"]["review_cost"] == 40.0


def test_cli_fails_clearly(env, tmp_path, baselines_dir, capsys):
    root, src, lake = env
    assert models_cli(["advanced", "--lake", str(tmp_path / "none")]) == 2
    assert "not found" in capsys.readouterr().err
    rc = models_cli(["advanced", "--lake", str(lake), "--baselines-dir", str(tmp_path / "nobase")])
    assert rc == 2 and "run `python -m mmrisk.models train`" in capsys.readouterr().err
    rc = models_cli(
        ["advanced", "--lake", str(lake), "--baselines-dir", str(baselines_dir),
         "--review-prevents", "1.5"]
    )  # fmt: skip
    assert rc == 2 and "review_prevents" in capsys.readouterr().err
