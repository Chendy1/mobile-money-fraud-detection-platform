"""CLI: python -m mmrisk.eda"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from mmrisk.eda.data import Analysis, EDAError
from mmrisk.eda.report import build_report


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        prog="python -m mmrisk.eda",
        description="Generate the exploratory data analysis report from the data lake.",
    )
    p.add_argument("--lake", type=Path, default=Path("data/processed"))
    p.add_argument(
        "--source",
        type=Path,
        default=Path("data/raw"),
        help="source extract; only used to find ground truth for the audit section",
    )  # noqa: E501
    p.add_argument("--no-truth", action="store_true", help="skip the ground-truth audit section")
    p.add_argument("--report", type=Path, default=Path("docs/eda-report.md"))
    p.add_argument("--figures", type=Path, default=Path("docs/figures/eda"))
    args = p.parse_args(argv)

    if not args.lake.exists():
        print(f"error: lake {args.lake} not found; run the ingestion first", file=sys.stderr)
        return 2
    try:
        analysis = Analysis.from_lake(args.lake, None if args.no_truth else args.source)
    except EDAError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    result = build_report(analysis, args.report, args.figures, use_truth=not args.no_truth)
    audit = "included" if analysis.truth_available and not args.no_truth else "skipped"
    print(f"report: {result.report_path}")
    print(f"figures: {len(result.figure_paths)} written to {args.figures}")
    print(f"ground-truth audit: {audit}")
    return 0
