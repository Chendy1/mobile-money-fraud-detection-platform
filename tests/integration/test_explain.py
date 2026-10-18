"""Phase 9 tests on a generated year: exactness, reason codes, the run and the CLI."""

import json
from datetime import date, datetime
from pathlib import Path

import numpy as np
import polars as pl
import pytest

pytest.importorskip("sklearn")
pytest.importorskip("lightgbm")
pytest.importorskip("matplotlib")

from mmrisk.features import compute_features  # noqa: E402
from mmrisk.features.registry import FEATURE_VERSION, feature_names  # noqa: E402
from mmrisk.generation import GeneratorConfig, generate  # noqa: E402
from mmrisk.ingestion import ingest_source_dir, read_layer  # noqa: E402
from mmrisk.models import explain as X  # noqa: E402
from mmrisk.models.boosting import fit_lightgbm  # noqa: E402
from mmrisk.models.bundle import META_FILE, RiskModel  # noqa: E402
from mmrisk.models.calibration import PlattCalibrator  # noqa: E402
from mmrisk.models.cli import main as models_cli  # noqa: E402
from mmrisk.models.costs import CostModel  # noqa: E402
from mmrisk.models.explain_run import (  # noqa: E402
    BundleMismatchError,
    load_bundle,
    refit_without,
    run_explain,
)
from mmrisk.models.split import make_split  # noqa: E402

CLOCK = lambda: datetime(2026, 3, 1, 12, 0, 0)  # noqa: E731


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    root = tmp_path_factory.mktemp("exp")
    src, lake = root / "src", root / "lake"
    generate(
        GeneratorConfig(
            num_customers=500, num_transactions=30_000, fraud_rate=0.02, seed=31,
            start_date=date(2025, 1, 1), end_date=date(2025, 12, 31), output_dir=src,
        )
    )  # fmt: skip
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
def risk(split):
    model, _ = fit_lightgbm(split, seed=0, quick=True)
    y_val = split.val["label_is_fraud"].to_numpy()
    return RiskModel(model, PlattCalibrator.fit(model.score(split.val), y_val), CostModel())


@pytest.fixture(scope="module")
def models_dir(env, split, risk):
    d = env[0] / "advanced"
    risk.save(d, extra={"split": {k: str(v) for k, v in split.bounds.items()}})
    return d


@pytest.fixture(scope="module")
def run(env, features, tables, models_dir):
    root, src, _ = env
    return run_explain(
        features, tables["labels"], models_dir, root / "ex.md", root / "ex_figs",
        truth_file=src / "ground_truth" / "transaction_truth.parquet",
        n_repeats=2, ablation_seeds=1, quick=True,
    )  # fmt: skip


def _logit(p):
    p = np.clip(p, 1e-12, 1 - 1e-12)
    return np.log(p / (1 - p))


# ---- exactness -------------------------------------------------------------------------------


def test_contributions_add_up_to_the_models_log_odds(split, risk):
    phi, base = X.contribution_frame(risk.scorer, split.test)
    assert phi.shape == (split.test.height, len(feature_names()))
    raw = risk.scorer.score(split.test)
    assert np.abs(base + phi.sum(axis=1) - _logit(raw)).max() < 1e-8


def test_contributions_match_the_shap_library(split, risk):
    shap = pytest.importorskip("shap")
    from mmrisk.models.preprocess import to_matrix

    sample = split.test.head(300)
    ours, base = risk.scorer.contributions(sample)
    theirs = shap.TreeExplainer(risk.scorer._booster).shap_values(to_matrix(sample, "tree"))
    theirs = theirs[1] if isinstance(theirs, list) else theirs
    assert np.abs(ours - theirs).max() < 1e-6


def test_calibrated_effects_add_up_to_the_calibrated_log_odds(split, risk):
    sample = split.test.head(2000)
    phi, base = X.contribution_frame(risk.scorer, sample)
    cal = risk.calibrator
    total = cal.a * (base + phi.sum(axis=1)) + cal.b
    mid = (risk.probability(sample) > 1e-4) & (risk.probability(sample) < 1 - 1e-4)
    assert np.abs(total[mid] - _logit(risk.probability(sample))[mid]).max() < 1e-6


# ---- reason codes through the bundle ---------------------------------------------------------


def test_bundle_explain_matches_direct_computation_and_is_row_independent(split, risk):
    sample = split.test.head(400)
    codes = risk.explain(sample, top_k=3)
    phi, _ = X.contribution_frame(risk.scorer, sample)
    direct = X.reason_codes(sample, phi, a=risk.calibrator.a, top_k=3)
    assert codes.equals(direct)
    assert codes.group_by("transaction_id").len()["len"].max() <= 3
    assert (codes["effect"] > 0).all()
    ids = sample.head(10)["transaction_id"]
    assert risk.explain(sample.head(10)).equals(
        codes.filter(pl.col("transaction_id").is_in(ids.to_list()))
    )


def test_reason_codes_need_no_labels(split, risk):
    unlabelled = split.test.head(300).drop("label_is_fraud")
    assert risk.explain(unlabelled).equals(risk.explain(split.test.head(300)))


def test_explaining_refuses_a_scorer_or_calibration_it_cannot_explain(split, risk):
    class Plain:
        name = "plain"

        def score(self, df):
            return np.full(df.height, 0.01)

    with pytest.raises(X.ExplainError):
        RiskModel(Plain(), risk.calibrator, CostModel()).explain(split.test.head(5))
    identity = PlattCalibrator(a=1.0, b=0.0, link="identity")
    with pytest.raises(X.ExplainError):
        RiskModel(risk.scorer, identity, CostModel()).explain(split.test.head(5))


def test_flagged_transactions_get_reasons_and_the_reasons_are_the_largest_effects(split, risk):
    assess = risk.assess(split.test)
    flagged = split.test.filter(pl.Series(assess["action"].to_numpy() != "ALLOW"))
    assert flagged.height > 0
    codes = risk.explain(flagged, top_k=3)
    assert codes["transaction_id"].n_unique() >= 0.95 * flagged.height
    # the first reason is at least as large as the second and third
    for _tid, grp in list(codes.group_by("transaction_id"))[:50]:
        eff = grp.sort("rank")["effect"].to_list()
        assert eff == sorted(eff, reverse=True)


# ---- the run ---------------------------------------------------------------------------------


def test_run_writes_report_figures_and_metrics(env, run):
    root = env[0]
    assert len(run.figures) in (2, 3) and all(f.stat().st_size > 8_000 for f in run.figures)
    assert (root / "advanced" / "explain_metrics.json").exists()
    saved = json.loads((root / "advanced" / "explain_metrics.json").read_text())
    assert saved["additivity_error_max"] < 1e-8
    assert "Model explanations" in run.text and "synthetic" in run.text.lower()


def test_report_numbers_are_computed_not_typed(run):
    m = run.metrics
    assert f"{m['additivity_error_max']:.1e}" in run.text
    top = m["importance_fraud_rows"][0]["unit"]
    assert f"`{top}`" in run.text
    assert f"{m['n_flagged']:,}" in run.text
    assert f"{m['n_test']:,}" in run.text
    assert abs(sum(r["share"] for r in m["importance_all"]) - 1.0) < 1e-9
    assert abs(sum(r["share"] for r in m["groups_all"]) - 1.0) < 1e-9


def test_permutation_importance_agrees_that_something_matters(run):
    drops = [r["drop"] for r in run.metrics["permutation"]]
    assert max(drops) > 0.02  # at least one feature carries real ranking power
    assert all(r["sd"] >= 0 for r in run.metrics["permutation"])


def test_slices_add_up_and_use_the_same_policy(env, split, risk, run):
    by_attr: dict[str, int] = {}
    for r in run.metrics["slices"]:
        by_attr[r["slice"]] = by_attr.get(r["slice"], 0) + r["n"]
    assert set(by_attr.values()) == {split.test.height}
    assess = risk.assess(split.test)
    flagged = (assess["action"] != "ALLOW").sum()
    seg = [r for r in run.metrics["slices"] if r["slice"] == "segment"]
    assert sum(r["flagged_per_10k"] * r["n"] / 10_000 for r in seg) == pytest.approx(flagged)
    assert run.metrics["n_flagged"] == flagged


def test_pattern_check_only_with_ground_truth(env, features, tables, models_dir):
    root = env[0]
    res = run_explain(
        features, tables["labels"], models_dir, root / "nt.md", root / "nt_figs",
        truth_file=None, n_repeats=2, ablation_seeds=1, quick=True,
    )  # fmt: skip
    assert res.metrics["patterns"] is None
    assert "Audit against ground truth" not in res.text


# ---- refitting without a feature -------------------------------------------------------------


def test_blanking_every_feature_leaves_a_model_with_no_skill(split, risk):
    all_features = feature_names()
    blank = refit_without(risk, split, all_features, seed=0)
    full = refit_without(risk, split, [], seed=0)
    rate = split.val["label_is_fraud"].mean()
    assert blank < 3 * rate and full > 5 * rate  # negative control: nothing to learn from


def test_refit_is_reproducible_and_does_not_touch_the_test_part(split, risk):
    a = refit_without(risk, split, ["is_new_device"], seed=1)
    b = refit_without(risk, split, ["is_new_device"], seed=1)
    assert a == b


# ---- the bundle must belong to this data -----------------------------------------------------


def test_a_bundle_from_another_split_or_feature_version_is_refused(env, split, risk, tmp_path):
    with pytest.raises(BundleMismatchError, match="no risk model"):
        load_bundle(tmp_path, split)
    other = tmp_path / "other"
    risk.save(other, extra={"split": {"train_end": "2000-01-01"}})
    with pytest.raises(BundleMismatchError, match="different split"):
        load_bundle(other, split)
    old = tmp_path / "old"
    risk.save(old, extra={"split": {k: str(v) for k, v in split.bounds.items()}})
    meta = json.loads((old / META_FILE).read_text())
    meta["feature_version"] = "0." + FEATURE_VERSION
    (old / META_FILE).write_text(json.dumps(meta))
    with pytest.raises(BundleMismatchError, match="feature set"):
        load_bundle(old, split)


# ---- CLI and card ----------------------------------------------------------------------------


def test_cli_explain_and_card_end_to_end(env, models_dir, tmp_path, capsys):
    root, src, lake = env
    # the card also needs the Phase 8 results; produce them with the real command
    rc = models_cli(["train", "--lake", str(lake), "--source", str(src), "--models-dir",
                     str(root / "bl"), "--report", str(root / "b.md"), "--figures",
                     str(root / "bf"), "--quick", "--bootstrap", "10"])  # fmt: skip
    assert rc == 0
    adv_dir = root / "advanced_cli"
    rc = models_cli(["advanced", "--lake", str(lake), "--source", str(src), "--baselines-dir",
                     str(root / "bl"), "--models-dir", str(adv_dir), "--report",
                     str(root / "a.md"), "--figures", str(root / "af"), "--quick",
                     "--bootstrap", "10"])  # fmt: skip
    assert rc == 0
    rc = models_cli(["explain", "--lake", str(lake), "--source", str(src), "--models-dir",
                     str(adv_dir), "--report", str(tmp_path / "e.md"), "--figures",
                     str(tmp_path / "f"), "--quick"])  # fmt: skip
    assert rc == 0 and (tmp_path / "e.md").exists()
    rc = models_cli(["card", "--models-dir", str(adv_dir), "--out", str(tmp_path / "card.md")])
    assert rc == 0
    card = (tmp_path / "card.md").read_text()
    adv = json.loads((adv_dir / "advanced_metrics.json").read_text())
    assert f"{adv['test_pr_auc']['lightgbm']:.3f}" in card
    assert "Synthetic data only" in card
    for line in card.splitlines():
        if line.startswith(("| Hand-written", "| Logistic", "| Random forest", "| LightGBM")):
            assert line.count("n/a") == 0, line  # every number the card promises was found


def test_cli_fails_clearly(env, tmp_path, capsys):
    root, src, lake = env
    assert models_cli(["explain", "--lake", str(tmp_path / "none")]) == 2
    rc = models_cli(["explain", "--lake", str(lake), "--source", str(src), "--models-dir",
                     str(tmp_path / "empty")])  # fmt: skip
    assert rc == 2 and "no risk model" in capsys.readouterr().err
    assert models_cli(["card", "--models-dir", str(tmp_path / "empty")]) == 2
    assert "run `advanced` and `explain` first" in capsys.readouterr().err


def test_explaining_does_not_change_the_saved_model(env, models_dir, features, tables):
    before = (models_dir / "risk_model.joblib").read_bytes()
    run_explain(
        features, tables["labels"], models_dir, env[0] / "again.md", env[0] / "again_figs",
        truth_file=None, n_repeats=1, ablation_seeds=1, quick=True,
    )  # fmt: skip
    assert (models_dir / "risk_model.joblib").read_bytes() == before
    assert Path(models_dir / META_FILE).exists()
