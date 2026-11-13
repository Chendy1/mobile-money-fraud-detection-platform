# ruff: noqa: F811, F401
"""Phase 25: the demo's real commands, run as subprocesses against a generated lake and model."""

import shutil
from pathlib import Path

import pytest
from test_api import COLUMNS, data, env, features, model, model_dir, ordered, wire  # noqa: F401

from mmrisk.demo import Paths
from mmrisk.demo import runner as R
from mmrisk.demo.cli import main
from mmrisk.demo.steps import build_steps, select
from mmrisk.registry import Registry

REPO = Path(__file__).resolve().parents[2]
RUNNABLE = (
    "prereq",
    "quality",
    "card",
    "apicheck",
    "secure",
    "simulate",
    "monitor",
    "dashcheck",
    "cost",
)


@pytest.fixture(scope="module")
def demo_paths(env, model_dir, tmp_path_factory):
    repo = tmp_path_factory.mktemp("demo-repo")
    registry = repo / "models" / "registry"
    reg = Registry(registry)
    version = reg.register(model_dir, source="bootstrap", stage="approved")
    reg.promote(version, "demo test")
    (repo / "docs").mkdir()
    shutil.copy(REPO / "docs" / "model-card.md", repo / "docs" / "model-card.md")
    (repo / "infrastructure" / "cost").mkdir(parents=True)
    shutil.copy(REPO / "infrastructure/cost/prices.json", repo / "infrastructure/cost/prices.json")
    return Paths(repo=repo, lake=env[1], model_dir=model_dir, registry=registry)


def snapshot(paths):
    return {
        p
        for root in (paths.repo, paths.lake, paths.model_dir)
        for p in root.rglob("*")
        if p.is_file()
    }


@pytest.fixture(scope="module")
def before(demo_paths):
    return snapshot(demo_paths)


@pytest.fixture(scope="module")
def results(demo_paths, before):
    # prereq looks for the model bundle file the real pipeline writes; the test model has one too
    steps = [s for s in select(build_steps(demo_paths), "standard") if s.id in RUNNABLE]
    return {r.id: r for r in R.run_demo(steps, demo_paths, pause=False, out=lambda s: None)}


@pytest.mark.parametrize("step_id", RUNNABLE)
def test_each_real_step_runs_and_shows_what_it_claims(results, step_id):
    r = results[step_id]
    assert r.status == "ok", f"{step_id}: {r.status} {r.detail}\n{r.output[-1500:]}"


def test_the_secure_step_reports_every_boundary_and_stops_its_server(results):
    out = results["secure"].output
    assert "no key: /v1/model is refused" in out and "plain HTTP is refused" in out
    assert "FAIL" not in out and "checks passed" in out


def test_the_demo_wrote_only_under_its_own_folder(demo_paths, before, results):
    after = snapshot(demo_paths)
    new = after - before
    assert new, "the demo should have written something"
    repo = demo_paths.repo
    stray = [p for p in new if (repo / "data" / "demo") not in p.parents]
    assert stray == []  # nothing new in the lake (`--no-write` held), the registry or the repo


def test_the_command_line_runs_steps_by_id_and_returns_zero(demo_paths, results, capsys):
    code = main(["run", "--yes", "--only", "card,cost", "--repo", str(demo_paths.repo)])
    out = capsys.readouterr().out
    assert code == 0 and "2 of 2 steps ok" in out


def test_clean_after_a_run_removes_the_outputs(demo_paths, results):
    assert (demo_paths.repo / "data" / "demo").exists()
    assert main(["clean", "--repo", str(demo_paths.repo)]) == 0
    assert not (demo_paths.repo / "data" / "demo").exists()
