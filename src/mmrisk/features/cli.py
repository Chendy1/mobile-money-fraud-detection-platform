"""CLI: python -m mmrisk.features build | catalog | audit"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import polars as pl

from mmrisk.features.audit import audit
from mmrisk.features.catalog import render
from mmrisk.features.compute import FeatureInputError, compute_features
from mmrisk.features.registry import FEATURE_VERSION, feature_names
from mmrisk.features.training import training_table
from mmrisk.generation.config import DISPUTE_WINDOW_DAYS
from mmrisk.ingestion.reader import layer_files, read_layer


def _load(lake: Path) -> tuple[pl.DataFrame, pl.DataFrame, pl.DataFrame]:
    if not lake.exists():
        raise SystemExit(f"error: lake {lake} not found; run the ingestion first")
    tables = {t: read_layer(lake, "cleaned", t) for t in ("transactions", "customers", "labels")}
    empty = [t for t, df in tables.items() if df.is_empty()]
    if empty:
        raise SystemExit(f"error: lake has no completed data for: {', '.join(empty)}")
    return tables["transactions"], tables["customers"], tables["labels"]


def cmd_build(args) -> int:
    txns, customers, _ = _load(args.lake)
    try:
        features = compute_features(txns, customers)
    except FeatureInputError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    args.out.parent.mkdir(parents=True, exist_ok=True)
    tmp = args.out.with_suffix(".tmp")
    features.write_parquet(tmp)
    tmp.replace(args.out)
    manifest = {
        "feature_version": FEATURE_VERSION,
        "rows": features.height,
        "features": feature_names(),
        "input_batches": {
            t: sorted({f.stem for f in layer_files(args.lake, "cleaned", t)})
            for t in ("transactions", "customers")
        },
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    args.out.with_name("features_manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(f"features: {features.height:,} rows x {len(feature_names())} features -> {args.out}")
    return 0


def cmd_catalog(args) -> int:
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(render(), encoding="utf-8", newline="\n")
    print(f"catalog: {args.out}")
    return 0


def cmd_audit(args) -> int:
    txns, customers, labels = _load(args.lake)
    features = compute_features(txns, customers)
    as_of = txns["timestamp"].max()
    table = training_table(features, labels, as_of)
    # Only matured rows: recent rows carry hidden fraud, so their labels would mislead the audit.
    matured = as_of - timedelta(days=DISPUTE_WINDOW_DAYS)
    table = table.filter(pl.col("timestamp") <= matured)
    result = audit(table)
    with pl.Config(tbl_rows=60, tbl_cols=10, fmt_str_lengths=40):
        print(result)
    flagged = result.filter(pl.col("suspicious"))
    if flagged.height:
        print(f"WARNING: {flagged.height} suspicious feature(s): {flagged['feature'].to_list()}")
        return 1
    print("no feature separates the classes suspiciously well on its own")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="python -m mmrisk.features")
    sub = p.add_subparsers(dest="command", required=True)
    b = sub.add_parser("build", help="compute features for every transaction in the cleaned lake")
    b.add_argument("--lake", type=Path, default=Path("data/processed"))
    b.add_argument("--out", type=Path, default=Path("data/features/transaction_features.parquet"))
    b.set_defaults(func=cmd_build)
    c = sub.add_parser("catalog", help="write the feature catalog Markdown")
    c.add_argument("--out", type=Path, default=Path("docs/feature-catalog.md"))
    c.set_defaults(func=cmd_catalog)
    a = sub.add_parser("audit", help="leak-smell audit: single-feature AUC on matured labels")
    a.add_argument("--lake", type=Path, default=Path("data/processed"))
    a.set_defaults(func=cmd_audit)
    args = p.parse_args(argv)
    return args.func(args)
