"""Command line interface: python -m mmrisk.generation --help"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import date
from pathlib import Path

from mmrisk.generation.config import GeneratorConfig
from mmrisk.generation.generator import generate


def _parse_args(argv):
    d = GeneratorConfig()
    p = argparse.ArgumentParser(
        prog="python -m mmrisk.generation",
        description="Generate a reproducible SYNTHETIC mobile-money transaction dataset.",
    )
    p.add_argument("--num-customers", type=int, default=d.num_customers)
    p.add_argument("--num-transactions", type=int, default=d.num_transactions)
    p.add_argument("--fraud-rate", type=float, default=d.fraud_rate)
    p.add_argument("--seed", type=int, default=d.seed)
    p.add_argument("--start-date", type=date.fromisoformat, default=d.start_date)
    p.add_argument("--end-date", type=date.fromisoformat, default=d.end_date)
    p.add_argument("--output-dir", type=Path, default=d.output_dir)
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = _parse_args(argv)
    cfg = GeneratorConfig(
        num_customers=args.num_customers,
        num_transactions=args.num_transactions,
        fraud_rate=args.fraud_rate,
        seed=args.seed,
        start_date=args.start_date,
        end_date=args.end_date,
        output_dir=args.output_dir,
    )
    try:
        cfg.validate()
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    t0 = time.perf_counter()
    frames = generate(cfg)
    secs = time.perf_counter() - t0
    txn = frames["transactions"]
    print(f"Wrote {len(frames)} tables to {cfg.output_dir} in {secs:.1f}s")
    print(f"transactions: {txn.height:,} rows; true fraud rows: {cfg.num_fraud:,}")
    print("All data is synthetic. See docs/data-dictionary.md.")
    return 0
