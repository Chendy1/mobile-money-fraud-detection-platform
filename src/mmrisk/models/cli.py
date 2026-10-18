"""CLI: python -m mmrisk.models train | advanced | explain | card"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from mmrisk.features import compute_features
from mmrisk.ingestion.reader import read_layer
from mmrisk.models.report import run
from mmrisk.models.split import SplitError


def _load(lake: Path):
    """(features, labels) from the cleaned lake, or an error message."""
    if not lake.exists():
        return None, f"lake {lake} not found; run the ingestion first"
    tables = {t: read_layer(lake, "cleaned", t) for t in ("transactions", "customers", "labels")}
    empty = [k for k, v in tables.items() if v.is_empty()]
    if empty:
        return None, f"lake has no completed data for: {', '.join(empty)}"
    return (compute_features(tables["transactions"], tables["customers"]), tables["labels"]), None


def _cmd_train(args) -> int:
    loaded, err = _load(args.lake)
    if err:
        print(f"error: {err}", file=sys.stderr)
        return 2
    features, labels = loaded
    truth = None if args.no_truth else args.source / "ground_truth" / "transaction_truth.parquet"
    try:
        result = run(
            features, labels, args.models_dir, args.report, args.figures,
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


def _cmd_advanced(args) -> int:
    from mmrisk.models.advanced import BaselineError, run_advanced
    from mmrisk.models.costs import CostModel, CostModelError

    loaded, err = _load(args.lake)
    if err:
        print(f"error: {err}", file=sys.stderr)
        return 2
    features, labels = loaded
    truth = None if args.no_truth else args.source / "ground_truth" / "transaction_truth.parquet"
    try:
        cm = CostModel(
            loss_rate=args.loss_rate, review_cost=args.review_cost,
            review_prevents=args.review_prevents, block_cost=args.block_cost,
        )  # fmt: skip
        result = run_advanced(
            features, labels, args.baselines_dir, args.models_dir, args.report, args.figures,
            truth_file=truth, cost_model=cm, seed=args.seed, quick=args.quick,
            n_boot=args.bootstrap,
        )  # fmt: skip
    except (SplitError, BaselineError, CostModelError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(f"report: {result.report_path}")
    print(f"risk model: {args.models_dir}")
    for name, v in result.metrics["cost_per_10k"].items():
        print(f"  cost per 10,000 txns (ASSUMED costs) {name:<20} {v['point']:>10,.0f} KES")
    return 0


def _cmd_explain(args) -> int:
    from mmrisk.models.explain import ExplainError
    from mmrisk.models.explain_run import BundleMismatchError, run_explain

    loaded, err = _load(args.lake)
    if err:
        print(f"error: {err}", file=sys.stderr)
        return 2
    features, labels = loaded
    truth = None if args.no_truth else args.source / "ground_truth" / "transaction_truth.parquet"
    try:
        result = run_explain(
            features, labels, args.models_dir, args.report, args.figures, truth_file=truth,
            seed=args.seed, quick=args.quick,
        )  # fmt: skip
    except (SplitError, BundleMismatchError, ExplainError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(f"report: {args.report}")
    top = result.metrics["importance_fraud_rows"][:3]
    print("  largest effects on fraud-labelled rows: " + ", ".join(r["unit"] for r in top))
    print(f"  largest additivity error: {result.metrics['additivity_error_max']:.1e}")
    return 0


def _cmd_card(args) -> int:
    from mmrisk.models.model_card import CardError, write

    try:
        out = write(args.models_dir, args.out)
    except CardError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(f"model card: {out}")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="python -m mmrisk.models")
    sub = p.add_subparsers(dest="command", required=True)

    def common(sp):
        sp.add_argument("--lake", type=Path, default=Path("data/processed"))
        sp.add_argument("--source", type=Path, default=Path("data/raw"))
        sp.add_argument("--no-truth", action="store_true", help="skip the ground-truth audit")
        sp.add_argument("--seed", type=int, default=42)
        sp.add_argument("--quick", action="store_true", help="smaller grid (faster)")
        sp.add_argument("--bootstrap", type=int, default=300, help="bootstrap resamples")

    t = sub.add_parser("train", help="fit baselines, evaluate, write models and the report")
    common(t)
    t.add_argument("--models-dir", type=Path, default=Path("models/baselines"))
    t.add_argument("--report", type=Path, default=Path("docs/model-baselines.md"))
    t.add_argument("--figures", type=Path, default=Path("docs/figures/models"))
    t.set_defaults(func=_cmd_train)

    a = sub.add_parser(
        "advanced", help="LightGBM + calibration + cost-based decisions (needs `train` first)"
    )
    common(a)
    a.add_argument("--baselines-dir", type=Path, default=Path("models/baselines"))
    a.add_argument("--models-dir", type=Path, default=Path("models/advanced"))
    a.add_argument("--report", type=Path, default=Path("docs/model-advanced.md"))
    a.add_argument("--figures", type=Path, default=Path("docs/figures/models"))
    a.add_argument("--loss-rate", type=float, default=1.0, help="ASSUMED share of amount lost")
    a.add_argument("--review-cost", type=float, default=25.0, help="ASSUMED KES per review")
    a.add_argument("--review-prevents", type=float, default=0.7, help="ASSUMED share stopped")
    a.add_argument("--block-cost", type=float, default=150.0, help="ASSUMED KES per wrong block")
    a.set_defaults(func=_cmd_advanced)

    e = sub.add_parser("explain", help="explain the shipped model (needs `advanced` first)")
    common(e)
    e.add_argument("--models-dir", type=Path, default=Path("models/advanced"))
    e.add_argument("--report", type=Path, default=Path("docs/model-explanations.md"))
    e.add_argument("--figures", type=Path, default=Path("docs/figures/models"))
    e.set_defaults(func=_cmd_explain)

    c = sub.add_parser("card", help="write the model card from the saved results")
    c.add_argument("--models-dir", type=Path, default=Path("models/advanced"))
    c.add_argument("--out", type=Path, default=Path("docs/model-card.md"))
    c.set_defaults(func=_cmd_card)

    args = p.parse_args(argv)
    return args.func(args)
