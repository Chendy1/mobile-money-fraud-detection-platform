"""python -m mmrisk.registry list | show | bootstrap | promote | rollback | verify

Exit codes: 0 done, 2 a mistake or a refused operation.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from mmrisk.registry.store import Registry, RegistryError

DEFAULT_ROOT = Path("models/registry")


def _list(reg: Registry, _args) -> int:
    prod = reg.production()
    rows = reg.versions()
    if not rows:
        print(f"no versions in {reg.root}")
        return 0
    print(f"{'version':8} {'stage':9} {'created':25} {'source':10} {'quick':5} note")
    for r in rows:
        mark = " <- production" if prod and prod["version"] == r["version"] else ""
        print(f"{r['version']:8} {r['stage']:9} {r['created_at']:25} {r['source']:10} "
              f"{'yes' if r['quick'] else 'no':5} {r['note']}{mark}")  # fmt: skip
    return 0


def _show(reg: Registry, args) -> int:
    print(json.dumps(reg.get(args.version), indent=2))
    return 0


def _bootstrap(reg: Registry, args) -> int:
    if reg.production() is not None:
        raise RegistryError("this registry already has a production model; use retrain instead")
    version = reg.register(
        args.model_dir, source="bootstrap", stage="approved", note="first model, not gated"
    )
    reg.promote(version, "bootstrap: first model in the registry (no champion to compare with)")
    print(f"{args.model_dir} registered as {version} and promoted to production")
    return 0


def _promote(reg: Registry, args) -> int:
    reg.promote(args.version, args.reason, force=args.force)
    print(f"{args.version} is now the production model")
    return 0


def _rollback(reg: Registry, args) -> int:
    pointer = reg.rollback(args.reason)
    print(f"production is now {pointer['version']} (was {pointer['previous']})")
    return 0


def _verify(reg: Registry, args) -> int:
    for v in [args.version] if args.version else reg.ids():
        reg.verify(v)
        print(f"{v}: checksum matches")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m mmrisk.registry", description=__doc__)
    p.add_argument("--registry", type=Path, default=DEFAULT_ROOT)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    s = sub.add_parser("show")
    s.add_argument("version")
    s = sub.add_parser("bootstrap", help="register a saved model as the first production model")
    s.add_argument("--model-dir", type=Path, default=Path("models/advanced"))
    s = sub.add_parser("promote")
    s.add_argument("version")
    s.add_argument("--reason", required=True)
    s.add_argument("--force", action="store_true", help="promote a version that is not approved")
    s = sub.add_parser("rollback")
    s.add_argument("--reason", required=True)
    s = sub.add_parser("verify")
    s.add_argument("version", nargs="?")
    return p


COMMANDS = {"list": _list, "show": _show, "bootstrap": _bootstrap, "promote": _promote,
            "rollback": _rollback, "verify": _verify}  # fmt: skip


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return COMMANDS[args.cmd](Registry(args.registry), args)
    except RegistryError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
