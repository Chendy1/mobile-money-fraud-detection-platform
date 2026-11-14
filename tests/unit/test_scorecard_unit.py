"""Phase 27: the scorecard computes levels from evidence and refuses to drift from it."""

import json
import re
from pathlib import Path

import pytest

from mmrisk.scorecard.cli import main
from mmrisk.scorecard.model import ScorecardError, evaluate_all, load
from mmrisk.scorecard.render import render, summary_counts

ROOT = Path(__file__).resolve().parents[2]
CI_YML = "jobs:\n  lint-and-test:\n    runs-on: x\n  docker-build:\n    runs-on: x\n"


def area(**over):
    base = dict(
        id="a", name="A", phases="1", summary="s", code=["src/a.py"], tests=["tests/t.py"],
        ci_jobs=["lint-and-test"], last_phase=3, last_change="x", uses_windows_suite=False,
        author_run=None, real_world=None, ran_for_real="no", real_note="n", limits=["l"], next="n",
    )  # fmt: skip
    base.update(over)
    return base


def make_repo(tmp_path, areas, ci_phase=5, suite_phase=4, files=("src/a.py", "tests/t.py")):
    for f in files:
        p = tmp_path / f
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x", encoding="utf-8")
    (tmp_path / ".github" / "workflows").mkdir(parents=True)
    (tmp_path / ".github" / "workflows" / "ci.yml").write_text(CI_YML, encoding="utf-8")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "v.md").write_text("run 777 and 99 passed and ran-it", encoding="utf-8")
    data = dict(
        ci=dict(run_id="777", covers_through_phase=ci_phase, source="docs/v.md", match="777"),
        windows_suite=dict(
            what="suite", as_of_phase=suite_phase, source="docs/v.md", match="99 passed"
        ),
        areas=areas,
    )  # fmt: skip
    (tmp_path / "docs" / "scorecard.json").write_text(json.dumps(data), encoding="utf-8")
    return tmp_path / "docs" / "scorecard.json"


def levels(tmp_path, **kw):
    path = make_repo(tmp_path, kw.pop("areas"), **kw)
    _, res = evaluate_all(path, tmp_path)
    return res


def test_code_missing_is_e0(tmp_path):
    (r,) = levels(tmp_path, areas=[area()], files=("tests/t.py",))
    assert r.level == 0


def test_code_only_is_e1(tmp_path):
    (r,) = levels(tmp_path, areas=[area(tests=[])])
    assert r.level == 1


def test_listed_test_that_does_not_exist_is_a_problem(tmp_path):
    (r,) = levels(tmp_path, areas=[area(tests=["tests/nope.py"])])
    assert r.level == 1 and any("nope.py" in p for p in r.problems)


def test_changed_after_ci_stays_e2(tmp_path):
    (r,) = levels(tmp_path, areas=[area(last_phase=6)], ci_phase=5)
    assert r.level == 2 and "after the latest green CI run" in r.notes[0]


def test_ci_covering_the_change_is_e3(tmp_path):
    (r,) = levels(tmp_path, areas=[area(last_phase=5)], ci_phase=5)
    assert r.level == 3 and "E4 missing" in r.notes[0]


def test_unknown_ci_job_blocks_e3_and_is_a_problem(tmp_path):
    (r,) = levels(tmp_path, areas=[area(ci_jobs=["ghost"])])
    assert r.level == 2 and any("ghost" in p for p in r.problems)


def test_no_ci_jobs_named_blocks_e3(tmp_path):
    (r,) = levels(tmp_path, areas=[area(ci_jobs=[])])
    assert r.level == 2


def test_ci_run_not_recorded_blocks_e3(tmp_path):
    path = make_repo(tmp_path, [area()])
    (tmp_path / "docs" / "v.md").write_text("nothing here", encoding="utf-8")
    _, (r,) = evaluate_all(path, tmp_path)
    assert r.level == 2 and r.problems


def test_windows_suite_lifts_to_e4_only_if_recent_enough(tmp_path):
    (r,) = levels(tmp_path, areas=[area(uses_windows_suite=True, last_phase=4)], suite_phase=4)
    assert r.level == 4
    (r,) = levels(
        tmp_path / "b", areas=[area(uses_windows_suite=True, last_phase=5)], suite_phase=4
    )
    assert r.level == 3


def test_windows_suite_not_used_unless_the_area_says_so(tmp_path):
    (r,) = levels(tmp_path, areas=[area(uses_windows_suite=False, last_phase=2)], suite_phase=9)
    assert r.level == 3


def test_author_run_lifts_to_e4_and_is_checked_in_its_source(tmp_path):
    run = dict(what="w", as_of_phase=3, source="docs/v.md", match="ran-it")
    (r,) = levels(tmp_path, areas=[area(author_run=run, last_phase=3)])
    assert r.level == 4
    bad = dict(run, match="never written")
    (r,) = levels(tmp_path / "b", areas=[area(author_run=bad, last_phase=3)])
    assert r.level == 3 and any("never written" in p for p in r.problems)


def test_author_run_before_the_last_change_does_not_count(tmp_path):
    run = dict(what="w", as_of_phase=2, source="docs/v.md", match="ran-it")
    (r,) = levels(tmp_path, areas=[area(author_run=run, last_phase=3)])
    assert r.level == 3


def test_e5_needs_an_outside_check_and_a_lower_rung(tmp_path):
    run = dict(what="w", as_of_phase=3, source="docs/v.md", match="ran-it")
    (r,) = levels(tmp_path, areas=[area(author_run=run, real_world="an outside review")])
    assert r.level == 5
    (r,) = levels(tmp_path / "b", areas=[area(real_world="an outside review")])
    assert r.level == 3  # a claim of outside review does not skip rungs


def test_malformed_data_is_an_error_not_a_low_score(tmp_path):
    path = make_repo(tmp_path, [area(ran_for_real="maybe")])
    with pytest.raises(ScorecardError):
        load(path)
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(ScorecardError):
        load(path)
    path = make_repo(tmp_path / "b", [area(), area()])
    with pytest.raises(ScorecardError):
        load(path)


def test_render_is_deterministic_and_matches_the_counts(tmp_path):
    path = make_repo(tmp_path, [area(), area(id="b", name="B", last_phase=9)])
    ev, res = evaluate_all(path, tmp_path)
    assert render(ev, res) == render(ev, res)
    text = render(ev, res)
    c = summary_counts(res)
    assert f"2 areas. E0: 0, E1: 0, E2: {c[2]}, E3: {c[3]}" in text


def test_check_passes_then_detects_a_stale_page_and_a_problem(tmp_path, capsys):
    make_repo(tmp_path, [area()])
    assert main(["check", "--repo", str(tmp_path)]) == 1  # no page yet
    assert main(["render", "--repo", str(tmp_path)]) == 0
    assert main(["check", "--repo", str(tmp_path)]) == 0
    page = tmp_path / "docs" / "scorecard.md"
    page.write_text(page.read_text(encoding="utf-8").replace("E3", "E4", 1), encoding="utf-8")
    assert main(["check", "--repo", str(tmp_path)]) == 1
    assert "STALE" in capsys.readouterr().out
    main(["render", "--repo", str(tmp_path)])
    (tmp_path / "docs" / "v.md").write_text("gone", encoding="utf-8")
    assert main(["check", "--repo", str(tmp_path)]) == 1
    assert "PROBLEM" in capsys.readouterr().out


def test_check_reports_a_broken_data_file(tmp_path):
    make_repo(tmp_path, [area()])
    (tmp_path / "docs" / "scorecard.json").write_text("{", encoding="utf-8")
    assert main(["check", "--repo", str(tmp_path)]) == 2


# ---- the real repository ----------------------------------------------------------------------


@pytest.fixture(scope="module")
def real():
    ev, res = evaluate_all(ROOT / "docs" / "scorecard.json", ROOT)
    return ev, res, {r.area.id: r for r in res}


def test_real_scorecard_has_no_problems_and_a_current_page(real):
    ev, res, _ = real
    assert [p for r in res for p in r.problems] == []
    page = (ROOT / "docs" / "scorecard.md").read_text(encoding="utf-8")
    assert page.replace("\r\n", "\n") == render(ev, res)


def test_no_area_claims_the_top_rung(real):
    _, res, _ = real
    assert all(r.level <= 4 for r in res) and all(not r.area.real_world for r in res)


def test_areas_never_run_are_not_at_the_author_run_level(real):
    _, _, by = real
    for name in ("prometheus-grafana", "dashboard", "aws-terraform"):
        assert by[name].level <= 3 and by[name].area.ran_for_real == "no"


def test_ci_run_in_the_data_is_the_one_the_verification_page_calls_latest(real):
    ev, _, _ = real
    text = (ROOT / "docs" / "verification-status.md").read_text(encoding="utf-8")
    assert re.search(r"Latest green run: \*\*" + ev.ci_run_id + r"\*\*", text)


def test_every_test_file_in_the_repo_belongs_to_some_area_or_is_cross_cutting(real):
    _, res, _ = real
    listed = {t for r in res for t in r.area.tests}
    unit = {f"tests/unit/{p.name}" for p in (ROOT / "tests" / "unit").glob("test_*.py")}
    unlisted = sorted(unit - listed)
    # cross-cutting guards that belong to no single area
    allowed = {"tests/unit/test_scorecard_unit.py", "tests/unit/test_secrets_unit.py"}
    stray = [
        u for u in unlisted if u not in allowed and "monitoring" not in u and "dashboard" not in u
    ]
    assert stray == [], f"test files no area claims (add them to docs/scorecard.json): {stray}"


def test_resume_bullets_only_rest_on_areas_that_exist(real):
    _, res, by = real
    resume = (ROOT / "docs" / "resume.md").read_text(encoding="utf-8")
    bullets = set(re.findall(r"^- \*\*(B\d+)\*\*", resume, flags=re.M))
    claimed = {b for r in res for b in r.area.resume}
    assert claimed == bullets, (
        f"bullets without an area, or areas naming a missing bullet: {claimed ^ bullets}"
    )


def test_the_cloud_bullet_rests_only_on_areas_never_run_for_real(real):
    _, res, _ = real
    b12 = [r for r in res if "B12" in r.area.resume]
    assert b12 and all(r.level < 4 and r.area.ran_for_real != "yes" for r in b12)


def test_the_security_bullet_has_an_area_run_on_the_authors_machine(real):
    _, res, _ = real
    assert any("B7" in r.area.resume and r.level >= 4 for r in res)


def test_summary_line_in_the_page_matches_the_table(real):
    _, res, _ = real
    page = (ROOT / "docs" / "scorecard.md").read_text(encoding="utf-8")
    rows = [ln for ln in page.splitlines() if re.match(r"\| .* \| E\d \| (yes|partly|no) \|", ln)]
    assert len(rows) == len(res)
    for k, n in summary_counts(res).items():
        assert sum(1 for ln in rows if f"| E{k} |" in ln) == n


def test_scorecard_page_is_ascii_and_free_of_banned_wording():
    page = (ROOT / "docs" / "scorecard.md").read_text(encoding="utf-8")
    assert page.isascii()
    low = page.lower()
    for bad in ("production-grade", "state-of-the-art", "deployed to aws", "in production"):
        assert bad not in low
