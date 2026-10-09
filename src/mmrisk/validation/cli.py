"""CLI: python -m mmrisk.validation check"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

from mmrisk.ingestion.lake import LakePaths
from mmrisk.ingestion.ledger import Ledger
from mmrisk.validation.checks import FAIL, PASS, WARN, run_checks


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="python -m mmrisk.validation")
    sub = p.add_subparsers(dest="command", required=True)
    c = sub.add_parser("check", help="run dataset-level data-quality checks on the lake")
    c.add_argument("--lake", type=Path, default=Path("data/processed"))
    c.add_argument(
        "--as-of",
        type=datetime.fromisoformat,
        default=None,
        help="reference time for the freshness check, e.g. 2025-12-31T23:59:59",
    )
    c.add_argument("--max-lag-hours", type=float, default=None)
    c.add_argument("--strict", action="store_true", help="treat WARN as failure (exit code 1)")
    c.add_argument("--verbose", action="store_true", help="also list PASS checks")
    c.add_argument("--no-write", action="store_true", help="do not write _meta/dq_report.json")
    args = p.parse_args(argv)

    if not Ledger.load(LakePaths(args.lake).ledger_path).batches:
        print(f"error: no ledger in {args.lake}; run the ingestion first", file=sys.stderr)
        return 2
    report = run_checks(args.lake, args.as_of, args.max_lag_hours, write=not args.no_write)
    shown = [x for x in report.checks if args.verbose or x.status != PASS]
    if shown:
        print(f"{'status':<8}{'check':<66}{'table':<14}observed")
        print("-" * 112)
        for x in shown:
            print(f"{x.status:<8}{x.name:<66}{x.table:<14}{x.observed}")
            if x.message:
                print(f"{'':<8}-> {x.message}")
    n = report.counts()
    print(
        f"\nOVERALL: {report.status}  (PASS {n[PASS]}, WARN {n[WARN]}, "
        f"FAIL {n[FAIL]}, SKIPPED {n['SKIPPED']})"
    )
    if not args.no_write:
        print(f"report written to {args.lake / '_meta' / 'dq_report.json'}")
    if report.status == FAIL or (args.strict and report.status == WARN):
        return 1
    return 0
