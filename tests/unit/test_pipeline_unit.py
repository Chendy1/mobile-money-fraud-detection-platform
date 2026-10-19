"""The pipeline's building blocks: tracker guard, lineage, metric collection and stage handling."""

import json
import math

import pytest

pytest.importorskip("sklearn")
pytest.importorskip("lightgbm")

from mmrisk.pipeline import (  # noqa: E402
    NullTracker,
    PipelineConfig,
    PipelineError,
    TrackingError,
    run_pipeline,
)
from mmrisk.pipeline.collect import collect, sanitize  # noqa: E402
from mmrisk.pipeline.lineage import code_version, data_fingerprint, environment  # noqa: E402
from mmrisk.pipeline.run import STAGES, resolve_dirs  # noqa: E402
from mmrisk.pipeline.tracker import check_tracking_uri  # noqa: E402

# ---- the tracking location guard ----


@pytest.mark.parametrize(
    "uri",
    ["sqlite:///mlflow.db", "sqlite:////tmp/x/mlflow.db", "mlruns", "./runs", "file:./runs",
     "C:\\Users\\me\\runs", "D:/runs"],
)  # fmt: skip
def test_local_tracking_locations_are_accepted(uri):
    assert check_tracking_uri(uri) == uri


@pytest.mark.parametrize(
    "uri",
    ["http://localhost:5000", "https://mlflow.example.com", "databricks", "postgresql://u:p@h/db",
     "mysql://u:p@h/db", "s3://bucket/path", "", "   "],
)  # fmt: skip
def test_remote_or_empty_tracking_locations_are_refused(uri):
    with pytest.raises(TrackingError):
        check_tracking_uri(uri)


# ---- the in-memory tracker ----


def test_null_tracker_records_and_skips_values_that_are_not_numbers():
    t = NullTracker()
    t.start("r", {"a": "1"})
    t.log_metrics({"ok": 1.5, "nan": math.nan, "inf": math.inf, "none": None, "flag": True})
    assert t.metrics == {"ok": 1.5} and t.skipped_metrics == 4
    t.log_params({"p": 1})
    t.log_params({"p": "1", "q": 2})  # the same value again is fine
    with pytest.raises(TrackingError, match="twice"):
        t.log_params({"p": 2})
    t.end("FINISHED")
    assert t.status == "FINISHED" and t.run_name == "r" and t.tags["a"] == "1"


def test_metric_names_are_made_safe():
    assert sanitize("a/b c-d.e_f") == "a/b c-d.e_f"
    assert sanitize("x@100%") == "x_100_"
    assert len(sanitize("y" * 500)) == 240


# ---- lineage ----


def _ledger(lake, batches):
    meta = lake / "_meta"
    meta.mkdir(parents=True)
    (meta / "ledger.json").write_text(json.dumps({"version": 1, "batches": batches}))


def _batch(table, sha, status="COMPLETE", rows=10, at="2026-01-01"):
    return {
        "table": table,
        "sha256": sha,
        "status": status,
        "rows_cleaned": rows,
        "ingested_at": at,
    }


def test_fingerprint_depends_on_the_content_of_complete_batches_only(tmp_path):
    a = tmp_path / "a"
    _ledger(a, [_batch("transactions", "aa", rows=5), _batch("labels", "bb", rows=7)])
    b = tmp_path / "b"  # same files, other order and other ingestion dates
    _ledger(
        b, [_batch("labels", "bb", rows=7, at="2027-05-05"), _batch("transactions", "aa", rows=5)]
    )
    c = tmp_path / "c"  # one source file differs
    _ledger(c, [_batch("transactions", "aX"), _batch("labels", "bb")])
    d = tmp_path / "d"  # an extra batch that was rejected does not count
    _ledger(
        d,
        [_batch("transactions", "aa"), _batch("labels", "bb"), _batch("labels", "zz", "REJECTED")],
    )
    fa = data_fingerprint(a)
    assert fa["sha256"] == data_fingerprint(b)["sha256"] == data_fingerprint(d)["sha256"]
    assert fa["sha256"] != data_fingerprint(c)["sha256"]
    assert fa["batches"] == 2 and fa["tables"]["transactions"]["rows_cleaned"] == 5


def test_an_empty_lake_has_a_fingerprint_and_no_batches(tmp_path):
    fp = data_fingerprint(tmp_path / "nothing")
    assert fp["batches"] == 0 and len(fp["sha256"]) == 64


def test_code_version_outside_a_repository_is_unknown(tmp_path):
    assert code_version(tmp_path) == {"git_commit": "unknown", "git_dirty": "unknown"}


def test_environment_names_libraries_not_people_or_machines():
    env = environment()
    assert {"python", "os", "polars", "numpy"} <= set(env)
    assert set(env) <= {"python", "os", "polars", "numpy", "scikit-learn", "lightgbm", "matplotlib",
                        "duckdb", "mlflow", "shap"}  # fmt: skip


# ---- collecting what the steps saved ----


def _models(tmp_path, with_explain=True):
    base = tmp_path / "baselines"
    adv = tmp_path / "advanced"
    base.mkdir(parents=True)
    adv.mkdir(parents=True)
    point = {
        "pr_auc": 0.5,
        "roc_auc": 0.9,
        "precision@100": 0.3,
        "recall@100": 0.6,
        "recall@10": 0.1,
    }
    (base / "baseline_metrics.json").write_text(json.dumps({
        "validation": {"rules": {"point": {**point, "pr_auc": 0.1}, "ci": {}}},
        "test": {"rules": {"point": {**point, "pr_auc": 0.2}, "ci": {}}},
        "meta": {"selected": {"random_forest": {"n_estimators": 300, "class_weight": None}}},
    }))  # fmt: skip
    (adv / "advanced_metrics.json").write_text(json.dumps({
        "test_pr_auc": {"lightgbm": 0.55}, "validation_pr_auc": {"lightgbm": 0.45},
        "cost_per_10k": {"lightgbm": {"point": 1234.5, "ci": [1, 2]}, "naive": {"point": 99.0}},
        "calibration": {"ece": {"lightgbm": 0.01}, "brier": {"lightgbm calibrated": 0.002}},
        "calibrators": {"lightgbm": {"a": 1.1, "b": -2.0, "link": "logit"}},
    }))  # fmt: skip
    (adv / "risk_model.json").write_text(json.dumps({
        "feature_version": "1.0", "rounds": 300,
        "scorer_params": {"num_leaves": 8}, "cost_model_ASSUMED": {"review_cost": 25.0},
    }))  # fmt: skip
    if with_explain:
        (adv / "explain_metrics.json").write_text(json.dumps({
            "additivity_error_max": 1e-14, "n_flagged": 700, "agreement": {"top10": 9},
            "permutation": [{"unit": "u", "drop": 0.2}, {"unit": "v", "drop": 0.1}],
            "ablation": [{"unit": "segment", "delta": -0.015}],
            "split_summary": [{"part": "test", "rows": 100, "fraud": 4}],
            "split_bounds": {"val_start": "2025-08-18"},
        }))  # fmt: skip
    return tmp_path


def test_collect_reads_the_right_numbers_under_stable_names(tmp_path):
    params, metrics = collect(_models(tmp_path))
    assert metrics["baseline/test/rules/pr_auc"] == 0.2
    assert metrics["baseline/validation/rules/pr_auc"] == 0.1
    assert "baseline/test/rules/recall@10" not in metrics  # only the chosen few
    assert metrics["test_pr_auc/lightgbm"] == 0.55 and metrics["validation_pr_auc/lightgbm"] == 0.45
    assert metrics["assumed_cost_per_10k/lightgbm"] == 1234.5
    assert metrics["assumed_cost_per_10k/naive"] == 99.0
    assert metrics["calibration_ece/lightgbm"] == 0.01
    assert metrics["calibration_brier/lightgbm calibrated"] == 0.002
    assert metrics["calibrator_a/lightgbm"] == 1.1 and metrics["calibrator_b/lightgbm"] == -2.0
    assert metrics["explain/permutation_top_drop"] == 0.2
    assert metrics["explain/ablation_delta/segment"] == -0.015
    assert metrics["explain/agreement/top10"] == 9.0
    assert params["lightgbm/num_leaves"] == 8 and params["lightgbm/rounds"] == 300
    assert params["assumed_review_cost"] == 25.0 and params["calibrator_link/lightgbm"] == "logit"
    assert params["baseline_selected/random_forest/n_estimators"] == 300
    assert params["split/test/rows"] == 100 and params["split/val_start"] == "2025-08-18"
    assert all(isinstance(v, float) for v in metrics.values())


def test_collect_copes_with_steps_that_have_not_run(tmp_path):
    assert collect(tmp_path) == ({}, {})
    params, metrics = collect(_models(tmp_path, with_explain=False))
    assert "explain/n_flagged" not in metrics and "test_pr_auc/lightgbm" in metrics


# ---- where a run writes ----


def test_a_quick_run_never_defaults_to_the_full_runs_folders(tmp_path):
    assert [str(p) for p in resolve_dirs(False, None, None)] == ["models", "docs"]
    models, docs = resolve_dirs(True, None, None)
    assert (str(models), str(docs)) == (str(resolve_dirs(True, None, None)[0]), str(docs))
    assert models.parts == ("models", "quick") and docs.parts == ("docs", "quick")
    given = resolve_dirs(True, tmp_path / "m", tmp_path / "d")
    assert given == (tmp_path / "m", tmp_path / "d")  # an explicit folder is used as given


# ---- stage handling, with every stage replaced by a stub ----


def _cfg(tmp_path):
    lake = tmp_path / "lake"
    _ledger(lake, [_batch("transactions", "aa")])
    return PipelineConfig(lake=lake, source=tmp_path / "src", models_dir=tmp_path / "m",
                          docs_dir=tmp_path / "d", run_name="unit")  # fmt: skip


def _stubs(order, fail_at=None, error=None):
    error = error or RuntimeError("boom")

    def make(name):
        def stub():
            order.append(name)
            if name == fail_at:
                raise error

        return stub

    return {name: make(name) for name in STAGES}


def test_stages_run_in_order_and_the_run_is_recorded(tmp_path):
    cfg = _cfg(tmp_path)
    cfg.models_dir.mkdir()
    order: list[str] = []
    t = NullTracker()
    res = run_pipeline(cfg, t, stages=_stubs(order))
    assert order == list(STAGES) and t.status == "FINISHED" and res.run_name == "unit"
    assert set(res.stage_seconds) == set(STAGES)
    assert t.params["data/fingerprint_sha256"] == data_fingerprint(cfg.lake)["sha256"]
    assert t.params["config/seed"] == 42 and t.tags["synthetic_data"] == "true"
    assert t.metrics["seconds/total"] == pytest.approx(sum(res.stage_seconds.values()))
    manifest = json.loads(res.manifest_path.read_text())
    assert manifest["data"]["batches"] == 1 and manifest["config"]["quick"] is False


def test_a_failing_stage_marks_the_run_failed_and_names_the_stage(tmp_path):
    cfg = _cfg(tmp_path)
    order: list[str] = []
    t = NullTracker()
    with pytest.raises(RuntimeError, match="boom"):
        run_pipeline(cfg, t, stages=_stubs(order, fail_at="advanced"))
    assert order == ["features", "baselines", "advanced"]  # nothing runs after the failure
    assert t.status == "FAILED"
    assert t.tags["failed_stage"] == "advanced" and t.tags["error_type"] == "RuntimeError"
    assert "seconds/baselines" in t.metrics and "seconds/advanced" in t.metrics


def test_an_interrupted_run_is_marked_killed_not_finished(tmp_path):
    t = NullTracker()
    with pytest.raises(KeyboardInterrupt):
        run_pipeline(
            _cfg(tmp_path), t, stages=_stubs([], fail_at="explain", error=KeyboardInterrupt())
        )
    assert t.status == "KILLED" and t.tags["failed_stage"] == "explain"


def test_a_missing_lake_stops_before_anything_is_recorded(tmp_path):
    t = NullTracker()
    cfg = PipelineConfig(lake=tmp_path / "none", models_dir=tmp_path / "m", docs_dir=tmp_path / "d")
    with pytest.raises(PipelineError, match="ingestion"):
        run_pipeline(cfg, t)
    assert t.status is None and t.run_name is None
