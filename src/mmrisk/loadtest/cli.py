"""python -m mmrisk.loadtest bench | run | inproc | show

bench   start the API on a free local port (fresh state), run a load profile, stop it, report
run     run a profile against an API you started yourself (it must have replayed only the history)
inproc  time the scoring code with no HTTP at all, to separate model time from server overhead
show    print a saved report

Only loopback addresses are loaded unless --allow-external is given: this is a load generator.
Exit codes: 0 done, 1 --fail-on-slo and a target was missed, 2 a mistake.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

from mmrisk.api.loading import read_lake
from mmrisk.api.service import ScoringService, ServiceError, load_model
from mmrisk.api.smoke import SmokeError, check_url
from mmrisk.loadtest import plan, report, stats
from mmrisk.loadtest.runner import HttpSender, run
from mmrisk.security.keys import KeyFileError, KeyStore
from mmrisk.streaming.events import split_stream

LOOPBACK = {"127.0.0.1", "localhost", "::1"}
KEY_VAR = "MMRISK_LOADTEST_KEY"
USER_ERRORS = (plan.PlanError, ServiceError, SmokeError, KeyFileError, ValueError, OSError)
BOOT_SECONDS = 120


class LoadTestError(ValueError):
    """The run cannot be done as asked."""


# ---- shared pieces ----


def _stream(args):
    txns, _ = read_lake(args.lake)
    start = datetime.fromisoformat(args.start) if args.start else None
    _, stream, start = split_stream(txns, start, args.last_days)
    return start, stream.to_dicts()


def _plan_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--profile", choices=plan.PROFILES, default="step")
    p.add_argument("--rate", type=float, default=10.0, help="constant rate; ramp start; spike base")
    p.add_argument("--rates", default="5,10,20,40", help="step profile: comma-separated, rising")
    p.add_argument("--peak", type=float, default=0.0, help="ramp end; spike peak")
    p.add_argument("--seconds", type=float, default=30.0, help="constant and ramp duration")
    p.add_argument("--step-seconds", type=float, default=10.0, help="each step; each spike part")
    p.add_argument("--arrivals", choices=plan.ARRIVALS, default="even")
    p.add_argument("--workers", type=int, default=8)
    p.add_argument("--warmup", type=int, default=20, help="requests sent first and not counted")
    p.add_argument("--timeout", type=float, default=10.0)
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--lake", type=Path, default=Path("data/processed"))
    p.add_argument("--from", dest="start", default=None, help="stream start (ISO time)")
    p.add_argument("--last-days", type=float, default=7.0)
    p.add_argument("--name", default=None)
    p.add_argument("--out-dir", type=Path, default=Path("data/loadtest"))
    p.add_argument("--p95-ms", type=float, default=stats.SLO_P95_MS)
    p.add_argument("--p99-ms", type=float, default=stats.SLO_P99_MS)
    p.add_argument("--max-error-rate", type=float, default=stats.SLO_MAX_ERROR_RATE)
    p.add_argument("--fail-on-slo", action="store_true", help="exit 1 when a target is missed")


def _segments(args) -> list[plan.Segment]:
    rates = (
        tuple(float(x) for x in args.rates.split(",") if x.strip())
        if args.profile == "step"
        else ()
    )
    return plan.segments_for(
        args.profile, rate=args.rate, rates=rates, seconds=args.seconds,
        step_seconds=args.step_seconds, peak=args.peak,
    )  # fmt: skip


def _slo(args) -> stats.Slo:
    if args.p95_ms <= 0 or args.p99_ms <= 0 or not 0 <= args.max_error_rate < 1:
        raise LoadTestError("targets must be above 0, and the error rate in [0, 1)")
    return stats.Slo(args.p95_ms, args.p99_ms, args.max_error_rate)


def _execute(args, url: str, key: str | None, stream: list[dict], mode: str, extra: dict) -> int:
    if args.warmup < 0 or args.workers < 1:
        raise LoadTestError("--warmup must not be negative and --workers must be at least 1")
    segments, slo = _segments(args), _slo(args)
    warm, rest = stream[: args.warmup], stream[args.warmup :]
    requests = plan.build(rest, segments, args.workers, args.arrivals, args.seed)
    planned = [sum(1 for r in requests if r.segment == i) for i in range(len(segments))]
    sender = HttpSender(url, key, args.timeout)
    for txn in warm:  # one at a time, in order: the first requests pay start-up costs
        if sender(txn)[0] == 0:
            raise LoadTestError(f"no answer from {url}; is the API running and ready?")
    sender.close()
    seconds = segments[-1].end
    print(f"sending {len(requests)} requests over {seconds:.0f} s with {args.workers} workers...")
    samples, not_sent = run(requests, sender, args.workers)
    settings = {
        "profile": args.profile, "segments": [(s.start, s.end, round(s.rate, 2)) for s in segments],
        "arrivals": args.arrivals, "workers": args.workers, "warmup_requests": len(warm),
        "stream_from": extra.pop("start"), "seed": args.seed, "timeout_s": args.timeout,
        **extra,
    }  # fmt: skip
    name = args.name or f"{mode}-{datetime.now(UTC):%Y%m%d-%H%M%S}"
    server = _server_count(
        url, key, ok_expected=len(warm) + sum(1 for s in samples if s.status == 200)
    )
    rep = report.build(
        name=name, mode=mode, settings=settings, segments=segments, samples=samples,
        planned=planned, not_sent=not_sent, slo=slo, server=server,
    )  # fmt: skip
    j, m = report.write(rep, args.out_dir)
    print(report.markdown(rep))
    print(f"wrote {j} and {m}")
    missed = any(not r["slo"]["met"] for r in rep["segments"])
    return 1 if args.fail_on_slo and missed else 0


_SCORE_OK = re.compile(
    r'^mmrisk_http_requests_total\{[^}]*endpoint="/v1/score"[^}]*status="200"[^}]*\}\s+([0-9.e+]+)',
    re.M,
)


def _server_count(url: str, key: str | None, ok_expected: int) -> dict | None:
    """Ask the server how many scoring requests it answered with 200, and compare."""
    req = urllib.request.Request(url.rstrip("/") + "/metrics")
    if key:
        req.add_header("authorization", f"Bearer {key}")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310
            text = resp.read().decode("utf-8", "replace")
    except OSError:
        return None  # no key for /metrics, or metrics off: the cross-check is simply absent
    found = [float(x) for x in _SCORE_OK.findall(text)]
    if not found:
        return None
    counted = int(sum(found))
    return {"server_200_responses": counted, "client_200_responses": ok_expected,
            "agree": counted == ok_expected}  # fmt: skip


# ---- commands ----


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_ready(url: str, proc: subprocess.Popen, log: Path) -> None:
    deadline = time.monotonic() + BOOT_SECONDS
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            tail = log.read_text(errors="replace")[-800:]
            raise LoadTestError(f"the API exited while starting (code {proc.returncode}):\n{tail}")
        try:
            with urllib.request.urlopen(url + "/ready", timeout=2) as r:  # noqa: S310
                if r.status == 200:
                    return
        except OSError:
            pass
        time.sleep(0.5)
    raise LoadTestError(f"the API was not ready after {BOOT_SECONDS} s")


def stop_process(proc: subprocess.Popen) -> None:
    """Stop a server we started, and everything it started.

    On Windows a virtual environment's python.exe is a small launcher that starts the real
    interpreter as a child: ending only the launcher leaves the server running, still holding its
    port and its log file. taskkill /T ends the whole tree.
    """
    if proc.poll() is None:
        if os.name == "nt":
            subprocess.run(  # noqa: S603
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True, check=False
            )
        else:
            proc.terminate()
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=15)


def _bench(args) -> int:
    start, stream = _stream(args)
    port = _free_port()
    url = f"http://127.0.0.1:{port}"
    # a folder that cannot be removed (an antivirus scan, a late handle) must not change the result
    with tempfile.TemporaryDirectory(prefix="mmrisk-bench-", ignore_cleanup_errors=True) as tmp:
        tmp = Path(tmp)
        cmd = [sys.executable, "-m", "mmrisk.api", "serve", "--model-dir", str(args.model_dir),
               "--lake", str(args.lake), "--port", str(port), "--history-until", start.isoformat(),
               "--log-level", "warning"]  # fmt: skip
        key = None
        if args.auth:
            token, _ = KeyStore(tmp / "keys.json").create("loadtest", "admin")
            key = token
            cmd += ["--auth-keys", str(tmp / "keys.json"), "--audit-log", str(tmp / "audit.jsonl"),
                    "--rate-per-minute", str(args.rate_per_minute),
                    "--rate-burst", str(args.rate_burst)]  # fmt: skip
        log = tmp / "server.log"
        print(f"starting the API on {url} with history before {start.isoformat()} ...")
        with open(log, "w") as out:
            proc = subprocess.Popen(cmd, stdout=out, stderr=subprocess.STDOUT)  # noqa: S603
            try:
                _wait_ready(url, proc, log)
                extra = {"start": start.isoformat(), "auth": bool(args.auth)}
                if args.auth:
                    extra["rate_limit_per_minute"] = args.rate_per_minute
                return _execute(args, url, key, stream, "bench", extra)
            finally:
                stop_process(proc)


def _run(args) -> int:
    url = check_url(args.url)
    host = urlparse(url).hostname
    if host not in LOOPBACK and not args.allow_external:
        raise LoadTestError(
            f"{host} is not a local address. This sends sustained load: add --allow-external "
            "only for a service you own and may load."
        )
    if not args.start:
        raise LoadTestError(
            "--from is required: the API must have replayed exactly the history before that "
            "time (serve --history-until <the same time>) and not seen this stream yet"
        )
    start, stream = _stream(args)
    key = os.environ.get(KEY_VAR) or None
    print(f"note: this changes the API's state (it scores {len(stream)} candidate transactions); "
          "restart it before another run.")  # fmt: skip
    return _execute(args, url, key, stream, "run", {"start": start.isoformat(), "auth": bool(key)})


def _inproc(args) -> int:
    start, stream = _stream(args)
    txns, customers = read_lake(args.lake)
    history, _, _ = split_stream(txns, start, args.last_days)
    service = ScoringService(load_model(args.model_dir), customers, history)
    n = min(args.requests, len(stream))
    if n < 1:
        raise LoadTestError("there are no transactions to time")
    for txn in stream[:20]:  # warm-up, not counted
        service.score(txn)
    ms = []
    for txn in stream[20 : 20 + n]:
        t0 = time.perf_counter()
        service.score(txn)
        ms.append((time.perf_counter() - t0) * 1000)
    mean = sum(ms) / len(ms)
    rep = {
        "name": args.name or f"inproc-{datetime.now(UTC):%Y%m%d-%H%M%S}", "mode": "inproc",
        "created_utc": datetime.now(UTC).isoformat(timespec="seconds"),
        "machine": report.machine(), "caveat": report.CAVEAT,
        "scored": len(ms), "latency_ms": {"mean": mean, "p50": stats.percentile(ms, 50),
        "p95": stats.percentile(ms, 95), "p99": stats.percentile(ms, 99), "max": max(ms)},
        "single_thread_ceiling_per_s": 1000 / mean,
        "note": "ScoringService.score with no HTTP, validation or auth: model and feature "
                "time only. The service holds one lock, so scoring is one at a time.",
    }  # fmt: skip
    args.out_dir.mkdir(parents=True, exist_ok=True)
    path = args.out_dir / f"{rep['name']}.json"
    path.write_text(json.dumps(rep, indent=2), encoding="utf-8")
    lat = rep["latency_ms"]
    print(
        f"{len(ms)} transactions scored in process: mean {lat['mean']:.2f} ms, "
        f"p50 {lat['p50']:.2f}, p95 {lat['p95']:.2f}, p99 {lat['p99']:.2f}, max {lat['max']:.2f}"
    )
    print(
        f"one thread at a time: about {rep['single_thread_ceiling_per_s']:.0f} per second at most"
    )
    print(f"wrote {path}")
    return 0


def _show(args) -> int:
    try:
        rep = json.loads(Path(args.file).read_text(encoding="utf-8"))
        print(report.markdown(rep) if "segments" in rep else json.dumps(rep, indent=2))
    except (OSError, ValueError, KeyError) as exc:
        raise LoadTestError(f"cannot read {args.file}: {exc}") from None
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m mmrisk.loadtest", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("bench")
    _plan_args(b)
    b.add_argument("--model-dir", type=Path, default=Path("models/advanced"))
    b.add_argument("--auth", action="store_true", help="run the API with API keys on")
    b.add_argument("--rate-per-minute", type=float, default=6_000_000.0)
    b.add_argument("--rate-burst", type=int, default=100_000)
    r = sub.add_parser("run")
    _plan_args(r)
    r.add_argument("--url", default="http://127.0.0.1:8000")
    r.add_argument("--allow-external", action="store_true")
    i = sub.add_parser("inproc")
    i.add_argument("--model-dir", type=Path, default=Path("models/advanced"))
    i.add_argument("--lake", type=Path, default=Path("data/processed"))
    i.add_argument("--from", dest="start", default=None)
    i.add_argument("--last-days", type=float, default=7.0)
    i.add_argument("--requests", type=int, default=500)
    i.add_argument("--name", default=None)
    i.add_argument("--out-dir", type=Path, default=Path("data/loadtest"))
    s = sub.add_parser("show")
    s.add_argument("file", type=Path)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return {"bench": _bench, "run": _run, "inproc": _inproc, "show": _show}[args.cmd](args)
    except (*USER_ERRORS, LoadTestError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("stopped", file=sys.stderr)
        return 130
