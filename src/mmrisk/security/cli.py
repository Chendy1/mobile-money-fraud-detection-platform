"""python -m mmrisk.security keys | dash-password | audit | scan

keys           create, list and revoke API keys (a key is shown once, when it is created)
dash-password  make the hash that protects the dashboard (asks for the password, never prints it)
audit          show the last audit-log entries
scan           look for committed secrets in a folder (exit 1 when something is found)

Exit codes: 0 done, 1 findings (scan), 2 a mistake.
"""

from __future__ import annotations

import argparse
import getpass
import json
import sys
from pathlib import Path

from mmrisk.security import passwords
from mmrisk.security.audit import AuditLog
from mmrisk.security.keys import KeyFileError, KeyStore
from mmrisk.security.scan import scan

DEFAULT_DIR = Path("data/security")


def _keys(args) -> int:
    store = KeyStore(args.file)
    audit = AuditLog(args.audit_log)
    if args.action == "create":
        token, record = store.create(args.name, args.role)
        audit.record("key_created", key_id=record["id"], role=record["role"], detail=record["name"])
        print(f"key {record['id']} ({record['role']}) created. Copy it now; it is not shown again:")
        print(token)
    elif args.action == "revoke":
        record = store.revoke(args.id)
        audit.record("key_revoked", key_id=record["id"], role=record["role"])
        print(f"key {record['id']} revoked")
    else:
        rows = store.keys()
        if not rows:
            print(f"no keys in {args.file}")
        for k in rows:
            state = f"revoked {k['revoked_at']}" if k["revoked_at"] else "active"
            print(f"{k['id']}  {k['role']:8} {state:32} {k['name']}")
    return 0


def _dash_password(args) -> int:
    first = getpass.getpass("dashboard password (at least 12 characters): ")
    if first != getpass.getpass("again: "):
        raise passwords.PasswordError("the two entries differ")
    hashed = passwords.hash_password(first)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(hashed + "\n", encoding="utf-8")
    print(f"hash written to {out}; start the dashboard with --password-file {out}")
    return 0


def _audit(args) -> int:
    rows = AuditLog(args.file).read(args.last)
    if not rows:
        print(f"no entries in {args.file}")
    for row in rows:
        print(json.dumps(row))
    return 0


def _scan(args) -> int:
    findings = scan(Path(args.root))
    for f in findings:
        print(f"{f.path}:{f.line}  {f.rule}  {f.preview}")
    print(f"{len(findings)} finding(s)" if findings else "no secrets found")
    return 1 if findings else 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m mmrisk.security", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)
    k = sub.add_parser("keys")
    k.add_argument("--file", type=Path, default=DEFAULT_DIR / "api_keys.json")
    k.add_argument("--audit-log", type=Path, default=DEFAULT_DIR / "audit.jsonl")
    ks = k.add_subparsers(dest="action", required=True)
    c = ks.add_parser("create")
    c.add_argument("--name", required=True)
    c.add_argument("--role", required=True, choices=["score", "metrics", "admin"])
    ks.add_parser("list")
    r = ks.add_parser("revoke")
    r.add_argument("id")
    d = sub.add_parser("dash-password")
    d.add_argument("--out", type=Path, default=DEFAULT_DIR / "dashboard.hash")
    a = sub.add_parser("audit")
    a.add_argument("--file", type=Path, default=DEFAULT_DIR / "audit.jsonl")
    a.add_argument("--last", type=int, default=50)
    s = sub.add_parser("scan")
    s.add_argument("--root", type=Path, default=Path("."))
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return {"keys": _keys, "dash-password": _dash_password, "audit": _audit,
                "scan": _scan}[args.cmd](args)  # fmt: skip
    except (KeyFileError, passwords.PasswordError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
