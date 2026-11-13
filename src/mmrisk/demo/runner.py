"""Run demo steps: print the talking points, run the commands, check the output."""

from __future__ import annotations

import os
import re
import socket
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path

from mmrisk.demo import Paths, Result, Step

Runner = Callable[[list[str], Path, int], tuple[int, str]]


def run_module(argv: list[str], cwd: Path, timeout: int) -> tuple[int, str]:
    """`python -m <argv...>`; stdout and stderr together; the demo's own interpreter."""
    env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
    try:
        done = subprocess.run(  # noqa: S603
            [sys.executable, "-m", *argv],
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return 124, f"timed out after {timeout} s"
    return done.returncode, (done.stdout or "") + (done.stderr or "")


# ---- built-in actions: each returns (ok, text) -------------------------------------------------


def action_prereq(p: Paths) -> tuple[bool, str]:
    checks = [
        (p.lake / "cleaned", "the cleaned lake layer", "dev.ps1 generate ; dev.ps1 ingest"),
        (
            p.model_dir / "risk_model.json",
            "the trained model bundle",
            "dev.ps1 advanced (or pipeline)",
        ),
        (p.registry / "production.json", "a production model in the registry", "dev.ps1 regboot"),
        (p.card, "the model card", "dev.ps1 card"),
        (p.prices, "the cost prices file", "(it is part of the repository)"),
    ]
    lines, ok = [], True
    for path, what, fix in checks:
        found = p.under_repo(path).exists()
        ok &= found
        lines.append(
            f"{'found  ' if found else 'MISSING'} {what}: {path}"
            + ("" if found else f"   fix: {fix}")
        )
    return ok, "\n".join(lines)


def action_card(p: Paths) -> tuple[bool, str]:
    """The results table, read from the model card (never retyped)."""
    path = p.under_repo(p.card)
    if not path.exists():
        return False, f"missing: {p.card}"
    rows = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.startswith("|")]
    start = next((i for i, ln in enumerate(rows) if ln.startswith("| Scorer")), None)
    if start is None:
        return False, "no results table found in the model card"
    table = []
    for ln in rows[start:]:
        table.append(ln)
        if "LightGBM (shipped)" in ln:
            break
    return True, f"From {p.card} (synthetic data; costs ASSUMED):\n" + "\n".join(table)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _wait_ready(url: str, cafile: Path, proc: subprocess.Popen, seconds: int = 90) -> None:
    import ssl
    import urllib.request

    ctx = ssl.create_default_context(cafile=str(cafile))
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if proc.poll() is not None:
            raise RuntimeError(f"the API exited with code {proc.returncode}")
        try:
            with urllib.request.urlopen(url + "/ready", timeout=2, context=ctx) as r:  # noqa: S310
                if r.status == 200:
                    return
        except OSError:
            pass
        time.sleep(0.5)
    raise RuntimeError("the API was not ready in time")


def action_secure(p: Paths) -> tuple[bool, str]:
    """Start a TLS + keys API in a temp folder and run the same checks as `deploy verify`."""
    from mmrisk.deploy import secrets_init as S
    from mmrisk.deploy.verify import verify
    from mmrisk.loadtest.cli import stop_process

    with tempfile.TemporaryDirectory(prefix="mmrisk-demo-") as tmp:
        root = Path(tmp)
        created = S.init(root / "secrets")
        port = _free_port()
        cmd = [
            sys.executable, "-m", "mmrisk.api", "serve", "--host", "127.0.0.1", "--port", str(port),
            "--model-dir", str(p.under_repo(p.registry)), "--lake", str(p.under_repo(p.lake)),
            "--no-history", "--auth-keys", str(root / "secrets" / S.API_KEYS),
            "--tls-cert", str(root / "secrets" / S.CERT),
            "--tls-key", str(root / "secrets" / S.KEY),
            "--audit-log", str(root / "audit.jsonl"), "--log-level", "warning",
        ]  # fmt: skip
        log = root / "server.log"
        with open(log, "w") as out:
            proc = subprocess.Popen(cmd, stdout=out, stderr=subprocess.STDOUT)  # noqa: S603
            try:
                url = f"https://127.0.0.1:{port}"
                _wait_ready(url, root / "secrets" / S.CERT, proc)
                metrics = (root / "secrets" / S.METRICS_KEY).read_text().strip()
                results = verify(url, root / "secrets" / S.CERT, created.score_key, metrics)
            except RuntimeError as exc:
                return False, f"{exc}\n{log.read_text()[-800:]}"
            finally:
                stop_process(proc)
    return summarize_checks(results)


def summarize_checks(results) -> tuple[bool, str]:
    """One line per check and a count; ok only when every check passed (and there were some)."""
    lines = [f"{'PASS' if c.status == 'pass' else 'FAIL'}  {c.name}  {c.detail}" for c in results]
    passed = sum(c.status == "pass" for c in results)
    lines.append(
        f"{passed} of {len(results)} checks passed (history replay skipped to start quickly)"
    )
    return bool(results) and passed == len(results), "\n".join(lines)


ACTIONS = {"prereq": action_prereq, "card": action_card, "secure": action_secure}


# ---- the loop ----------------------------------------------------------------------------------


def trim(text: str, max_lines: int) -> str:
    lines = text.rstrip("\n").splitlines()
    if len(lines) <= max_lines:
        return "\n".join(lines)
    return "\n".join([*lines[:max_lines], f"... ({len(lines) - max_lines} more lines)"])


def run_step(step: Step, p: Paths, runner: Runner = run_module, timeout: int = 900) -> Result:
    started = time.monotonic()
    cwd = p.repo
    if step.action:
        try:
            ok, text = ACTIONS[step.action](p)
        except Exception as exc:  # a demo must report, not crash
            ok, text = False, f"{exc.__class__.__name__}: {exc}"
        codes_ok = ok
    else:
        chunks, codes_ok = [], True
        for argv in step.commands:
            code, text = runner(list(argv), cwd, timeout)
            chunks.append(text)
            if code not in step.ok_codes and not (
                len(step.commands) > 1 and argv is not step.commands[-1] and code == 0
            ):
                codes_ok = False
                chunks.append(
                    f"(exit code {code}; expected {' or '.join(map(str, step.ok_codes))})"
                )
                break
        text = "\n".join(chunks)
    seconds = time.monotonic() - started
    if not codes_ok:
        return Result(step.id, "failed", seconds, "command failed or exit code not expected", text)
    missing = [e for e in step.expect if not re.search(e, text)]
    if missing:
        return Result(step.id, "unexpected", seconds, "output lacks: " + "; ".join(missing), text)
    return Result(step.id, "ok", seconds, "", text)


def run_demo(
    steps: list[Step],
    p: Paths,
    *,
    pause: bool = True,
    max_lines: int = 30,
    runner: Runner = run_module,
    input_fn: Callable[[str], str] = input,
    out: Callable[[str], None] = print,
) -> list[Result]:
    results: list[Result] = []
    done: dict[str, str] = {}
    total = len(steps)
    for n, step in enumerate(steps, 1):
        out(f"\n=== Step {n} of {total}: {step.title}  (about {step.seconds} s as measured) ===")
        for line in step.say:
            out(f"SAY: {line}")
        blocked = [d for d in step.needs if done.get(d) != "ok"]
        if blocked:
            res = Result(step.id, "skipped", 0.0, f"needs {', '.join(blocked)} first")
            out(f"SKIPPED: {res.detail}")
        else:
            if pause:
                input_fn("[Enter to run] ")
            res = run_step(step, p, runner)
            out(trim(res.output, max_lines))
            out(
                f"-> {res.status.upper()} in {res.seconds:.1f} s"
                + (f": {res.detail}" if res.detail else "")
            )
        done[step.id] = res.status
        results.append(res)
    return results


def summary(results: list[Result]) -> str:
    rows = [f"  {r.status.upper():<10} {r.id:<12} {r.seconds:6.1f} s  {r.detail}" for r in results]
    bad = [r for r in results if r.status != "ok"]
    rows.append(
        f"{len(results) - len(bad)} of {len(results)} steps ok"
        + ("" if not bad else f"; not ok: {', '.join(r.id for r in bad)}")
    )
    return "\n".join(rows)
