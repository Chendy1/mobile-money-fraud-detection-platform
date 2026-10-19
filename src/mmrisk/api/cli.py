"""CLI: python -m mmrisk.api serve | check"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime
from pathlib import Path

import numpy as np

from mmrisk.api.loading import build_service, read_lake
from mmrisk.api.service import ScoringService, ServiceError, load_model

LOOPBACK = ("127.0.0.1", "localhost", "::1")


def _cmd_serve(args) -> int:
    try:
        import uvicorn

        from mmrisk.api.app import create_app
    except ImportError:
        print('error: the API needs its extra: pip install -e ".[api]"', file=sys.stderr)
        return 2
    if args.host not in LOOPBACK and not args.allow_external:
        print(
            f"error: refusing to listen on {args.host}: the API has no authentication or TLS. "
            "Use 127.0.0.1, or pass --allow-external if you accept that.",
            file=sys.stderr,
        )
        return 2
    try:
        until = datetime.fromisoformat(args.history_until) if args.history_until else None
        service = build_service(args.model_dir, args.lake, until, not args.no_history)
    except (ServiceError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    info = service.info()
    print(
        f"model {info['scorer']} (features {info['feature_version']}), {info['customers']} "
        f"customers, {info['history_transactions']} past transactions replayed"
    )
    if args.host not in LOOPBACK:
        print("WARNING: listening on a non-local address with no authentication or TLS.")
    print(f"listening on http://{args.host}:{args.port} (docs at /docs unless --no-docs)")
    app = create_app(service, docs=not args.no_docs)
    uvicorn.run(app, host=args.host, port=args.port, log_level=args.log_level)
    return 0


def _cmd_check(args) -> int:
    """Stream the last transactions through the service and compare with batch scoring."""
    from mmrisk.features import compute_features

    try:
        model = load_model(args.model_dir)
        txns, customers = read_lake(args.lake)
    except ServiceError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if args.n < 1 or args.n >= txns.height:
        print(
            "error: --n must be at least 1 and smaller than the number of transactions",
            file=sys.stderr,
        )
        return 2
    ordered = txns.sort("timestamp", "transaction_id")
    history, held = ordered.head(ordered.height - args.n), ordered.tail(args.n)
    service = ScoringService(model, customers, history)
    millis, got = [], {}
    for row in held.to_dicts():
        t0 = time.perf_counter()
        out = service.score(row)
        millis.append((time.perf_counter() - t0) * 1000)
        got[out["transaction_id"]] = out
    feats = compute_features(txns, customers)
    batch = model.assess(feats.filter(feats["transaction_id"].is_in(list(got))))
    diff = max(
        abs(r["risk_probability"] - got[r["transaction_id"]]["risk_probability"])
        for r in batch.to_dicts()
    )
    same = all(r["action"] == got[r["transaction_id"]]["action"] for r in batch.to_dicts())
    actions = {
        a: sum(1 for o in got.values() if o["action"] == a) for a in ("ALLOW", "REVIEW", "BLOCK")
    }
    q = np.percentile(millis, [50, 95])
    print(f"scored {len(got)} held-out transactions after replaying {history.height} earlier ones")
    print(f"  largest probability difference from batch scoring: {diff:.2e}")
    print(f"  same action as batch scoring: {same}")
    print(f"  actions: {actions}")
    print(
        f"  latency per transaction (this machine, one process): p50 {q[0]:.1f} ms, "
        f"p95 {q[1]:.1f} ms, max {max(millis):.1f} ms"
    )
    ok = diff < 1e-9 and same
    print("check: " + ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="python -m mmrisk.api", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)
    for name in ("serve", "check"):
        s = sub.add_parser(name)
        s.add_argument("--model-dir", type=Path, default=Path("models/advanced"))
        s.add_argument("--lake", type=Path, default=Path("data/processed"))
        if name == "serve":
            s.add_argument("--host", default="127.0.0.1")
            s.add_argument("--port", type=int, default=8000)
            s.add_argument(
                "--allow-external",
                action="store_true",
                help="listen on a non-local address (no authentication or TLS!)",
            )
            s.add_argument(
                "--history-until",
                default=None,
                help="replay only transactions before this ISO time (default: all)",
            )
            s.add_argument(
                "--no-history", action="store_true", help="start with no past transactions"
            )
            s.add_argument(
                "--no-docs", action="store_true", help="turn off /docs and /openapi.json"
            )
            s.add_argument("--log-level", default="info")
        else:
            s.add_argument(
                "--n", type=int, default=300, help="how many final transactions to hold out"
            )
    args = p.parse_args(argv)
    return {"serve": _cmd_serve, "check": _cmd_check}[args.cmd](args)


if __name__ == "__main__":
    raise SystemExit(main())
