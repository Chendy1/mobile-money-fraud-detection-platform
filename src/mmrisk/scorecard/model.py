"""The evidence ladder and how a level is computed.

E0  the code is not there
E1  the code is there
E2  E1 and automated tests exist for it (they passed in the assistant's sandbox)
E3  E2 and CI ran them green *after the area's last change* (see `ci.covers_through_phase`)
E4  E3 and the author ran it on the author's own machine after the area's last change
E5  E4 and it was checked against something the project did not write: real data, an outside
    review, real traffic. Not reachable here: the data is synthetic by design (ADR-001).

A rung counts only if every rung below it holds and its own evidence checks out: files exist,
and the text a claim relies on is found in the document that is cited for it.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

LEVELS = {
    0: "not built",
    1: "built",
    2: "built and tested in the sandbox",
    3: "tests green in CI after the last change",
    4: "run on the author's machine after the last change",
    5: "checked against something the project did not write",
}


class ScorecardError(Exception):
    """The data file is malformed (a mistake in the file, not a low score)."""


@dataclass(frozen=True)
class AuthorRun:
    what: str
    as_of_phase: int
    source: str
    match: str


@dataclass(frozen=True)
class Area:
    id: str
    name: str
    phases: str
    summary: str
    code: tuple[str, ...]
    tests: tuple[str, ...]
    ci_jobs: tuple[str, ...]
    last_phase: int
    last_change: str
    uses_windows_suite: bool
    author_run: AuthorRun | None
    real_world: str | None
    ran_for_real: str  # yes | partly | no
    real_note: str
    limits: tuple[str, ...]
    next: str
    resume: tuple[str, ...] = ()


@dataclass(frozen=True)
class Evidence:
    """Project-wide evidence shared by many areas."""

    ci_run_id: str
    ci_covers_through_phase: int
    ci_source: str
    ci_match: str
    suite_what: str
    suite_as_of_phase: int
    suite_source: str
    suite_match: str


@dataclass
class Evaluation:
    area: Area
    level: int
    notes: list[str] = field(default_factory=list)  # why each missing rung is missing
    problems: list[str] = field(
        default_factory=list
    )  # evidence cited but not found: a bug in the data


def load(path: Path) -> tuple[Evidence, list[Area]]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        ci, suite = raw["ci"], raw["windows_suite"]
        ev = Evidence(
            str(ci["run_id"]), int(ci["covers_through_phase"]), ci["source"], ci["match"],
            suite["what"], int(suite["as_of_phase"]), suite["source"], suite["match"],
        )  # fmt: skip
        areas = []
        for a in raw["areas"]:
            run = a.get("author_run")
            areas.append(
                Area(
                    id=a["id"], name=a["name"], phases=a["phases"], summary=a["summary"],
                    code=tuple(a["code"]), tests=tuple(a["tests"]), ci_jobs=tuple(a["ci_jobs"]),
                    last_phase=int(a["last_phase"]), last_change=a["last_change"],
                    uses_windows_suite=bool(a["uses_windows_suite"]),
                    author_run=AuthorRun(run["what"], int(run["as_of_phase"]), run["source"], run["match"]) if run else None,
                    real_world=a.get("real_world"), ran_for_real=a["ran_for_real"], real_note=a["real_note"],
                    limits=tuple(a["limits"]), next=a["next"], resume=tuple(a.get("resume", ())),
                )
            )  # fmt: skip
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ScorecardError(f"{path}: {exc.__class__.__name__}: {exc}") from exc
    ids = [a.id for a in areas]
    if len(ids) != len(set(ids)):
        raise ScorecardError("duplicate area id")
    for a in areas:
        if a.ran_for_real not in ("yes", "partly", "no"):
            raise ScorecardError(f"{a.id}: ran_for_real must be yes, partly or no")
    return ev, areas


def _workflow_jobs(repo: Path) -> set[str]:
    text = (repo / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    return set(re.findall(r"^  ([a-z][a-z0-9-]*):\s*$", text, flags=re.M)) - {
        "push",
        "pull_request",
    }


def _found(repo: Path, source: str, match: str) -> bool:
    p = repo / source
    return p.is_file() and match in p.read_text(encoding="utf-8")


def evaluate(area: Area, ev: Evidence, repo: Path) -> Evaluation:
    out = Evaluation(area, 0)
    missing_code = [c for c in area.code if not (repo / c).exists()]
    if missing_code or not area.code:
        out.notes.append("E1 missing: " + (", ".join(missing_code) or "no code listed"))
        return out
    out.level = 1
    missing_tests = [t for t in area.tests if not (repo / t).is_file()]
    if missing_tests:
        out.problems.append("tests listed but not found: " + ", ".join(missing_tests))
    if not area.tests or missing_tests:
        out.notes.append("E2 missing: no automated tests found")
        return out
    out.level = 2

    jobs = _workflow_jobs(repo)
    unknown = [j for j in area.ci_jobs if j not in jobs]
    if unknown:
        out.problems.append("CI jobs not in ci.yml: " + ", ".join(unknown))
    if not _found(repo, ev.ci_source, ev.ci_match):
        out.problems.append(f"CI run {ev.ci_run_id} not found in {ev.ci_source}")
    if not area.ci_jobs or unknown:
        out.notes.append("E3 missing: no CI job named for this area")
        return out
    if area.last_phase > ev.ci_covers_through_phase:
        out.notes.append(
            f"E3 missing: last changed in Phase {area.last_phase}, after the latest green CI run "
            f"({ev.ci_run_id}, through Phase {ev.ci_covers_through_phase})"
        )
        return out
    if not _found(repo, ev.ci_source, ev.ci_match):
        out.notes.append("E3 missing: the CI run is not recorded where it is cited")
        return out
    out.level = 3

    suite_ok = (
        area.uses_windows_suite
        and ev.suite_as_of_phase >= area.last_phase
        and _found(repo, ev.suite_source, ev.suite_match)
    )
    run_ok = False
    if area.author_run is not None:
        r = area.author_run
        if not _found(repo, r.source, r.match):
            out.problems.append(f"author run '{r.match}' not found in {r.source}")
        run_ok = r.as_of_phase >= area.last_phase and _found(repo, r.source, r.match)
    if not (suite_ok or run_ok):
        why = f"last changed in Phase {area.last_phase}"
        if area.uses_windows_suite:
            why += f"; the Windows suite on record is Phase {ev.suite_as_of_phase}"
        out.notes.append(f"E4 missing: no run on the author's machine after that change ({why})")
        return out
    out.level = 4

    if not area.real_world:
        out.notes.append(
            "E5 missing: nothing checked against real data, an outside review or real traffic"
        )
        return out
    out.level = 5
    return out


def evaluate_all(path: Path, repo: Path) -> tuple[Evidence, list[Evaluation]]:
    ev, areas = load(path)
    return ev, [evaluate(a, ev, repo) for a in areas]
