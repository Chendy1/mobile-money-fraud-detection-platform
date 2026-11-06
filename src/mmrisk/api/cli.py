"""CLI: python -m mmrisk.api serve | check"""

from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime
from pathlib import Path

import numpy as np

from mmrisk.api.loading import build_service, read_lake
from mmrisk.api.service import ScoringService, ServiceError, load_model

LOOPBACK = ("127.0.0.1", "localhost", "::1")


class SecurityOptionError(ValueError):
    """A combination of security options that is refused (the message says what to change)."""


def _security(args):
    """(Security or None, TLS on?) from the command line, or raise SecurityOptionError."""
    from mmrisk.security.apiauth import Security
    from mmrisk.security.audit import AuditLog
    from mmrisk.security.keys import KeyFileError, KeyStore
    from mmrisk.security.ratelimit import RateLimitError, TokenBucket

    if bool(args.tls_cert) != bool(args.tls_key):
        raise SecurityOptionError("--tls-cert and --tls-key go together")
    tls = bool(args.tls_cert)
    for path in (args.tls_cert, args.tls_key):
        if path and not Path(path).is_file():
            raise SecurityOptionError(f"{path} does not exist")
    if args.host not in LOOPBACK and not args.allow_insecure_external:
        missing = [m for m, absent in (("--auth-keys", not args.auth_keys), ("--tls-cert and "
                   "--tls-key", not tls)) if absent]  # fmt: skip
        if not args.allow_external or missing:
            need = " and ".join(missing) or "--allow-external"
            raise SecurityOptionError(
                f"refusing to listen on {args.host}: add {need}"
                + (" and --allow-external" if missing and not args.allow_external else "")
                + ", or use 127.0.0.1. (--allow-insecure-external overrides this; the API then "
                "has no protection.)"
            )
    security = None
    if args.auth_keys:
        store = KeyStore(args.auth_keys)
        try:
            if store.active_count() == 0:
                raise SecurityOptionError(
                    f"no active API key in {args.auth_keys}; make one with: "
                    "python -m mmrisk.security keys create --name <name> --role score"
                )
            bucket = TokenBucket(args.rate_per_minute, args.rate_burst)
        except (KeyFileError, RateLimitError) as exc:
            raise SecurityOptionError(str(exc)) from None
        audit = AuditLog(args.audit_log)
        security = Security(store, audit, bucket)
        audit.record("startup", detail=f"auth on, tls {'on' if tls else 'off'}, host {args.host}")
    return security, tls


def _cmd_serve(args) -> int:
    try:
        import uvicorn

        from mmrisk.api.app import create_app
    except ImportError:
        print('error: the API needs its extra: pip install -e ".[api]"', file=sys.stderr)
        return 2
    try:
        security, tls = _security(args)
    except SecurityOptionError as exc:
        print(f"error: {exc}", file=sys.stderr)
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
    if args.host not in LOOPBACK and (security is None or not tls):
        missing = " and ".join(
            m for m, absent in (("API keys", security is None), ("TLS", not tls)) if absent
        )
        print(f"WARNING: listening on a non-local address without {missing}.")
    scheme = "https" if tls else "http"
    docs = (
        "docs at /docs unless --no-docs" if security is None else "docs are off: keys are required"
    )
    print(f"listening on {scheme}://{args.host}:{args.port} ({docs})")
    app = create_app(service, docs=not args.no_docs, security=security)
    ssl = {"ssl_certfile": str(args.tls_cert), "ssl_keyfile": str(args.tls_key)} if tls else {}
    uvicorn.run(app, host=args.host, port=args.port, log_level=args.log_level, **ssl)
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


def _cmd_smoke(args) -> int:
    from mmrisk.api.smoke import SmokeError, run_smoke

    key = os.environ.get("MMRISK_API_KEY") or None  # never a flag: it would end up in history
    try:
        results = run_smoke(args.url, args.timeout, key, args.cafile)
    except SmokeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    for name, ok, detail in results:
        print(f"  {'PASS' if ok else 'FAIL'}  {name} ({detail})")
    passed = all(ok for _, ok, _ in results)
    print("smoke: " + ("PASS" if passed else "FAIL"))
    return 0 if passed else 1


def build_parser() -> argparse.ArgumentParser:
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
                help="listen on a non-local address (needs --auth-keys, --tls-cert, --tls-key)",
            )
            s.add_argument(
                "--allow-insecure-external",
                action="store_true",
                help="listen on a non-local address with no keys or TLS (the container does)",
            )
            s.add_argument(
                "--auth-keys",
                type=Path,
                default=os.environ.get("MMRISK_API_KEYS_FILE") or None,
                help="require API keys from this file (or MMRISK_API_KEYS_FILE); turns /docs off",
            )
            s.add_argument("--audit-log", type=Path, default=Path("data/security/audit.jsonl"))
            s.add_argument("--rate-per-minute", type=float, default=600.0)
            s.add_argument("--rate-burst", type=int, default=50)
            s.add_argument("--tls-cert", type=Path, default=None, help="PEM certificate file")
            s.add_argument("--tls-key", type=Path, default=None, help="PEM private key file")
            s.add_argument(
                "--history-until",
                default=os.environ.get("MMRISK_HISTORY_UNTIL") or None,
                help="replay only transactions before this ISO time (default: all; "
                "or the MMRISK_HISTORY_UNTIL environment variable)",
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
    m = sub.add_parser("smoke", help="non-mutating checks of a running API")
    m.add_argument("--url", default="http://127.0.0.1:8000")
    m.add_argument("--timeout", type=float, default=5.0)
    m.add_argument(
        "--cafile", default=None, help="trust this certificate (a self-signed https API)"
    )
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return {"serve": _cmd_serve, "check": _cmd_check, "smoke": _cmd_smoke}[args.cmd](args)


if __name__ == "__main__":
    raise SystemExit(main())
