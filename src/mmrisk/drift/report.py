"""Text and Markdown output for a drift comparison."""

from __future__ import annotations

from mmrisk.drift import config as cfg
from mmrisk.drift.detect import Drift, Result, ranked

_MARK = {"PASS": "stable", "WARN": "moderate", "FAIL": "MAJOR", "SKIPPED": "skipped"}


def _psi(r: Result) -> str:
    return "-" if r.psi is None else f"{r.psi:.3f}"


def verdict(d: Drift) -> str:
    if not d.scored:
        return "nothing could be scored (too few rows)"
    if d.drift_detected:
        names = ", ".join(r.name for r in d.counted if r.status == "FAIL")
        return f"DRIFT DETECTED: major shift in {names}"
    moderate = [r.name for r in d.counted if r.status == "WARN"]
    if moderate:
        return f"no major drift; moderate shift in {', '.join(moderate)}"
    return "no drift detected"


def text(d: Drift, top: int = 12) -> str:
    rows = ranked(d)[:top]
    lines = [f"  {_MARK[r.status]:9} {r.name:26} PSI {_psi(r):>6}  {r.detail}" for r in rows]
    return "\n".join([*lines, f"{d.rows} rows over {d.days:.1f} days; {verdict(d)}"])


def markdown(d: Drift, meta: dict, series: list[dict]) -> str:
    out = [
        "# Drift report",
        "",
        "Synthetic data only. Thresholds are assumptions (`src/mmrisk/drift/config.py`).",
        "",
        f"- Reference: {meta['reference_window']} ({meta['reference_rows']} transactions)",
        f"- Current: {meta['current_window']} ({d.rows} transactions, {d.days:.1f} days)",
        f"- PSI limits: moderate {cfg.PSI_WARN}, major {cfg.PSI_FAIL} (conventions, assumed)",
        f"- Result: **{verdict(d)}**",
    ]
    if meta.get("injected"):
        out.append(f"- **Injected shift (demonstration only):** {meta['injected']}")
    out += [
        "",
        "| Feature | PSI | Noise level | Status | Policy | Note |",
        "|---|---:|---:|---|---|---|",
    ]
    for r in ranked(d):
        noise = "-" if r.noise is None else f"{r.noise:.4f}"
        out.append(
            f"| {r.name} | {_psi(r)} | {noise} | {_MARK[r.status]} | {r.policy} | {r.detail} |"
        )
    if series:
        out += [
            "",
            f"## Over time (windows of {meta['series_window']})",
            "",
            "| Window start | Rows | Largest PSI | Feature | Score PSI |",
            "|---|---:|---:|---|---:|",
        ]
        for s in series:
            if s["psi"] is None:
                out.append(f"| {s['start']} | {s['rows']} | - | too few rows | - |")
            else:
                score = "-" if s["score_psi"] is None else f"{s['score_psi']:.3f}"
                out.append(
                    f"| {s['start']} | {s['rows']} | {s['psi']:.3f} | {s['feature']} | {score} |"
                )
    out += [
        "",
        "How to read it. PSI compares the share of rows in each reference bin with the share in "
        "the current window. The noise level is what two samples of the same distribution are "
        "expected to show, so a PSI near it means no change. `calendar` and `accumulating` "
        "features are explained in `config.py`: the first needs a window of a month or more, the "
        "second moves as time passes and does not by itself declare drift. Drift in the inputs is "
        "not proof that the model is wrong, and no drift is not proof that it is right: that "
        "needs labels.",
        "",
    ]
    return "\n".join(out)
