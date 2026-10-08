"""CLI: python -m mmrisk.ingestion run|status"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from mmrisk.ingestion.lake import LakePaths
from mmrisk.ingestion.ledger import COMPLETE, Ledger
from mmrisk.ingestion.pipeline import REJECTED_BATCH, ingest_source_dir
from mmrisk.ingestion.reconcile import reconcile


def _print_results(results) -> None:
    head = (
        f"{'table':<14}{'status':<26}{'rows_in':>9}{'cleaned':>9}"
        f"{'dups':>7}{'conflict':>9}{'rejected':>9}"
    )
    print(head)
    print("-" * len(head))
    for r in results:
        print(
            f"{r.table:<14}{r.status:<26}{r.rows_in:>9,}{r.rows_cleaned:>9,}"
            f"{r.rows_duplicate:>7,}{r.rows_conflict:>9,}{r.rows_rejected:>9,}"
        )
        if r.reason:
            print(f"    reason: {r.reason}")


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="python -m mmrisk.ingestion")
    sub = p.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="ingest source files into the lake layers")
    run.add_argument("--source", type=Path, default=Path("data/raw"))
    run.add_argument("--lake", type=Path, default=Path("data/processed"))
    st = sub.add_parser("status", help="show the ledger and reconcile layers")
    st.add_argument("--lake", type=Path, default=Path("data/processed"))
    args = p.parse_args(argv)

    if args.command == "run":
        if not args.source.exists():
            print(
                f"error: source directory {args.source} not found (run the generator first)",
                file=sys.stderr,
            )
            return 2
        results, ignored = ingest_source_dir(args.source, args.lake)
        if not results:
            print(f"error: no known source tables found in {args.source}", file=sys.stderr)
            return 2
        _print_results(results)
        if ignored:
            print(f"ignored (not an ingestible table): {', '.join(ignored)}")
        problems = reconcile(args.lake)
        print("reconciliation:", "OK" if not problems else f"{len(problems)} problem(s)")
        for prob in problems:
            print("  -", prob)
        rejected = any(r.status == REJECTED_BATCH for r in results)
        return 1 if (rejected or problems) else 0

    ledger = Ledger.load(LakePaths(args.lake).ledger_path)
    if not ledger.batches:
        print(f"no ledger found in {args.lake}")
        return 2
    for b in ledger.batches:
        print(
            f"{b['table']:<14}{b['status']:<10}{b['source_file']:<34}"
            f"rows_in={b['rows_in']:,} cleaned={b['rows_cleaned']:,}"
        )
    problems = reconcile(args.lake)
    print("reconciliation:", "OK" if not problems else f"{len(problems)} problem(s)")
    for prob in problems:
        print("  -", prob)
    done = len(ledger.complete())
    print(f"{done}/{len(ledger.batches)} batches {COMPLETE}")
    return 1 if problems else 0
