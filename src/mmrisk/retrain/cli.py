"""python -m mmrisk.retrain check | run

check  decides whether a retrain is worth doing (exit 0 no, 1 yes, 2 a mistake)
run    trains a candidate, applies the gates, registers it (exit 0 approved, 1 rejected, 2 mistake)

Promotion is a separate, explicit step (python -m mmrisk.registry promote) unless --auto-promote.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

from mmrisk.api.loading import read_lake
from mmrisk.api.service import ServiceError
from mmrisk.features import compute_features
from mmrisk.ingestion.reader import read_layer
from mmrisk.monitoring.cli import _write
from mmrisk.registry import Registry, RegistryError
from mmrisk.retrain import trigger
from mmrisk.retrain.config import Gates
from mmrisk.retrain.run import (
    RetrainError,
    champion_info,
    markdown,
    new_fraud_labels,
    payload,
    run_retrain,
)  # fmt: skip

DEFAULT_REGISTRY = Path("models/registry")
DEFAULT_OUT = Path("data/retrain")
DEFAULT_DRIFT = Path("data/drift/report.json")


class UsageError(ValueError):
    """A mistake in how the command was called."""


def _drift_report(path: Path) -> dict | None:
    if not Path(path).exists():
        return None
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        data["drift_detected"]
    except (ValueError, KeyError, TypeError):
        raise UsageError(f"{path} is not a report written by mmrisk.drift report") from None
    return data


def _cmd_check(args) -> int:
    try:
        champ = champion_info(Registry(args.registry))
        labels = read_layer(args.lake, "cleaned", "labels")
        txns, _ = read_lake(args.lake)
    except (RetrainError, ServiceError, RegistryError, FileNotFoundError, OSError) as exc:
        raise UsageError(str(exc)) from None
    as_of = txns["timestamp"].max()
    if args.as_of:
        try:
            as_of = datetime.fromisoformat(args.as_of)
        except ValueError:
            raise UsageError("--as-of must be an ISO time such as 2025-12-31T00:00:00") from None
    age = None if champ.data_end is None else (as_of - champ.data_end).total_seconds() / 86400
    new = None if champ.data_end is None else new_fraud_labels(labels, txns, champ.data_end, as_of)
    decision = trigger.evaluate(
        drift=_drift_report(args.drift_report), model_age_days=age, new_fraud_labels=new,
        force=args.force,
    )  # fmt: skip
    for t in decision.triggers:
        state = "unknown" if t.fired is None else ("FIRED" if t.fired else "no")
        print(f"{t.name:11} {state:8} {t.detail}")
    print("RETRAIN RECOMMENDED" if decision.retrain else "no retrain needed now")
    _write(Path(args.out) / "check.json", json.dumps({
        "retrain": decision.retrain, "champion": champ.version, "as_of": str(as_of),
        "triggers": [t.__dict__ for t in decision.triggers],
    }, indent=2) + "\n")  # fmt: skip
    return 1 if decision.retrain else 0


def _cmd_run(args) -> int:
    try:
        txns, customers = read_lake(args.lake)
        labels = read_layer(args.lake, "cleaned", "labels")
        features = compute_features(txns, customers)
        out = run_retrain(
            features, labels, Registry(args.registry), seed=args.seed, quick=args.quick,
            auto_promote=args.auto_promote,
            th=Gates(
                min_train_fraud=args.min_train_fraud,
                min_val_fraud=args.min_val_fraud,
                min_test_fraud=args.min_test_fraud,
                pr_auc_margin=args.pr_auc_margin,
                cost_margin=args.cost_margin,
            ),
        )  # fmt: skip
    except (RetrainError, ServiceError, RegistryError, FileNotFoundError, OSError) as exc:
        raise UsageError(str(exc)) from None
    for g in out.gates:
        mark = "PASS" if g.passed else ("FAIL" if g.required else "WARN")
        print(f"{mark:5} {g.name:17} {g.detail}")
    print(markdown(out).splitlines()[2].strip("*"))
    _write(Path(args.out) / "report.md", markdown(out))
    _write(Path(args.out) / "report.json", json.dumps(payload(out), indent=2, default=str) + "\n")
    print(f"report written to {Path(args.out) / 'report.md'}")
    return 0 if out.approved else 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m mmrisk.retrain", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)
    for name in ("check", "run"):
        s = sub.add_parser(name)
        s.add_argument("--lake", type=Path, default=Path("data/processed"))
        s.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY)
        s.add_argument("--out", type=Path, default=DEFAULT_OUT)
        if name == "check":
            s.add_argument("--drift-report", type=Path, default=DEFAULT_DRIFT)
            s.add_argument("--as-of", default=None, help="ISO time; default: end of the lake")
            s.add_argument("--force", action="store_true", help="recommend a retrain regardless")
        else:
            s.add_argument("--seed", type=int, default=42)
            s.add_argument("--quick", action="store_true", help="smoke test; cannot be promoted")
            for field in ("train", "val", "test"):
                s.add_argument(
                    f"--min-{field}-fraud", type=int, default=getattr(Gates(), f"min_{field}_fraud")
                )
            s.add_argument("--pr-auc-margin", type=float, default=Gates().pr_auc_margin)
            s.add_argument("--cost-margin", type=float, default=Gates().cost_margin)
            s.add_argument("--auto-promote", action="store_true",
                           help="promote the candidate if every required gate passes")  # fmt: skip
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return _cmd_check(args) if args.cmd == "check" else _cmd_run(args)
    except UsageError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
