"""python -m mmrisk.dashboard run | check

run    starts the dashboard on this computer only (needs the `dashboard` extra)
check  prints which data files were found and what each page would show; no Streamlit needed

Exit codes: 0 done, 2 a mistake.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

from mmrisk.dashboard import data as D

APP = Path(__file__).with_name("app.py")
LOOPBACK = ("127.0.0.1", "localhost", "::1")


class UsageError(ValueError):
    """A mistake in how the command was called."""


def _sources(args) -> D.Sources:
    return D.Sources(args.store, args.baseline, args.drift_report, args.registry, args.retrain_dir)


def _env(src: D.Sources) -> dict:
    from mmrisk.dashboard.app import ENV

    env = dict(os.environ)
    env.update({ENV[k]: str(getattr(src, k)) for k in ENV})
    return env


def _check(args) -> int:
    src = _sources(args)
    for row in D.source_status(src):
        print(f"{'found  ' if row['found'] else 'missing'} {row['source']:20} {row['path']}")
    loaded = D.load_store(src.store)
    if loaded.ok:
        ov = D.overview(loaded.value, args.window)
        print(f"predictions: {ov['rows']} rows, flagged {ov['flagged_share']:.2%}, "
              f"{ov['first_event']} to {ov['last_event']}")  # fmt: skip
        h = D.health(loaded.value, src.monitoring_baseline, args.window)
        print(f"health: {h.value['overall'] if h.ok else h.message}")
    else:
        print(f"predictions: {loaded.message}")
    models = D.models_view(src.registry)
    if models.ok:
        prod = models.value.production
        print(f"models: {models.value.versions.height} versions, production "
              f"{prod['version'] if prod else 'none'}")  # fmt: skip
    else:
        print(f"models: {models.message}")
    return 0


def _run(args) -> int:
    if args.host not in LOOPBACK and not args.allow_external:
        raise UsageError(
            f"--host {args.host} would expose the dashboard to the network. It has no login "
            "(Phase 18) and shows transaction details. Use --allow-external only if you accept it."
        )
    if not 1 <= args.port <= 65535:
        raise UsageError("--port must be between 1 and 65535")
    try:
        import streamlit  # noqa: F401
    except ImportError:
        raise UsageError('streamlit is not installed: pip install -e ".[dashboard]"') from None
    cmd = [
        sys.executable, "-m", "streamlit", "run", str(APP),
        "--server.address", args.host, "--server.port", str(args.port),
        "--browser.gatherUsageStats", "false",
        "--server.headless", "true" if args.no_browser else "false",
    ]  # fmt: skip
    print(f"dashboard at http://{args.host}:{args.port} (Ctrl+C stops it)")
    return subprocess.call(cmd, env=_env(_sources(args)))


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m mmrisk.dashboard", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)
    for name in ("run", "check"):
        s = sub.add_parser(name)
        s.add_argument("--store", type=Path, default=D.Sources().store)
        s.add_argument("--baseline", type=Path, default=D.Sources().monitoring_baseline)
        s.add_argument("--drift-report", type=Path, default=D.Sources().drift_report)
        s.add_argument("--registry", type=Path, default=D.Sources().registry)
        s.add_argument("--retrain-dir", type=Path, default=D.Sources().retrain_dir)
        if name == "run":
            s.add_argument("--host", default="127.0.0.1")
            s.add_argument("--port", type=int, default=8501)
            s.add_argument("--allow-external", action="store_true")
            s.add_argument("--no-browser", action="store_true", help="do not open a browser tab")
        else:
            s.add_argument("--window", default="1d", choices=D.WINDOWS)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return _run(args) if args.cmd == "run" else _check(args)
    except UsageError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
