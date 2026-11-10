"""python -m mmrisk.cloudcost estimate [--scenario NAME|all] [--prices FILE] [--json]"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from mmrisk.cloudcost.model import (
    SCENARIOS,
    Assumptions,
    CostError,
    estimate,
    load_prices,
    render,
)

DEFAULT_PRICES = Path("infrastructure/cost/prices.json")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m mmrisk.cloudcost",
        description="Lower-bound monthly cost of the AWS design from prices read on AWS pages. "
        "An estimate, not a quote; nothing is created or called.",
    )
    sub = p.add_subparsers(dest="command", required=True)
    e = sub.add_parser("estimate", help="print the estimate for one scenario or all")
    e.add_argument("--scenario", choices=(*SCENARIOS, "all"), default="all")
    e.add_argument("--prices", type=Path, default=DEFAULT_PRICES)
    e.add_argument("--json", action="store_true")
    d = Assumptions()
    for name in ("session_hours", "ecr_gb", "pull_gb_per_start", "log_gb_month_always_on"):
        e.add_argument(f"--{name.replace('_', '-')}", type=float, default=getattr(d, name),
                       help=f"ASSUMED, default {getattr(d, name)}")  # fmt: skip
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        prices, not_priced = load_prices(args.prices)
        a = Assumptions(
            session_hours=args.session_hours,
            ecr_gb=args.ecr_gb,
            pull_gb_per_start=args.pull_gb_per_start,
            log_gb_month_always_on=args.log_gb_month_always_on,
        )
        names = SCENARIOS if args.scenario == "all" else (args.scenario,)
        results = [estimate(prices, not_priced, n, a) for n in names]
    except CostError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps([r.as_dict() for r in results], indent=2))
    else:
        print("\n\n".join(render(r) for r in results))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
