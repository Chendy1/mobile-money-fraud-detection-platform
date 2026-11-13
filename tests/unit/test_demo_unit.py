"""Phase 25: the demo's step table, runner and actions (no subprocess, no real model)."""

import importlib.util
from pathlib import Path

import pytest

from mmrisk.demo import DemoError, Paths, Step
from mmrisk.demo import runner as R
from mmrisk.demo.cli import main
from mmrisk.demo.steps import TRACKS, build_steps, select

REPO = Path(__file__).resolve().parents[2]
P = Paths(repo=REPO)
STEPS = build_steps(P)


def step(**kw):
    base = {"id": "x", "title": "t", "seconds": 1, "say": ("s",), "commands": (("m", "a"),)}
    return Step(**{**base, **kw})


def fake(*replies):
    """A runner that returns the given (code, text) pairs in order and records the calls."""
    calls, queue = [], list(replies)

    def run(argv, cwd, timeout):
        calls.append(argv)
        return queue.pop(0)

    run.calls = calls
    return run


# ---- the step table ----------------------------------------------------------------------------


def test_ids_are_unique_and_every_step_has_a_title_talking_points_and_something_to_run():
    ids = [s.id for s in STEPS]
    assert len(ids) == len(set(ids))
    for s in STEPS:
        assert s.title and s.say and s.seconds >= 0
        assert bool(s.commands) != bool(s.action), s.id  # exactly one of the two
        assert s.action == "" or s.action in R.ACTIONS


def test_every_command_names_a_module_that_exists():
    for s in STEPS:
        for argv in s.commands:
            assert importlib.util.find_spec(argv[0]) is not None, f"{s.id}: {argv[0]}"


def test_needs_point_to_earlier_steps_and_the_short_track_has_no_unmet_need():
    seen = set()
    for s in STEPS:
        assert set(s.needs) <= seen, f"{s.id} needs a step that comes later or does not exist"
        seen.add(s.id)
    short = {s.id for s in select(STEPS, "short")}
    for s in select(STEPS, "short"):
        assert set(s.needs) <= short, f"short track runs {s.id} without {s.needs}"


def test_the_short_track_is_a_strict_subset_of_the_standard_track():
    short = [s.id for s in select(STEPS, "short")]
    standard = [s.id for s in select(STEPS, "standard")]
    assert set(short) < set(standard) and standard == [s.id for s in STEPS]
    assert set(TRACKS) == {"short", "standard"}


def test_select_by_id_keeps_the_script_order_and_rejects_unknown_ids_and_tracks():
    got = [s.id for s in select(STEPS, "standard", ("cost", "quality"))]
    assert got == ["quality", "cost"]
    with pytest.raises(DemoError, match="unknown step"):
        select(STEPS, "standard", ("nope",))
    with pytest.raises(DemoError, match="unknown track"):
        select(STEPS, "long")


def test_every_step_writes_only_under_the_demo_folder():
    out = str(P.out)
    for s in STEPS:
        for argv in s.commands:
            for flag in ("--out", "--store", "--report"):
                if flag in argv:
                    assert argv[argv.index(flag) + 1].startswith(out), f"{s.id} {flag}"
    quality = next(s for s in STEPS if s.id == "quality").commands[0]
    assert "--no-write" in quality  # the quality check would otherwise write into the lake


def test_the_drift_demonstration_expects_the_detector_to_fire():
    s = next(s for s in STEPS if s.id == "drift_shift")
    assert s.ok_codes == (1,) and "--scale-amounts" in s.commands[-1]


# ---- running one step --------------------------------------------------------------------------


def test_a_command_that_exits_zero_and_prints_the_marker_is_ok():
    r = R.run_step(step(expect=(r"all good",)), P, fake((0, "all good\n")))
    assert r.status == "ok" and "all good" in r.output


def test_an_unexpected_exit_code_is_a_failure_and_says_which_code():
    r = R.run_step(step(), P, fake((2, "boom")))
    assert r.status == "failed" and "exit code 2" in r.output


def test_exit_code_one_is_ok_only_where_the_step_allows_it():
    assert R.run_step(step(ok_codes=(0, 1)), P, fake((1, "x"))).status == "ok"
    assert R.run_step(step(ok_codes=(1,)), P, fake((0, "x"))).status == "failed"


def test_output_that_lacks_the_expected_marker_is_reported_as_unexpected_not_ok():
    r = R.run_step(step(expect=(r"PASS",)), P, fake((0, "something else")))
    assert r.status == "unexpected" and "PASS" in r.detail


def test_every_marker_must_match():
    r = R.run_step(step(expect=(r"alpha", r"beta")), P, fake((0, "alpha only")))
    assert r.status == "unexpected" and "beta" in r.detail


def test_a_later_command_is_not_run_after_an_earlier_one_fails():
    run = fake((1, "first failed"), (0, "second"))
    r = R.run_step(step(commands=(("m", "one"), ("m", "two"))), P, run)
    assert r.status == "failed" and len(run.calls) == 1


def test_all_commands_of_a_multi_command_step_run_and_their_output_is_joined():
    run = fake((0, "first"), (0, "second"))
    r = R.run_step(step(commands=(("m", "one"), ("m", "two"))), P, run)
    assert r.status == "ok" and "first" in r.output and "second" in r.output
    assert run.calls == [["m", "one"], ["m", "two"]]


def test_an_action_that_raises_is_reported_not_raised(monkeypatch):
    def boom(p):
        raise RuntimeError("no luck")

    monkeypatch.setitem(R.ACTIONS, "boom", boom)
    r = R.run_step(step(commands=(), action="boom"), P)
    assert r.status == "failed" and "RuntimeError: no luck" in r.output


# ---- running the whole script ------------------------------------------------------------------


def test_the_loop_prints_talking_points_pauses_before_each_step_and_summarises():
    steps = [step(id="a", say=("say a",)), step(id="b", say=("say b",))]
    said, asked = [], []
    results = R.run_demo(
        steps, P, runner=fake((0, "oa"), (0, "ob")), input_fn=lambda q: asked.append(q) or "",
        out=said.append,
    )  # fmt: skip
    text = "\n".join(said)
    assert "SAY: say a" in text and "SAY: say b" in text and "oa" in text
    assert len(asked) == 2 and [r.status for r in results] == ["ok", "ok"]
    assert "2 of 2 steps ok" in R.summary(results)


def test_no_pause_means_input_is_never_asked():
    def never(_):
        raise AssertionError("asked")

    R.run_demo([step()], P, pause=False, runner=fake((0, "x")), input_fn=never, out=lambda s: None)


def test_a_step_whose_dependency_failed_is_skipped_not_run():
    steps = [step(id="a"), step(id="b", needs=("a",))]
    run = fake((1, "a broke"))
    results = R.run_demo(steps, P, pause=False, runner=run, out=lambda s: None)
    assert [r.status for r in results] == ["failed", "skipped"] and len(run.calls) == 1
    assert "not ok: a, b" in R.summary(results)


def test_long_output_is_trimmed_with_a_count_of_what_was_left_out():
    text = "\n".join(f"line {i}" for i in range(50))
    out = R.trim(text, 10)
    assert (
        out.splitlines()[-1] == "... (40 more lines)" and "line 9" in out and "line 10" not in out
    )
    assert R.trim("a\nb", 10) == "a\nb"


# ---- the built-in actions ----------------------------------------------------------------------


def test_the_card_action_copies_the_table_from_the_model_card_and_never_retypes_it():
    ok, text = R.action_card(P)
    assert ok and "LightGBM (shipped)" in text and "Hand-written rules" in text
    card = (REPO / "docs" / "model-card.md").read_text(encoding="utf-8")
    for line in text.splitlines():
        if line.startswith("|"):
            assert line in card  # every table line is a line of the card, character for character


def test_the_card_action_fails_without_a_card(tmp_path):
    ok, text = R.action_card(Paths(repo=tmp_path))
    assert not ok and "missing" in text


def test_prereq_names_what_is_missing_and_how_to_make_it(tmp_path):
    ok, text = R.action_prereq(Paths(repo=tmp_path))
    assert not ok
    assert text.count("MISSING") == 5 and "dev.ps1 regboot" in text and "dev.ps1 advanced" in text


def test_prereq_passes_when_everything_is_there(tmp_path):
    for rel in (
        "data/processed/cleaned/x",
        "models/advanced/risk_model.json",
        "models/registry/production.json",
        "docs/model-card.md",
        "infrastructure/cost/prices.json",
    ):
        f = tmp_path / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("x")
    ok, text = R.action_prereq(Paths(repo=tmp_path))
    assert ok and "MISSING" not in text


# ---- the command line --------------------------------------------------------------------------


def test_list_prints_every_step_and_its_talking_points_without_running_anything(capsys):
    assert main(["list", "--repo", str(REPO)]) == 0
    out = capsys.readouterr().out
    for s in STEPS:
        assert s.id in out and s.say[0] in out


def test_an_unknown_step_is_a_mistake_exit_code_two(capsys):
    assert main(["run", "--yes", "--only", "nope", "--repo", str(REPO)]) == 2
    assert "unknown step" in capsys.readouterr().err


def test_check_exits_one_and_names_the_fix_when_a_prerequisite_is_missing(tmp_path, capsys):
    assert main(["check", "--repo", str(tmp_path)]) == 1
    assert "fix:" in capsys.readouterr().out


def test_clean_removes_only_the_demo_folder(tmp_path):
    keep = tmp_path / "data" / "processed" / "keep.txt"
    gone = tmp_path / "data" / "demo" / "predictions" / "p.parquet"
    for f in (keep, gone):
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("x")
    assert main(["clean", "--repo", str(tmp_path)]) == 0
    assert keep.exists() and not (tmp_path / "data" / "demo").exists()


def test_the_secure_summary_is_red_when_any_boundary_check_fails_or_none_ran():
    from mmrisk.deploy.checks import fail, ok

    good = R.summarize_checks([ok("a", "200"), ok("b", "401")])
    assert good[0] and "2 of 2 checks passed" in good[1]
    bad = R.summarize_checks([ok("a", "200"), fail("b", "200, wanted 401")])
    assert not bad[0] and "FAIL  b" in bad[1] and "1 of 2 checks passed" in bad[1]
    assert R.summarize_checks([])[0] is False


def test_the_demo_guide_mentions_every_step_id_and_no_unknown_ones():
    guide = (REPO / "docs" / "demo.md").read_text(encoding="utf-8")
    for s in STEPS:
        assert f"`{s.id}`" in guide, f"docs/demo.md does not mention step {s.id}"
    import re

    mentioned = set(re.findall(r"\| `([a-z_]+)` \|", guide))
    assert mentioned <= {s.id for s in STEPS}, mentioned - {s.id for s in STEPS}


def test_the_guide_timings_agree_with_the_short_track():
    guide = (REPO / "docs" / "demo.md").read_text(encoding="utf-8")
    assert f"{len(select(STEPS, 'short'))} steps" in guide  # "6 steps"
    assert f"{len(STEPS)} steps" in guide  # "12 steps"
