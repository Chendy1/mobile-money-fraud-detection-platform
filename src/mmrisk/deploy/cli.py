"""python -m mmrisk.deploy init | preflight | verify | backup | restore

init       make the secrets (API keys, TLS certificate, passwords) under ./secrets
preflight  check the setup before `docker compose up` (exit 1 on a failure)
verify     check a running stack from outside: keys, roles, TLS, metrics
backup     archive the registry, records and audit log, with a manifest of hashes
restore    verify an archive, then restore it (never over existing files without --force)

Exit codes: 0 done, 1 a check failed, 2 a mistake.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from mmrisk.deploy import (
    DeployError,
    checks,
)
from mmrisk.deploy import (
    backup as B,
)
from mmrisk.deploy import (
    preflight as P,
)
from mmrisk.deploy import (
    secrets_init as S,
)
from mmrisk.deploy import (
    verify as V,
)

COMPOSE = Path("docker-compose.prod.yml")
SECRETS = Path("secrets")


def _init(args) -> int:
    made = S.init(args.secrets, args.force)
    print(f"secrets written to {args.secrets}. Copy these now; they are not shown again:")
    print(f"  score key (for applications):  {made.score_key}")
    print(f"  admin key (for you):           {made.admin_key}")
    print(f"  dashboard password:            {made.dashboard_password}")
    print(f"The metrics key and the Grafana password are files in {args.secrets} (Prometheus and "
          "Grafana read them there).")  # fmt: skip
    print(f"TLS certificate (self-signed, for localhost and api) valid until {made.cert_expires}.")
    return 0


def _preflight(args) -> int:
    results = P.run_all(
        args.root, args.secrets, args.compose, args.model_dir, args.lake, args.running
    )
    for c in results:
        print(c.line())
    print(checks.summary(results))
    return checks.verdict(results, args.strict)


def _verify(args) -> int:
    score = os.environ.get("MMRISK_API_KEY")
    if not score:
        raise DeployError("set MMRISK_API_KEY to a score or admin key (an environment variable, "
                          "not a flag: a flag would stay in your shell history)")  # fmt: skip
    metrics = args.metrics_key_file.read_text(encoding="utf-8").strip()
    results = V.verify(args.url, args.cafile, score, metrics, args.timeout)
    for c in results:
        print(c.line())
    print(checks.summary(results))
    return checks.verdict(results)


def _backup(args) -> int:
    done = B.backup(args.root, args.out, args.with_predictions, args.include_secrets)
    print(f"wrote {done['archive']}: {done['files']} files, {done['bytes']} bytes")
    if done["skipped_empty"]:
        print(f"nothing found in: {', '.join(done['skipped_empty'])}")
    if done["includes_secrets"]:
        print("WARNING: the archive holds your secrets and is not encrypted. Keep it private.")
    return 0


def _restore(args) -> int:
    done = B.restore(args.archive, args.target, args.force, args.include_secrets, args.dry_run)
    print(done)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m mmrisk.deploy",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = p.add_subparsers(dest="cmd", required=True)
    i = sub.add_parser("init")
    i.add_argument("--secrets", type=Path, default=SECRETS)
    i.add_argument("--force", action="store_true")
    f = sub.add_parser("preflight")
    f.add_argument("--root", type=Path, default=Path("."))
    f.add_argument("--secrets", type=Path, default=SECRETS)
    f.add_argument("--compose", type=Path, default=COMPOSE)
    f.add_argument("--model-dir", type=Path, default=Path("models/registry"))
    f.add_argument("--lake", type=Path, default=Path("data/processed"))
    f.add_argument("--running", action="store_true", help="the stack is up: do not check the ports")
    f.add_argument("--strict", action="store_true", help="warnings also fail")
    v = sub.add_parser("verify")
    v.add_argument("--url", default="https://127.0.0.1:8443")
    v.add_argument("--cafile", type=Path, default=SECRETS / "tls" / "cert.pem")
    v.add_argument("--metrics-key-file", type=Path, default=SECRETS / "metrics_key")
    v.add_argument("--timeout", type=float, default=5.0)
    b = sub.add_parser("backup")
    b.add_argument("--root", type=Path, default=Path("."))
    b.add_argument("--out", type=Path, required=True)
    b.add_argument("--with-predictions", action="store_true")
    b.add_argument("--include-secrets", action="store_true")
    r = sub.add_parser("restore")
    r.add_argument("archive", type=Path)
    r.add_argument("--target", type=Path, default=Path("."))
    r.add_argument("--force", action="store_true")
    r.add_argument("--include-secrets", action="store_true")
    r.add_argument("--dry-run", action="store_true", help="verify the archive, write nothing")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return {"init": _init, "preflight": _preflight, "verify": _verify, "backup": _backup,
                "restore": _restore}[args.cmd](args)  # fmt: skip
    except (DeployError, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("stopped", file=sys.stderr)
        return 130
