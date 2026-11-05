"""The report: what was run, on what, and what came out. JSON for tools, markdown for people."""

from __future__ import annotations

import json
import os
import platform
import sys
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from mmrisk.loadtest.plan import Segment
from mmrisk.loadtest.stats import Sample, Slo, knee, summarize_segment

CAVEAT = (
    "These numbers describe this machine, this run, and a load generator sharing its CPU with the "
    "server. They are not a capacity statement for any other machine, and the latency targets are "
    "ASSUMED, not requirements."
)


def machine() -> dict:
    versions = {}
    for name in ("polars", "scikit-learn", "lightgbm", "fastapi", "uvicorn", "numpy"):
        try:
            from importlib.metadata import version

            versions[name] = version(name)
        except Exception:  # a package that is not installed
            versions[name] = None
    return {
        "platform": platform.platform(), "python": sys.version.split()[0],
        "cpu_count": os.cpu_count(), "versions": versions,
    }  # fmt: skip


def build(
    *,
    name: str,
    mode: str,
    settings: dict,
    segments: list[Segment],
    samples: list[Sample],
    planned: list[int],
    not_sent: int,
    slo: Slo,
    server: dict | None = None,
) -> dict:
    rows = []
    for i, seg in enumerate(segments):
        part = [s for s in samples if s.segment == i]
        rows.append(
            {"start": seg.start, "end": seg.end,
             **summarize_segment(part, seg.seconds, planned[i], seg.rate, slo, seg.start)}
        )  # fmt: skip
    total_ok = sum(r["ok"] for r in rows)
    warnings = []
    if not_sent:
        warnings.append(
            f"{not_sent} requests were never sent: the server stopped answering "
            "(20 connection failures in a row in one worker)."
        )
    if any(r["duplicates"] for r in rows):
        warnings.append(
            "some answers were `duplicate`: the server had already seen those transactions "
            "(restart it before another run), so those latencies are not scoring times."
        )
    if any(r["errors"].get("conflict") for r in rows):
        warnings.append(
            "409 conflicts occurred: transactions reached the server out of order or repeated. "
            "The run did not exercise normal scoring; check the stream start and the server state."
        )
    if any(r["saturated"] for r in rows):
        warnings.append(
            "in some segments requests started late (the workers could not keep up): latency "
            "from the intended start includes that queueing. More --workers may help, up to the "
            "load generator's own limit."
        )
    return {
        "name": name, "mode": mode, "created_utc": datetime.now(UTC).isoformat(timespec="seconds"),
        "settings": settings, "machine": machine(), "caveat": CAVEAT,
        "slo_ASSUMED": asdict(slo), "segments": rows, "knee": knee(rows),
        "totals": {
            "planned": sum(planned), "sent": len(samples), "ok": total_ok, "not_sent": not_sent,
        },
        "server": server, "warnings": warnings,
    }  # fmt: skip


def _f(x, digits=1):
    return "–" if x is None else f"{x:.{digits}f}"


def markdown(rep: dict) -> str:
    s = rep["settings"]
    lines = [
        f"# Load test: {rep['name']}", "",
        f"Mode `{rep['mode']}`, {rep['created_utc']}. {rep['caveat']}", "",
        "## Setup", "",
        *(f"- {k}: {v}" for k, v in s.items()),
        f"- machine: {rep['machine']['platform']}, Python {rep['machine']['python']}, "
        f"{rep['machine']['cpu_count']} CPUs", "",
        "## Results", "",
        "Latency is measured from the time each request was meant to start. "
        "Achieved = answers divided by the time until the last one arrived (a backlog lowers it). "
        "Meets = p95, p99, error rate and throughput all within the (ASSUMED) targets: "
        f"p95 ≤ {rep['slo_ASSUMED']['p95_ms']:g} ms, p99 ≤ {rep['slo_ASSUMED']['p99_ms']:g} ms, "
        f"errors ≤ {rep['slo_ASSUMED']['max_error_rate']:.0%}, "
        f"achieved ≥ {rep['slo_ASSUMED']['min_achieved']:.0%} of offered.", "",
        "| offered/s | achieved/s | sent | errors | p50 ms | p95 ms | p99 ms | max ms | service p95 ms | late-start p95 ms | meets |",  # noqa: E501
        "|---:|---:|---:|---|---:|---:|---:|---:|---:|---:|:--:|",
    ]  # fmt: skip
    for r in rep["segments"]:
        lat, svc = r["latency_ms_from_intended_start"], r["latency_ms_service"]
        errs = ", ".join(f"{k} {v}" for k, v in r["errors"].items()) or "0"
        lines.append(
            f"| {r['offered_rate']:.1f} | {r['achieved_rate']:.1f} | {r['sent']} | {errs} | "
            f"{_f(lat['p50'])} | {_f(lat['p95'])} | {_f(lat['p99'])} | {_f(lat['max'])} | "
            f"{_f(svc['p95'])} | {_f(r['start_lag_ms_p95'])} | "
            f"{'yes' if r['slo']['met'] else 'NO'} |"
        )
    k = rep["knee"]
    lines += ["", "## Reading", ""]
    if k is None:
        lines.append("The first segment already missed the targets: no rate was sustained.")
    elif k["limited"]:
        lines.append(
            f"Targets met up to {k['offered_rate']:g} requests/s; the next segment missed them."
        )
    else:
        lines.append(
            f"Every segment met the targets (highest offered {k['offered_rate']:g}/s): "
            "the limit was not reached, so this says nothing about where it is."
        )
    if rep.get("server"):
        lines += ["", "## Server's own count", "", f"`{json.dumps(rep['server'])}`"]
    if rep["warnings"]:
        lines += ["", "## Warnings", "", *(f"- {w}" for w in rep["warnings"])]
    return "\n".join(lines) + "\n"


def write(rep: dict, out_dir: Path) -> tuple[Path, Path]:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    j, m = out_dir / f"{rep['name']}.json", out_dir / f"{rep['name']}.md"
    j.write_text(json.dumps(rep, indent=2), encoding="utf-8")
    m.write_text(markdown(rep), encoding="utf-8")
    return j, m
