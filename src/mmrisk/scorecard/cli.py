"""`python -m mmrisk.scorecard render|check|show`."""

from __future__ import annotations

import argparse
from pathlib import Path

from mmrisk.scorecard.model import ScorecardError, evaluate_all
from mmrisk.scorecard.render import render


def _repo_root() -> Path:
    here = Path.cwd()
    for p in [here, *here.parents]:
        if (p / "docs" / "scorecard.json").is_file():
            return p
    return here


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="mmrisk.scorecard", description="Evidence scorecard")
    ap.add_argument("command", choices=["render", "check", "show"])
    ap.add_argument(
        "--repo",
        type=Path,
        default=None,
        help="repository root (default: found from the current folder)",
    )
    args = ap.parse_args(argv)
    repo = args.repo or _repo_root()
    data, page = repo / "docs" / "scorecard.json", repo / "docs" / "scorecard.md"
    try:
        ev, results = evaluate_all(data, repo)
    except (ScorecardError, OSError) as exc:
        print(f"ERROR: {exc}")
        return 2
    text = render(ev, results)
    if args.command == "show":
        for r in results:
            print(f"E{r.level}  {r.area.id}")
        return 0
    if args.command == "render":
        page.write_text(text, encoding="utf-8", newline="\n")
        print(f"wrote {page} ({len(results)} areas)")
        return 0
    bad = False
    for r in results:
        for p in r.problems:
            print(f"PROBLEM {r.area.id}: {p}")
            bad = True
    current = page.read_text(encoding="utf-8") if page.is_file() else None
    if current is None or current.replace("\r\n", "\n") != text:
        print(
            "STALE: docs/scorecard.md does not match docs/scorecard.json and the repository; run `render`"
        )
        bad = True
    print("scorecard check: " + ("FAILED" if bad else f"ok ({len(results)} areas)"))
    return 1 if bad else 0
