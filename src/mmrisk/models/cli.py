"""CLI: python -m mmrisk.models train"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from mmrisk.features import compute_features
from mmrisk.ingestion.reader import read_layer
from mmrisk.models.report import run
from mmrisk.models.split import SplitError


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="python -m mmrisk.models")
    sub = p.add_subparsers(dest="command", required=True)
    t = sub.add_parser("train", help="fit baselines, evaluate, write models and the report")
    t.add_argument("--lake", type=Path, default=Path("data/processed"))
    t.add_argument("--source", type=Path, default=Path("data/raw"))
    t.add_argument("--no-truth", action="store_true", help="skip the ground-truth audit")
    t.add_argument("--models-dir", type=Path, default=Path("models/baselines"))
    t.add_argument("--report", type=Path, default=Path("docs/model-baselines.md"))
    t.add_argument("--figures", type=Path, default=Path("docs/figures/models"))
    t.add_argument("--seed", type=int, default=42)
    t.add_argument("--quick", action="store_true", help="smaller grid and fewer trees (faster)")
    t.add_argument("--bootstrap", type=int, default=300, help="bootstrap resamples")
    args = p.parse_args(argv)

    if not args.lake.exists():
        print(f"error: lake {args.lake} not found; run the ingestion first", file=sys.stderr)
        return 2
    tables = {
        t_: read_layer(args.lake, "cleaned", t_) for t_ in ("transactions", "customers", "labels")
    }
    empty = [k for k, v in tables.items() if v.is_empty()]
    if empty:
        print(f"error: lake has no completed data for: {', '.join(empty)}", file=sys.stderr)
        return 2
    features = compute_features(tables["transactions"], tables["customers"])
    truth = None if args.no_truth else args.source / "ground_truth" / "transaction_truth.parquet"
    try:
        result = run(
            features, tables["labels"], args.models_dir, args.report, args.figures,
            truth_file=truth, seed=args.seed, quick=args.quick, n_boot=args.bootstrap,
        )  # fmt: skip
    except SplitError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(f"report: {result.report_path}")
    print(f"models: {args.models_dir}")
    for name, v in result.metrics["test"].items():
        print(f"  test PR-AUC {name:<20} {v['point']['pr_auc']:.3f}")
    return 0
