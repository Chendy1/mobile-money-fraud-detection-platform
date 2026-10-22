"""python -m mmrisk.monitoring baseline | report

Exit codes: 0 PASS or WARN, 1 FAIL, 2 a mistake in how it was called.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

from mmrisk.monitoring import config as cfg
from mmrisk.monitoring.checks import (
    FAIL,
    MonitoringError,
    overall,
    profile,
    run_checks,
)
from mmrisk.monitoring.report import markdown, text
from mmrisk.streaming import store as pstore

DEFAULT_STORE = Path("data/predictions")
DEFAULT_OUT = Path("data/monitoring")


def _write(path: Path, content: str) -> None:
    """Write through a temporary file so a reader never sees half a report."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, path)


def _load_store(path: Path):
    if not pstore.parts(path):
        raise MonitoringError(f"no predictions in {path} (run python -m mmrisk.streaming run)")
    return pstore.read(path)


def _baseline(path: Path | None) -> dict | None:
    if path is None:
        return None
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise MonitoringError(f"cannot read the baseline {path}: {exc}") from None
    need = {"flagged_share", "action_share", "rows_per_hour"}
    if not isinstance(data, dict) or not need <= data.keys() or "BLOCK" not in data["action_share"]:
        raise MonitoringError(f"{path} is not a baseline written by `baseline`")
    return data


def _cmd_baseline(args) -> int:
    base = profile(_load_store(args.store), args.window)
    out = args.out / "baseline.json"
    _write(out, json.dumps(base, indent=2) + "\n")
    print(f"baseline of {base['rows']} predictions written to {out}")
    print(f"flagged share {base['flagged_share']:.2%}, {base['rows_per_hour']:.1f} events per hour")
    return 0


def _cmd_report(args) -> int:
    df = _load_store(args.store)
    base = _baseline(args.baseline)
    as_of = datetime.fromisoformat(args.as_of) if args.as_of else None
    checks, table = run_checks(df, base, args.window, as_of, args.review_capacity_per_hour)
    print(text(checks))
    meta = {
        "store": str(args.store).replace("\\", "/"),
        "rows": df.height,
        "baseline": str(args.baseline).replace("\\", "/") if args.baseline else "none",
    }
    _write(args.out / "report.md", markdown(checks, table, args.window, meta))
    _write(
        args.out / "report.json",
        json.dumps({"overall": overall(checks), "checks": [c.__dict__ for c in checks]}, indent=2)
        + "\n",
    )
    print(f"report written to {args.out / 'report.md'}")
    return 1 if overall(checks) == FAIL else 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m mmrisk.monitoring", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)
    for name, helptext in (
        ("baseline", "freeze the statistics of a healthy store"),
        ("report", "check a store, optionally against a baseline"),
    ):
        s = sub.add_parser(name, help=helptext)
        s.add_argument("--store", type=Path, default=DEFAULT_STORE)
        s.add_argument("--window", default=cfg.DEFAULT_WINDOW, help="30m, 1h, 6h, 1d ...")
        s.add_argument("--out", type=Path, default=DEFAULT_OUT)
        if name == "report":
            s.add_argument("--baseline", type=Path, default=None)
            s.add_argument("--as-of", default=None, help="ISO time (UTC) for the freshness check")
            s.add_argument("--review-capacity-per-hour", type=float, default=None)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return {"baseline": _cmd_baseline, "report": _cmd_report}[args.cmd](args)
    except (MonitoringError, pstore.StoreError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
