"""CLI: python -m mmrisk.streaming run | summary"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

import polars as pl

from mmrisk.api.loading import read_lake
from mmrisk.api.service import ScoringService, ServiceError, load_model
from mmrisk.api.smoke import SmokeError
from mmrisk.streaming.events import FaultConfig, FaultConfigError, split_stream
from mmrisk.streaming.runner import StreamConfig, StreamError, run_stream
from mmrisk.streaming.scorers import HttpScorer, LocalScorer
from mmrisk.streaming.store import PredictionStore, StoreError, read

USER_ERRORS = (ServiceError, FaultConfigError, StreamError, StoreError, SmokeError, ValueError)
REPORT_FILE = "stream_run.json"


def _cmd_run(args) -> int:
    try:
        faults = FaultConfig(
            duplicate_rate=args.duplicate_rate, bad_rate=args.bad_rate,
            jitter_seconds=args.jitter_seconds, seed=args.seed,
        )  # fmt: skip
        cfg = StreamConfig(
            speed=args.speed, queue_size=args.queue_size, max_lateness=args.max_lateness,
            faults=faults,
        )  # fmt: skip
        if args.url and not args.start:
            raise StreamError(
                "--from is required with --url: the API must have replayed exactly the history "
                "before that time (serve --history-until <the same time>)"
            )
        start = datetime.fromisoformat(args.start) if args.start else None
        txns, customers = read_lake(args.lake)
        history, stream, start = split_stream(txns, start, args.last_days)
        if args.limit:
            stream = stream.head(args.limit)
        if stream.is_empty():
            raise StreamError(f"no transactions at or after {start.isoformat()}")
        store = None
        if not args.no_store:
            if PredictionStore.has_parts(args.store) and not args.append:
                raise StoreError(
                    f"{args.store} already holds predictions; use --append or another --store"
                )
            store = PredictionStore(args.store)
        if args.url:
            scorer = HttpScorer(args.url)
            if not scorer.ready():
                raise StreamError(f"the API at {args.url} is not ready (start it first)")
            print(
                f"API mode: the API must have replayed history before {start.isoformat()} only "
                f"(serve --history-until {start.isoformat()}) and must not have seen the stream."
            )
        else:
            scorer = LocalScorer(ScoringService(load_model(args.model_dir), customers, history))
    except USER_ERRORS as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if cfg.max_lateness < faults.jitter_seconds:
        print("warning: max lateness is below the jitter, so some events will arrive too late")
    print(
        f"streaming {stream.height} transactions from {start.isoformat()} "
        f"({history.height} earlier ones are history), scoring {scorer.name}, speed {args.speed}"
    )
    try:
        report = run_stream(stream.to_dicts(), scorer, cfg, store)
    except StreamError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    report["finished_at"] = datetime.now(UTC).isoformat(timespec="seconds")
    target = (
        Path(args.report) if args.report else (None if store is None else store.dir / REPORT_FILE)
    )
    if target is not None:
        target.write_text(json.dumps(report, indent=2) + "\n")
    _print(report, store, target)
    bad = report["outcomes"]["unavailable"] + report["outcomes"]["error"]
    if report["outcomes"]["scored"] + report["outcomes"]["duplicate"] == 0:
        print("error: nothing was scored", file=sys.stderr)
        return 1
    return 0 if report["balanced"] and not bad else 1


def _print(report: dict, store, target) -> None:
    o, a, b, lat, q, t = (
        report[k] for k in ("outcomes", "actions", "buffer", "latency_ms", "queue", "timing")
    )
    print(f"events in {report['events']['in']} {report['events']['by_kind']}")
    print(f"  scored {o['scored']}, repeats {o['duplicate']}, invalid {o['invalid']}, "
          f"out of order {o['out_of_order']}, unknown customer {o['unknown_customer']}, "
          f"conflicts {o['conflict']}, unavailable {o['unavailable']}, "
          f"errors {o['error']}")  # fmt: skip
    print(f"  buffer dropped {b['dropped_duplicate']} duplicates and {b['dropped_late']} too late "
          f"(at most {b['max_waiting']} waiting)")  # fmt: skip
    print(f"  actions {a}; flagged {report['flagged_share']:.2%}")
    print(
        f"  scorer latency p50 {lat['p50']:.1f} ms, p95 {lat['p95']:.1f} ms, "
        f"max {lat['max']:.1f} ms"
    )
    print(
        f"  queue peak {q['max_depth']}/{q['capacity']}, "
        f"producer waited {q['producer_blocked_seconds']} s"
    )
    print(f"  {t['scored_per_second']:.0f} scored per second over {t['wall_seconds']} s "
          f"({t['simulated_seconds'] / 3600:.1f} simulated hours)")  # fmt: skip
    print("balanced: " + ("every event is accounted for" if report["balanced"] else "NOT BALANCED"))
    if store is not None:
        print(f"predictions: {store.rows_written} rows in {store.dir}")
    if target is not None:
        print(f"report: {target}")


def _cmd_summary(args) -> int:
    df = read(args.store)
    if df.is_empty():
        print(f"no predictions in {args.store}")
        return 0
    print(f"{df.height} predictions, {df['transaction_id'].n_unique()} distinct transactions")
    print(f"event time {df['event_time'].min()} to {df['event_time'].max()}")
    by = df.group_by("action").agg(n=pl.len(), mean_prob=pl.col("risk_probability").mean())
    for r in by.sort("action").to_dicts():
        print(f"  {r['action']:<7} {r['n']:>8}  mean probability {r['mean_prob']:.4f}")
    flagged = df.filter(pl.col("action") != "ALLOW")
    print(f"flagged {flagged.height / df.height:.2%}")
    report = Path(args.store) / REPORT_FILE
    if report.exists():
        info = json.loads(report.read_text())
        print(f"last run: scorer {info['scorer']}, finished {info.get('finished_at', '?')}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m mmrisk.streaming", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="stream transactions through the scorer")
    r.add_argument("--lake", type=Path, default=Path("data/processed"))
    r.add_argument("--model-dir", type=Path, default=Path("models/advanced"))
    r.add_argument("--url", default=None, help="score through a running API instead of in-process")
    r.add_argument("--from", dest="start", default=None, help="ISO time the stream starts at")
    r.add_argument(
        "--last-days", type=float, default=7.0, help="stream the last N days (default 7)"
    )
    r.add_argument("--limit", type=int, default=0, help="stream at most this many transactions")
    r.add_argument("--speed", type=float, default=0.0,
                   help="simulated seconds per real second; 0 = as fast as possible")  # fmt: skip
    r.add_argument("--seed", type=int, default=42)
    r.add_argument("--duplicate-rate", type=float, default=0.0)
    r.add_argument("--bad-rate", type=float, default=0.0)
    r.add_argument("--jitter-seconds", type=float, default=0.0)
    r.add_argument("--max-lateness", type=float, default=60.0)
    r.add_argument("--queue-size", type=int, default=1000)
    r.add_argument("--store", type=Path, default=Path("data/predictions"))
    r.add_argument("--no-store", action="store_true")
    r.add_argument(
        "--append", action="store_true", help="add to a store that already has predictions"
    )
    r.add_argument("--report", default=None, help="where to write the run report (JSON)")
    s = sub.add_parser("summary", help="describe a prediction store")
    s.add_argument("--store", type=Path, default=Path("data/predictions"))
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return {"run": _cmd_run, "summary": _cmd_summary}[args.cmd](args)


if __name__ == "__main__":
    raise SystemExit(main())
