"""Phase 8 figures: calibration, the decision map, and what acting on the scores costs."""

from __future__ import annotations

from pathlib import Path

import numpy as np
from matplotlib.colors import ListedColormap

from mmrisk.models.costs import CostModel
from mmrisk.models.figures import INK, MUTED, STYLE, _save, plt

# Severity is an ordered quantity, so the three actions use one hue from light to dark.
ACTION_COLOURS = ("#F7F3EA", "#F0B35B", "#B45F06")


def calibration(raw_rows: list[dict], cal_rows: list[dict], path: Path) -> Path:
    """When the model says p, how often is the transaction actually fraud? (test period)"""
    colour = STYLE["lightgbm"][0]
    fig, ax = plt.subplots(figsize=(6.2, 5.6))
    lo, hi = 1e-4, 1.0
    ax.plot([lo, hi], [lo, hi], color=MUTED, ls="--", lw=1.2)
    ax.text(0.00022, 0.00013, "perfectly calibrated", color=MUTED, rotation=40, fontsize=9)
    for rows, filled, label in (
        (raw_rows, False, "Raw LightGBM score"),
        (cal_rows, True, "After calibration"),
    ):
        pts = [r for r in rows if r["fraud"] > 0]
        x = np.array([r["predicted"] for r in pts])
        y = np.array([r["observed"] for r in pts])
        err = np.array(
            [
                [r["observed"] - r["ci_low"] for r in pts],
                [r["ci_high"] - r["observed"] for r in pts],
            ]
        )
        ax.errorbar(
            x, y, yerr=err, color=colour, marker="D", ms=7, lw=1.6 if filled else 1.0, capsize=2,
            ls="-" if filled else ":", mfc=colour if filled else "white", mec=colour, label=label,
        )  # fmt: skip
    ax.set_xscale("log")
    ax.set_yscale("log")
    ax.set_xlim(lo, hi)
    ax.set_ylim(lo, hi)
    ax.set_xlabel("Probability the model gives (average in the group)")
    ax.set_ylabel("Share that really were fraud (95% interval)")
    ax.set_title("When the model says p, is it right?")
    ax.legend(loc="upper left", fontsize=9)
    return _save(fig, path)


def decision_map(cm: CostModel, path: Path) -> Path:
    """Which action is cheapest for each fraud probability and amount?"""
    p = np.logspace(-3, 0, 400)
    a = np.logspace(np.log10(50), np.log10(150_000), 400)
    pp, aa = np.meshgrid(p, a)
    action = cm.decide(pp.ravel(), aa.ravel()).reshape(pp.shape)
    fig, ax = plt.subplots(figsize=(7.2, 5.2))
    ax.pcolormesh(
        pp, aa, action, cmap=ListedColormap(ACTION_COLOURS), vmin=0, vmax=2, shading="auto"
    )
    ax.set_xscale("log")
    ax.set_yscale("log")
    ax.grid(False)
    for name, x, y, colour in (
        ("ALLOW", 0.0035, 700, INK),
        ("REVIEW", 0.06, 4500, INK),
        ("BLOCK", 0.18, 60_000, "white"),
    ):
        ax.text(x, y, name, color=colour, fontsize=12, fontweight="bold")
    ax.set_xlabel("Calibrated fraud probability")
    ax.set_ylabel("Transaction amount (KES)")
    ax.set_title("Which action is cheapest?")
    return _save(fig, path)


def cost_by_scorer(rows: list[dict], sensitivity: dict, path: Path) -> Path:
    """Left: cost per 10,000 transactions by policy. Right: saving as the friction costs change."""
    fig, (a, b) = plt.subplots(1, 2, figsize=(12.5, 4.8), gridspec_kw={"width_ratios": [1.2, 1]})
    rows = list(reversed(rows))  # the first row ends up at the top
    ys = np.arange(len(rows))
    right = max((r["ci"][1] if r.get("ci") else r["cost"]) for r in rows)
    for y, r in zip(ys, rows, strict=True):
        a.barh(y, r["cost"], color=r["colour"], height=0.62)
        end = r["cost"]
        if r.get("ci"):
            xerr = [[r["cost"] - r["ci"][0]], [r["ci"][1] - r["cost"]]]
            a.errorbar(r["cost"], y, xerr=xerr, color=INK, capsize=3, lw=1)
            end = r["ci"][1]
        a.text(end + right * 0.015, y, f"{r['cost']:,.0f}", va="center", color=INK, fontsize=9)
    a.set_yticks(ys)
    a.set_yticklabels([r["label"] for r in rows])
    a.set_xlim(0, right * 1.14)
    a.set_xlabel("KES per 10,000 transactions (assumed costs; 95% interval)")
    a.set_title("What does each policy cost?", fontsize=11)
    factors = sensitivity["factors"]
    for name, vals in sensitivity["savings"].items():
        colour, ls, marker, label = STYLE[name]
        b.plot(factors, [v * 100 for v in vals], color=colour, ls=ls, marker=marker or "o",
               ms=5, lw=1.8, label=label)  # fmt: skip
    b.axvline(1.0, color=MUTED, ls=":", lw=1)
    b.set_xscale("log")
    b.set_xticks(factors)
    b.set_xticklabels([f"{f:g}x" for f in factors])
    b.minorticks_off()
    b.set_xlabel("Review and block costs (1x = assumed)")
    b.set_ylabel("Cost saved vs allowing everything (%)")
    b.set_title("Does the ranking depend on the costs?", fontsize=11)
    b.legend(fontsize=9, loc="lower left")
    return _save(fig, path)
