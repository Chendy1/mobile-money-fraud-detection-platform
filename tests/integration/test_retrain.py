"""Phase 16 with trained models: a champion built on older data, a candidate on the full year."""

import json
import shutil
from datetime import date, datetime

import polars as pl
import pytest

pytest.importorskip("sklearn")
pytest.importorskip("lightgbm")

from mmrisk.api.service import ModelLoadError, load_model  # noqa: E402
from mmrisk.features import compute_features  # noqa: E402
from mmrisk.generation import GeneratorConfig, generate  # noqa: E402
from mmrisk.ingestion import ingest_source_dir, read_layer  # noqa: E402
from mmrisk.models.boosting import fit_lightgbm  # noqa: E402
from mmrisk.models.bundle import RiskModel  # noqa: E402
from mmrisk.models.calibration import PlattCalibrator  # noqa: E402
from mmrisk.models.costs import CostModel  # noqa: E402
from mmrisk.models.split import make_split  # noqa: E402
from mmrisk.registry import Registry, RegistryError  # noqa: E402
from mmrisk.retrain.cli import main as retrain_cli  # noqa: E402
from mmrisk.retrain.config import Gates  # noqa: E402
from mmrisk.retrain.run import RetrainError, run_retrain  # noqa: E402

CLOCK = lambda: datetime(2026, 3, 1, 12, 0, 0)  # noqa: E731
CUTOFF = datetime(2025, 10, 31, 23, 59, 59)
COSTS = CostModel(block_cost=175.0)  # not the default, so a copied-over cost model shows
SMALL = dict(min_train_fraud=10, min_val_fraud=5, min_test_fraud=5, n_boot=40)
LENIENT = Gates(
    pr_auc_margin=1.0, cost_margin=1e6, ece_margin=1.0, flagged_ratio_min=0.0,
    flagged_ratio_max=1e9, min_agreement=0.0, **SMALL,
)  # fmt: skip


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    root = tmp_path_factory.mktemp("retrain")
    src, lake = root / "src", root / "lake"
    generate(
        GeneratorConfig(
            num_customers=400, num_transactions=24_000, fraud_rate=0.02, seed=31,
            start_date=date(2025, 1, 1), end_date=date(2025, 12, 31), output_dir=src,
        )
    )  # fmt: skip
    ingest_source_dir(src, lake, CLOCK)
    data = {t: read_layer(lake, "cleaned", t) for t in ("transactions", "customers", "labels")}
    feats = compute_features(data["transactions"], data["customers"])
    return {"root": root, "lake": lake, "features": feats, "labels": data["labels"]}


@pytest.fixture(scope="module")
def champion_dir(env, tmp_path_factory):
    """A model built from the data as it stood on 2025-10-31 (saved like the advanced step does)."""
    feats = env["features"].filter(pl.col("timestamp") <= CUTOFF)
    ids = feats.select("transaction_id")
    labels = env["labels"].join(ids, on="transaction_id", how="inner")
    split = make_split(feats, labels)
    lgbm, _ = fit_lightgbm(split, seed=1, quick=True)
    y_val = split.val["label_is_fraud"].to_numpy()
    model = RiskModel(lgbm, PlattCalibrator.fit(lgbm.score(split.val), y_val), COSTS)
    out = tmp_path_factory.mktemp("champ")
    model.save(out, extra={"split": {k: str(v) for k, v in split.bounds.items()}})
    return out


@pytest.fixture
def registry(tmp_path, champion_dir):
    reg = Registry(tmp_path / "registry")
    v = reg.register(champion_dir, source="bootstrap", stage="approved")
    reg.promote(v, "test champion")
    return reg


def retrain(env, reg, **kw):
    return run_retrain(env["features"], env["labels"], reg, **kw)


def test_candidate_is_trained_scored_and_registered(env, registry):
    out = retrain(env, registry, quick=True, th=LENIENT)
    assert out.trained and out.approved and out.candidate == "v0002" and not out.promoted
    names = [g.name for g in out.gates]
    assert names == ["data_sufficiency", "fair_comparison", "pr_auc", "cost", "calibration",
                     "flagged_share", "agreement", "roundtrip"]  # fmt: skip
    info = registry.get("v0002")
    assert info["stage"] == "approved" and info["parent"] == "v0001" and info["quick"] is True
    assert [g["name"] for g in info["gates"]] == names
    assert info["metrics"]["rows"] > 0 and info["metrics"]["fraud"] > 0
    assert registry.production()["version"] == "v0001"  # promotion is a separate decision
    registry.verify("v0002")


def test_candidate_is_scored_on_the_test_window_the_champion_never_trained_on(env, registry):
    out = retrain(env, registry, quick=True, th=LENIENT)
    meta = json.loads((registry.versions_dir / out.candidate / "risk_model.json").read_text())
    champ_meta = json.loads((registry.versions_dir / "v0001" / "risk_model.json").read_text())
    assert datetime.fromisoformat(champ_meta["split"]["train_end"]) < datetime.fromisoformat(
        meta["split"]["test_start"]
    )
    assert meta["parent"] == "v0001" and meta["quick"] is True
    # the candidate uses the champion's cost model so the comparison is about the scores only
    assert load_model(registry.version_dir(out.candidate)).cost_model == COSTS


def test_an_impossible_bar_rejects_and_keeps_the_candidate_for_audit(env, registry):
    hard = Gates(pr_auc_margin=-1.0, cost_margin=1e6, ece_margin=1.0, flagged_ratio_min=0.0,
                 flagged_ratio_max=1e9, **SMALL)  # fmt: skip
    out = retrain(env, registry, quick=True, th=hard, auto_promote=True)
    assert out.trained and not out.approved and not out.promoted
    assert [g.name for g in out.gates if g.required and not g.passed] == ["pr_auc"]
    assert registry.get(out.candidate)["stage"] == "rejected"
    assert registry.production()["version"] == "v0001"
    with pytest.raises(RegistryError, match="not 'approved'"):
        registry.promote(out.candidate, "try anyway")


def test_too_little_fraud_stops_before_any_training(env, registry):
    out = retrain(env, registry, quick=True, th=Gates(min_test_fraud=10**6))
    assert not out.trained and not out.approved and out.candidate is None
    assert [g.name for g in out.gates if not g.passed] == ["data_sufficiency"]
    assert registry.ids() == ["v0001"]  # nothing registered


def test_a_champion_that_trained_into_the_test_window_blocks_the_comparison(env, tmp_path):
    full = make_split(env["features"], env["labels"])
    lgbm, _ = fit_lightgbm(full, seed=1, quick=True)
    y_val = full.val["label_is_fraud"].to_numpy()
    model = RiskModel(lgbm, PlattCalibrator.fit(lgbm.score(full.val), y_val), COSTS)
    late = tmp_path / "late"
    bounds = {k: str(v) for k, v in full.bounds.items()}
    bounds["train_end"] = str(full.bounds["matured_end"])  # claims to have seen the test window
    model.save(late, extra={"split": bounds})
    reg = Registry(tmp_path / "reg")
    reg.promote(reg.register(late, source="b", stage="approved"), "x")
    out = retrain(env, reg, quick=True, th=LENIENT)
    assert not out.approved and not out.trained
    assert [g.name for g in out.gates if not g.passed] == ["fair_comparison"]


def test_auto_promote_switches_production_and_the_api_loads_it(env, registry):
    out = retrain(env, registry, th=LENIENT, auto_promote=True)  # full training, not quick
    assert out.approved and out.promoted
    prod = registry.production()
    assert prod["version"] == out.candidate and prod["previous"] == "v0001"
    assert "every required gate" in prod["reason"]
    served = load_model(registry.root)  # what `--model-dir <registry>` would load
    assert served.scorer.n_rounds == load_model(registry.version_dir(out.candidate)).scorer.n_rounds
    registry.rollback("test")
    assert (
        load_model(registry.root).scorer.n_rounds
        == load_model(registry.version_dir("v0001")).scorer.n_rounds
    )


def test_quick_candidates_are_never_auto_promoted(env, registry):
    out = retrain(env, registry, quick=True, th=LENIENT, auto_promote=True)
    assert out.approved and not out.promoted
    assert registry.production()["version"] == "v0001"
    with pytest.raises(RegistryError, match="quick"):
        registry.promote(out.candidate, "smoke")


def test_no_champion_is_an_error(env, tmp_path):
    with pytest.raises(RetrainError, match="no production model"):
        retrain(env, Registry(tmp_path / "empty"), quick=True)


def test_a_champion_for_other_features_is_refused(env, champion_dir, tmp_path):
    model = load_model(champion_dir)
    model.feature_version = "0.9"
    old = tmp_path / "old"
    model.save(
        old, extra={"split": json.loads((champion_dir / "risk_model.json").read_text())["split"]}
    )
    reg = Registry(tmp_path / "reg")
    reg.promote(reg.register(old, source="b", stage="approved"), "x")
    with pytest.raises(RetrainError, match="cannot load the champion"):
        retrain(env, reg, quick=True)


def test_a_corrupt_champion_is_refused(env, registry):
    (registry.versions_dir / "v0001" / "risk_model.joblib").write_bytes(b"damaged")
    with pytest.raises(RetrainError, match="checksum"):
        retrain(env, registry, quick=True)


def test_data_too_short_is_an_error(env, registry):
    short = env["features"].filter(pl.col("timestamp") < datetime(2025, 3, 1))
    with pytest.raises(RetrainError, match="cannot split"):
        run_retrain(short, env["labels"], registry, quick=True)


# ---- the command line -------------------------------------------------------------------------


def cli(env, registry, *args):
    return retrain_cli([args[0], "--lake", str(env["lake"]), "--registry", str(registry.root),
                        "--out", str(env["root"] / "out"), *args[1:]])  # fmt: skip


def test_check_cli_decisions(env, registry, capsys):
    assert cli(env, registry, "check") == 0  # young model, few new labels, no drift report
    assert "no retrain needed" in capsys.readouterr().out
    assert cli(env, registry, "check", "--force") == 1
    drift = env["root"] / "drift.json"
    drift.write_text(json.dumps({"drift_detected": True, "verdict": "MAJOR", "window": "w"}))
    assert cli(env, registry, "check", "--drift-report", str(drift)) == 1
    out = capsys.readouterr().out
    assert "FIRED" in out and "RETRAIN RECOMMENDED" in out
    saved = json.loads((env["root"] / "out" / "check.json").read_text())
    assert saved["retrain"] is True and saved["champion"] == "v0001"
    # one year later in simulated time the model is old, whatever the drift report says
    assert cli(env, registry, "check", "--as-of", "2026-03-01T00:00:00") == 1
    assert "model_age" in capsys.readouterr().out


def test_check_cli_mistakes(env, registry, tmp_path, capsys):
    bad = tmp_path / "bad.json"
    bad.write_text("[]")
    assert cli(env, registry, "check", "--drift-report", str(bad)) == 2
    assert cli(env, registry, "check", "--as-of", "yesterday") == 2
    empty = Registry(tmp_path / "none")
    assert cli(env, empty, "check") == 2
    assert "no production model" in capsys.readouterr().err


def test_run_cli_writes_reports_and_uses_exit_codes(env, registry, capsys):
    code = cli(
        env,
        registry,
        "run",
        "--quick",
        "--pr-auc-margin",
        "1",
        "--cost-margin",
        "1000000",
        "--min-train-fraud",
        "10",
        "--min-val-fraud",
        "5",
        "--min-test-fraud",
        "5",
    )
    out = capsys.readouterr().out
    assert code in (0, 1)  # the other gates decide; the report must agree with the exit code
    report = json.loads((env["root"] / "out" / "report.json").read_text())
    assert (code == 0) == report["approved"]
    assert report["quick"] is True and report["candidate"] == "v0002"
    md = (env["root"] / "out" / "report.md").read_text()
    assert "# Gated retraining report" in md and "ASSUMED" in md and "Quick mode" in md
    assert "report written" in out


def test_run_cli_without_a_champion_exits_2(env, tmp_path, capsys):
    code = retrain_cli(["run", "--lake", str(env["lake"]), "--registry", str(tmp_path / "x"),
                        "--out", str(tmp_path / "o")])  # fmt: skip
    assert code == 2 and "no production model" in capsys.readouterr().err


def test_other_tools_accept_a_registry_root(env, registry, tmp_path):
    from mmrisk.api.loading import build_service
    from mmrisk.drift.cli import main as drift_cli

    svc = build_service(registry.root, env["lake"], use_history=False)
    assert svc is not None
    ref = tmp_path / "ref.json"
    assert drift_cli(["reference", "--lake", str(env["lake"]), "--model-dir", str(registry.root),
                      "--out", str(ref)]) == 0  # fmt: skip
    assert ref.exists()
    shutil.rmtree(registry.root / "versions" / "v0001")
    with pytest.raises(ModelLoadError):
        load_model(registry.root)


def test_run_cli_exit_code_is_1_when_a_gate_fails(env, registry, capsys):
    code = cli(
        env,
        registry,
        "run",
        "--quick",
        "--pr-auc-margin",
        "-1",
        "--cost-margin",
        "1000000",
        "--min-train-fraud",
        "10",
        "--min-val-fraud",
        "5",
        "--min-test-fraud",
        "5",
    )
    assert code == 1
    assert "FAIL  pr_auc" in capsys.readouterr().out
    assert registry.get("v0002")["stage"] == "rejected"
