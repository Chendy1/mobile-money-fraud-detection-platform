"""Phase 10 tests: one command trains everything, and the run is tracked on this machine only."""

import json
from datetime import date, datetime

import pytest

pytest.importorskip("sklearn")
pytest.importorskip("lightgbm")
pytest.importorskip("matplotlib")
pytest.importorskip("shap")
mlflow = pytest.importorskip("mlflow")

from mmrisk.generation import GeneratorConfig, generate  # noqa: E402
from mmrisk.ingestion import ingest_source_dir  # noqa: E402
from mmrisk.pipeline import (  # noqa: E402
    MlflowTracker,
    NullTracker,
    PipelineConfig,
    run_pipeline,
)
from mmrisk.pipeline.cli import main as cli  # noqa: E402
from mmrisk.pipeline.run import STAGES  # noqa: E402

CLOCK = lambda: datetime(2026, 3, 1, 12, 0, 0)  # noqa: E731


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    root = tmp_path_factory.mktemp("pipe")
    src, lake = root / "src", root / "lake"
    generate(
        GeneratorConfig(
            num_customers=400, num_transactions=24_000, fraud_rate=0.02, seed=31,
            start_date=date(2025, 1, 1), end_date=date(2025, 12, 31), output_dir=src,
        )
    )  # fmt: skip
    ingest_source_dir(src, lake, CLOCK)
    return root, src, lake


def _cfg(env, name, seed=42, tag=None):
    root, src, lake = env
    return PipelineConfig(
        lake=lake, source=src, models_dir=root / name / "m", docs_dir=root / name / "d",
        seed=seed, quick=True, bootstrap=20, run_name=name,
    )  # fmt: skip


def _stable(d):
    return {k: v for k, v in d.items() if not k.startswith("seconds/")}


@pytest.fixture(scope="module")
def runs(env):
    uri = f"sqlite:///{(env[0] / 'track.db').as_posix()}"
    a = NullTracker()
    res_a = run_pipeline(_cfg(env, "a"), a)
    b = MlflowTracker(uri, "t")
    res_b = run_pipeline(_cfg(env, "b"), b)
    c = NullTracker()
    run_pipeline(_cfg(env, "c", seed=7), c)
    return a, res_a, res_b, c, uri


def test_the_run_is_the_same_whichever_tracker_is_used(runs):
    a, res_a, res_b, _, _ = runs
    assert a.status == "FINISHED" and res_b.run_id and res_a.run_id is None
    assert res_a.params == res_b.params and len(res_a.params) > 30
    ma, mb = _stable(res_a.metrics), _stable(res_b.metrics)
    assert ma.keys() == mb.keys() and len(ma) > 60
    for k in ma:
        assert ma[k] == pytest.approx(mb[k], rel=1e-9, abs=1e-12), k


def test_another_seed_gives_other_numbers(runs):
    a, _, _, c, _ = runs
    assert c.params["config/seed"] == 7
    assert _stable(a.metrics) != _stable(c.metrics)


def test_the_tracked_run_holds_what_a_reviewer_needs(runs):
    _, _, res, _, uri = runs
    client = mlflow.MlflowClient(uri)
    run = client.get_run(res.run_id)
    assert run.info.status == "FINISHED"
    assert run.data.tags["mlflow.user"] == "local"
    assert run.data.tags["mlflow.source.name"] == "mmrisk.pipeline"
    assert run.data.tags["synthetic_data"] == "true"
    assert run.data.params["data/fingerprint_sha256"]
    assert run.data.metrics["test_pr_auc/lightgbm"] == pytest.approx(
        res.metrics["test_pr_auc/lightgbm"]
    )
    top = {a.path for a in client.list_artifacts(res.run_id)}
    assert {"reports", "models", "pipeline_run.json"} <= top
    adv = {a.path for a in client.list_artifacts(res.run_id, "models/advanced")}
    assert "models/advanced/risk_model.joblib" in adv
    assert not any("baselines" in p and p.endswith(".joblib") for p in adv)  # large, not logged
    reports = {a.path for a in client.list_artifacts(res.run_id, "reports")}
    assert "reports/model-card.md" in reports


def test_nothing_identifying_the_machine_is_recorded(runs, env):
    _, _, res, _, uri = runs
    run = mlflow.MlflowClient(uri).get_run(res.run_id)
    blob = json.dumps([run.data.tags, run.data.params])
    assert str(env[0]) not in blob and "/home/" not in blob and "Users" not in blob


def test_the_manifest_matches_the_run(runs):
    _, _, res, _, _ = runs
    m = json.loads(res.manifest_path.read_text())
    assert m["config"]["seed"] == 42 and m["config"]["quick"] is True
    assert set(m["stage_seconds"]) == set(STAGES)


def test_a_failure_in_a_real_run_is_recorded_as_failed(env):
    t = NullTracker()

    def boom():
        raise ValueError("injected")

    with pytest.raises(ValueError):
        run_pipeline(_cfg(env, "f"), t, stages={"features": lambda: None, "baselines": boom})
    assert t.status == "FAILED" and t.tags["failed_stage"] == "baselines"


# ---- the command line ----


def _args(env, name, *extra):
    root, src, lake = env
    m, d = str(root / name / "m"), str(root / name / "d")
    return [
        "run",
        "--lake",
        str(lake),
        "--source",
        str(src),
        "--models-dir",
        m,
        "--docs-dir",
        d,
        "--quick",
        "--bootstrap",
        "20",
        *extra,
    ]


def test_cli_run_then_compare(env, capsys):
    uri = f"sqlite:///{(env[0] / 'cli.db').as_posix()}"
    assert cli(_args(env, "cli1", "--run-name", "first", "--tracking-uri", uri)) == 0
    out = capsys.readouterr().out
    assert "test PR-AUC lightgbm" in out and "ASSUMED" in out and "mlflow ui" in out
    assert cli(["compare", "--tracking-uri", uri]) == 0
    table = capsys.readouterr().out
    assert "first" in table and "FINISHED" in table


def test_cli_without_tracking_writes_no_store(env, tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert cli(_args(env, "cli2", "--no-tracking")) == 0
    assert "tracked in" not in capsys.readouterr().out
    assert not (tmp_path / "mlflow.db").exists() and not (tmp_path / "mlruns").exists()


def test_cli_refuses_a_remote_tracking_server_and_writes_nothing(env, capsys):
    root = env[0]
    code = cli(_args(env, "cli3", "--tracking-uri", "http://mlflow.example.com:5000"))
    assert code == 2 and "local" in capsys.readouterr().err
    assert not (root / "cli3").exists()


def test_cli_reports_user_mistakes_with_exit_code_2(env, tmp_path, capsys):
    assert cli(["run", "--lake", str(tmp_path / "none"), "--no-tracking"]) == 2
    assert "error:" in capsys.readouterr().err
    assert cli(_args(env, "cli4", "--no-tracking", "--review-prevents", "2")) == 2
    assert cli(["compare", "--tracking-uri", "https://x.example"]) == 2


def test_compare_with_nothing_recorded_says_so(tmp_path, capsys):
    uri = f"sqlite:///{(tmp_path / 'empty.db').as_posix()}"
    assert cli(["compare", "--tracking-uri", uri]) == 0
    assert "run the pipeline first" in capsys.readouterr().out


def test_a_quick_run_defaults_to_its_own_folders(env, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    root, src, lake = env
    code = cli(["run", "--lake", str(lake), "--source", str(src), "--quick", "--bootstrap", "20",
                "--no-tracking"])  # fmt: skip
    assert code == 0
    assert (tmp_path / "models" / "quick" / "pipeline_run.json").exists()
    assert (tmp_path / "docs" / "quick" / "model-card.md").exists()
    assert not (tmp_path / "docs" / "model-card.md").exists()
