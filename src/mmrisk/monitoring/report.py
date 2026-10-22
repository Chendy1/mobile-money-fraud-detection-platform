"""Plain-text and Markdown output for the prediction-store checks."""

from __future__ import annotations

import polars as pl

from mmrisk.monitoring.checks import Check, overall

_MARK = {"PASS": "PASS", "WARN": "WARN", "FAIL": "FAIL", "SKIPPED": "skip"}


def text(checks: list[Check]) -> str:
    width = max(len(c.name) for c in checks)
    lines = [f"  {_MARK[c.status]:4}  {c.name:<{width}}  {c.detail}" for c in checks]
    return "\n".join([*lines, f"overall: {overall(checks)}"])


def markdown(checks: list[Check], table: pl.DataFrame, window: str, meta: dict) -> str:
    out = [
        "# Prediction monitoring report",
        "",
        "Synthetic data only. Thresholds are assumptions (`src/mmrisk/monitoring/config.py`).",
        "",
        f"- Store: `{meta['store']}`; {meta['rows']} predictions; window {window}",
        f"- Baseline: {meta['baseline']}",
        f"- Overall: **{overall(checks)}**",
        "",
        "| Check | Result | Detail |",
        "|---|---|---|",
        *(f"| {c.name} | {c.status} | {c.detail} |" for c in checks),
        "",
        f"## Windows of {window}",
        "",
        "| Start | Events | Allow | Review | Block | Flagged | Mean probability |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for start, rows, a, r, b, share, mean in table.iter_rows():
        s = "-" if share is None else f"{share:.2%}"
        m = "-" if mean is None else f"{mean:.4f}"
        out.append(f"| {start} | {rows} | {a} | {r} | {b} | {s} | {m} |")
    out += [
        "",
        "These checks look at what the system decided. They do not show whether the decisions were "
        "right (that needs labels, which arrive up to 45 days later) or whether the inputs "
        "changed (Phase 15).",
        "",
    ]
    return "\n".join(out)
