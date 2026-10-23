"""python -m mmrisk.drift reference | report

Exit codes: 0 no major drift, 1 drift detected, 2 a mistake in how it was called.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

import polars as pl

from mmrisk.api.loading import read_lake
from mmrisk.api.service import ServiceError, load_model
from mmrisk.drift import config as cfg
from mmrisk.drift import reference as refmod
from mmrisk.drift import stats
from mmrisk.drift.detect import detect
from mmrisk.drift.report import markdown, text, verdict
from mmrisk.features import compute_features
from mmrisk.features.registry import feature_names
from mmrisk.monitoring.cli import _write

DEFAULT_OUT = Path("data/drift")
DEFAULT_REF = DEFAULT_OUT / "reference.json"


class UsageError(ValueError):
    """A mistake in how the command was called."""


def _when(text_: str | None, what: str) -> datetime | None:
    if not text_:
        return None
    try:
        return datetime.fromisoformat(text_)
    except ValueError:
        raise UsageError(f"{what} must be an ISO time such as 2025-12-24T00:00:00") from None


def _split_bounds(model_dir: Path) -> tuple[datetime, datetime]:
    try:
        meta = json.loads((Path(model_dir) / "risk_model.json").read_text(encoding="utf-8"))
        return (
            datetime.fromisoformat(meta["split"]["val_start"]),
            datetime.fromisoformat(meta["split"]["test_start"]),
        )
    except (OSError, ValueError, KeyError, TypeError):
        raise UsageError(
            f"cannot read the validation window from {model_dir}; give --from and --to"
        ) from None


def _inject(txns: pl.DataFrame, start: datetime, factor: float) -> pl.DataFrame:
    """Demonstration only: scale the amounts of transactions after `start`."""
    scaled = (pl.col("amount_kes") * factor).round().cast(pl.Int64)
    return txns.with_columns(
        pl.when(pl.col("timestamp") > start).then(scaled).otherwise(pl.col("amount_kes"))
        .alias("amount_kes")
    )  # fmt: skip


def _window(df: pl.DataFrame, after: datetime, upto: datetime) -> pl.DataFrame:
    return df.filter((pl.col("timestamp") > after) & (pl.col("timestamp") <= upto))


def _load(args):
    try:
        model = load_model(args.model_dir)
        txns, customers = read_lake(args.lake)
    except ServiceError as exc:
        raise UsageError(str(exc)) from None
    return model, txns, customers


def _cmd_reference(args) -> int:
    model, txns, customers = _load(args)
    lo, hi = _when(args.start, "--from"), _when(args.end, "--to")
    if lo is None or hi is None:
        default_lo, default_hi = _split_bounds(args.model_dir)
        lo, hi = lo or default_lo, hi or default_hi
    if lo >= hi:
        raise UsageError("--from must be before --to")
    feats = _window(compute_features(txns, customers), lo, hi)
    if feats.is_empty():
        raise UsageError(f"no transactions between {lo} and {hi}")
    scores = model.assess(feats)["risk_probability"]
    ref = refmod.build(feats, scores, feature_names(), (lo, hi), str(model.feature_version))
    out = Path(args.out)
    refmod.save(ref, out)
    print(f"reference of {ref['rows']} transactions ({lo} to {hi}) written to {out}")
    return 0


def _series(ref, feats, scores, every: str) -> list[dict]:
    from mmrisk.monitoring.checks import window_hours

    days = window_hours(every) / 24
    frame = feats.with_columns(scores.alias("_score"))
    out = []
    grouped = frame.sort("timestamp").group_by_dynamic(
        "timestamp", every=every, closed="left", label="left"
    )
    for key, part in grouped:
        start = key[0] if isinstance(key, tuple) else key
        if part.height < cfg.MIN_CURRENT_ROWS:
            out.append({"start": str(start), "rows": part.height, "psi": None})
            continue
        d = detect(ref, part.drop("_score"), part["_score"], days)
        scored = [r for r in d.counted if r.psi is not None and r.name != "risk_probability"]
        worst = max(scored, key=lambda r: r.psi)
        out.append({"start": str(start), "rows": part.height, "psi": worst.psi,
                    "feature": worst.name, "score_psi": d.score.psi})  # fmt: skip
    return out


def _cmd_report(args) -> int:
    ref = refmod.load(args.reference)
    model, txns, customers = _load(args)
    if str(ref["feature_version"]) != str(model.feature_version):
        raise UsageError(
            f"the reference is for feature version {ref['feature_version']}, "
            f"the model for {model.feature_version}"
        )
    if args.scale_amounts is not None and args.scale_amounts <= 0:
        raise UsageError("--scale-amounts must be above zero")
    end = _when(args.end, "--to") or txns["timestamp"].max()
    start = _when(args.start, "--from")
    if start is None:
        if args.last_days <= 0:
            raise UsageError("--last-days must be above zero")
        start = end - timedelta(days=args.last_days)
    if start >= end:
        raise UsageError("--from must be before --to")
    stats_window = (start, end)
    if args.scale_amounts is not None:
        txns = _inject(txns, start, args.scale_amounts)
    feats = _window(compute_features(txns, customers), *stats_window)
    if feats.is_empty():
        raise UsageError(f"no transactions between {start} and {end}")
    scores = model.assess(feats)["risk_probability"]
    days = (end - start).total_seconds() / 86400
    result = detect(ref, feats, scores, days)
    if not result.scored:
        raise UsageError(
            f"the window has {feats.height} transactions; at least {cfg.MIN_CURRENT_ROWS} "
            "are needed to compare it with the reference"
        )
    series = _series(ref, feats, scores, args.series_window)
    print(text(result))
    meta = {
        "reference_window": f"{ref['window']['from']} to {ref['window']['to']}",
        "reference_rows": ref["rows"],
        "current_window": f"{start} to {end}",
        "series_window": args.series_window,
        "injected": (
            f"amounts multiplied by {args.scale_amounts:g} after {start}"
            if args.scale_amounts is not None
            else None
        ),
    }
    _write(Path(args.out) / "report.md", markdown(result, meta, series))
    payload = {
        "drift_detected": result.drift_detected,
        "verdict": verdict(result),
        "rows": result.rows,
        "days": result.days,
        "window": meta["current_window"],
        "reference_window": meta["reference_window"],
        "injected": meta["injected"],
        "results": [r.__dict__ for r in [*result.features, result.score]],
        "series": series,
    }
    _write(Path(args.out) / "report.json", json.dumps(payload, indent=2) + "\n")
    print(f"report written to {Path(args.out) / 'report.md'}")
    return 1 if result.drift_detected else 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m mmrisk.drift", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)
    for name, helptext in (
        ("reference", "summarise a reference window (default: the model's validation window)"),
        ("report", "compare a window with the reference"),
    ):
        s = sub.add_parser(name, help=helptext)
        s.add_argument("--lake", type=Path, default=Path("data/processed"))
        s.add_argument("--model-dir", type=Path, default=Path("models/advanced"))
        if name == "reference":
            s.add_argument("--from", dest="start", default=None)
            s.add_argument("--to", dest="end", default=None)
            s.add_argument("--out", type=Path, default=DEFAULT_REF)
        else:
            s.add_argument("--reference", type=Path, default=DEFAULT_REF)
            s.add_argument(
                "--from", dest="start", default=None, help="ISO time; default: --last-days"
            )
            s.add_argument("--to", dest="end", default=None, help="ISO time; default: newest")
            s.add_argument("--last-days", type=float, default=cfg.DEFAULT_LAST_DAYS)
            s.add_argument("--series-window", default=cfg.DEFAULT_SERIES_WINDOW)
            s.add_argument("--out", type=Path, default=DEFAULT_OUT)
            s.add_argument(
                "--scale-amounts",
                type=float,
                default=None,
                help="demonstration: multiply amounts in the window by this factor",
            )
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return {"reference": _cmd_reference, "report": _cmd_report}[args.cmd](args)
    except (UsageError, refmod.ReferenceFileError, stats.DriftStatsError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
