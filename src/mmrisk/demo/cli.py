"""python -m mmrisk.demo list | check | run | clean

list   the steps and what to say at each, without running anything
check  only the prerequisites
run    the guided demo (Enter between steps; --yes runs straight through)
clean  delete data/demo (the only folder the demo writes)

Exit codes: 0 all steps ok, 1 a step failed or its output was not what it should show, 2 a mistake.
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

from mmrisk.demo import DemoError, Paths
from mmrisk.demo.runner import action_prereq, run_demo, summary
from mmrisk.demo.steps import TRACKS, build_steps, select


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m mmrisk.demo", description=__doc__.split("\n\n")[0])
    sub = p.add_subparsers(dest="command", required=True)
    for name, text in (("list", "show the steps"), ("check", "check prerequisites"),
                       ("run", "run the demo"), ("clean", "delete data/demo")):  # fmt: skip
        s = sub.add_parser(name, help=text)
        s.add_argument("--repo", type=Path, default=Path("."), help="repository root")
        if name in ("list", "run"):
            s.add_argument("--track", choices=TRACKS, default="standard")
            s.add_argument("--only", default="", help="comma-separated step ids")
        if name == "run":
            s.add_argument("--yes", action="store_true", help="no pauses")
            s.add_argument("--max-lines", type=int, default=30)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    paths = Paths(repo=args.repo.resolve())
    try:
        if args.command == "clean":
            target = (paths.repo / paths.out).resolve()
            if target.name != "demo" or target.parent.name != "data":
                raise DemoError(f"refusing to delete {target}: not <repo>/data/demo")
            shutil.rmtree(target, ignore_errors=True)
            print(f"removed {target}")
            return 0
        if args.command == "check":
            ok, text = action_prereq(paths)
            print(text)
            return 0 if ok else 1
        only = tuple(x for x in args.only.split(",") if x)
        steps = select(build_steps(paths), args.track, only)
        if args.command == "list":
            for n, s in enumerate(steps, 1):
                print(f"{n:>2}. {s.id:<12} {s.title}  (~{s.seconds} s)")
                for line in s.say:
                    print(f"      SAY: {line}")
            print(
                f"about {sum(s.seconds for s in steps)} s of running time as measured, plus talking"
            )
            return 0
        results = run_demo(steps, paths, pause=not args.yes, max_lines=args.max_lines)
        print("\n" + summary(results))
        return 0 if all(r.status == "ok" for r in results) else 1
    except DemoError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\nstopped", file=sys.stderr)
        return 130
