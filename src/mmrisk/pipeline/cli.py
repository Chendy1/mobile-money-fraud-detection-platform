"""CLI: python -m mmrisk.pipeline run | compare"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from mmrisk.models.advanced import BaselineError
from mmrisk.models.costs import CostModel, CostModelError
from mmrisk.models.explain import ExplainError
from mmrisk.models.explain_run import BundleMismatchError
from mmrisk.models.model_card import CardError
from mmrisk.models.split import SplitError
from mmrisk.pipeline.run import (
    EXPERIMENT,
    PipelineConfig,
    PipelineError,
    resolve_dirs,
    run_pipeline,
)
from mmrisk.pipeline.tracker import (
    DEFAULT_URI,
    MlflowTracker,
    NullTracker,
    TrackingError,
    check_tracking_uri,
)

USER_ERRORS = (PipelineError, TrackingError, SplitError, BaselineError, BundleMismatchError,
               ExplainError, CardError, CostModelError)  # fmt: skip


def _cmd_run(args) -> int:
    try:
        cm = CostModel(
            loss_rate=args.loss_rate, review_cost=args.review_cost,
            review_prevents=args.review_prevents, block_cost=args.block_cost,
        )  # fmt: skip
        models_dir, docs_dir = resolve_dirs(args.quick, args.models_dir, args.docs_dir)
        cfg = PipelineConfig(
            lake=args.lake, source=args.source, models_dir=models_dir, docs_dir=docs_dir,
            seed=args.seed, quick=args.quick, bootstrap=args.bootstrap,
            use_truth=not args.no_truth, cost_model=cm, run_name=args.run_name,
        )  # fmt: skip
        tracker = (
            NullTracker() if args.no_tracking else MlflowTracker(args.tracking_uri, args.experiment)
        )
        result = run_pipeline(cfg, tracker)
    except USER_ERRORS as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(f"run: {result.run_name}")
    print(f"  models in {models_dir}, reports in {docs_dir}")
    for stage, sec in result.stage_seconds.items():
        print(f"  {stage:<10} {sec:7.1f} s")
    shipped = result.metrics.get("test_pr_auc/lightgbm")
    cost = result.metrics.get("assumed_cost_per_10k/lightgbm")
    if shipped is not None:
        print(f"  test PR-AUC lightgbm {shipped:.3f}")
    if cost is not None:
        print(f"  cost per 10,000 txns (ASSUMED costs) lightgbm {cost:,.0f} KES")
    print(f"manifest: {result.manifest_path}")
    if result.tracker == "mlflow":
        print(
            f"tracked in {args.tracking_uri} (experiment {args.experiment}, run id {result.run_id})"
        )
        print(f"browse: mlflow ui --backend-store-uri {args.tracking_uri}")
    return 0


def _cmd_compare(args) -> int:
    try:
        check_tracking_uri(args.tracking_uri)
        import mlflow
    except TrackingError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except ImportError:
        print('error: MLflow is not installed: pip install -e ".[tracking]"', file=sys.stderr)
        return 2
    mlflow.set_tracking_uri(args.tracking_uri)
    exp = mlflow.get_experiment_by_name(args.experiment)
    if exp is None:
        print(f"no experiment '{args.experiment}' in {args.tracking_uri}; run the pipeline first")
        return 0
    runs = mlflow.search_runs(experiment_ids=[exp.experiment_id], max_results=args.last,
                              order_by=["attributes.start_time DESC"])  # fmt: skip
    if runs.empty:
        print("no runs yet")
        return 0
    cols = {
        "tags.mlflow.runName": "run", "status": "status", "params.config/quick": "quick",
        "metrics.test_pr_auc/lightgbm": "test PR-AUC", "metrics.assumed_cost_per_10k/lightgbm":
        "cost/10k (assumed)", "metrics.seconds/total": "seconds", "tags.git_commit": "commit",
    }  # fmt: skip
    table = runs[[c for c in cols if c in runs.columns]].rename(columns=cols)
    if "commit" in table:
        table["commit"] = table["commit"].astype(str).str[:8]
    print(table.to_string(index=False, float_format=lambda v: f"{v:,.3f}"))
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="python -m mmrisk.pipeline")
    sub = p.add_subparsers(dest="command", required=True)

    r = sub.add_parser("run", help="features, baselines, advanced model, explanations and card")
    r.add_argument("--lake", type=Path, default=Path("data/processed"))
    r.add_argument("--source", type=Path, default=Path("data/raw"))
    r.add_argument(
        "--models-dir", type=Path, default=None, help="default models (quick: models/quick)"
    )
    r.add_argument("--docs-dir", type=Path, default=None, help="default docs (quick: docs/quick)")
    r.add_argument("--seed", type=int, default=42)
    r.add_argument("--quick", action="store_true", help="smaller grids, fewer shuffles (faster)")
    r.add_argument("--bootstrap", type=int, default=300)
    r.add_argument("--no-truth", action="store_true", help="skip the ground-truth audits")
    r.add_argument("--loss-rate", type=float, default=1.0, help="ASSUMED share of amount lost")
    r.add_argument("--review-cost", type=float, default=25.0, help="ASSUMED KES per review")
    r.add_argument("--review-prevents", type=float, default=0.7, help="ASSUMED share stopped")
    r.add_argument("--block-cost", type=float, default=150.0, help="ASSUMED KES per wrong block")
    r.add_argument("--run-name", default=None)
    r.add_argument("--no-tracking", action="store_true", help="do not write to MLflow")
    r.add_argument("--tracking-uri", default=DEFAULT_URI, help="local only (sqlite file or folder)")
    r.add_argument("--experiment", default=EXPERIMENT)
    r.set_defaults(func=_cmd_run)

    c = sub.add_parser("compare", help="list recent tracked runs")
    c.add_argument("--tracking-uri", default=DEFAULT_URI)
    c.add_argument("--experiment", default=EXPERIMENT)
    c.add_argument("--last", type=int, default=10)
    c.set_defaults(func=_cmd_compare)

    args = p.parse_args(argv)
    return args.func(args)
