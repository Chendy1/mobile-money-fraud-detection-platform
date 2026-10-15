"""Figures for the model report. Colour-blind-safe palette (Okabe-Ito), one chart per question."""

from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
from sklearn.metrics import precision_recall_curve  # noqa: E402

from mmrisk.models.metrics import BUDGETS_PER_10K  # noqa: E402

INK, MUTED, GRID = "#1f2328", "#6b7280", "#e5e7eb"
STYLE = {  # fixed colour and marker per model, in a fixed order
    "no_skill": ("#8A8F98", "--", None, "No skill (random)"),
    "rules": ("#E69F00", "-", "s", "Hand-written rules"),
    "logistic_regression": ("#0072B2", "-", "o", "Logistic regression"),
    "random_forest": ("#009E73", "-", "^", "Random forest"),
    "lightgbm": ("#CC79A7", "-", "D", "LightGBM"),  # used from Phase 8 on
}
plt.rcParams.update(
    {
        "font.size": 10,
        "axes.edgecolor": MUTED,
        "axes.labelcolor": INK,
        "axes.titlesize": 12,
        "axes.titleweight": "bold",
        "axes.titlelocation": "left",
        "xtick.color": MUTED,
        "ytick.color": MUTED,
        "axes.spines.top": False,
        "axes.spines.right": False,
        "axes.grid": True,
        "grid.color": GRID,
        "grid.linewidth": 0.8,
        "legend.frameon": False,
    }
)


def _save(fig, path: Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.tight_layout()
    fig.savefig(path, dpi=130)
    plt.close(fig)
    return path


def timeline(bounds: dict, first_ts, path: Path) -> Path:
    """Which dates feed training, the gap, validation and test?"""
    fig, ax = plt.subplots(figsize=(10, 2.6))
    parts = [
        ("Train", first_ts, bounds["train_end"], "#0072B2", None),
        ("Gap", bounds["train_end"], bounds["val_start"], "#d1d5db", "//"),
        ("Validation", bounds["val_start"], bounds["test_start"], "#56B4E9", None),
        ("Test", bounds["test_start"], bounds["matured_end"], "#009E73", None),
        ("Labels still\nincomplete", bounds["matured_end"], bounds["data_end"], "#f3f4f6", "xx"),
    ]
    for name, a, b, colour, hatch in parts:
        ax.barh(
            0, (b - a).days, left=a, height=0.5, color=colour, hatch=hatch,
            edgecolor="white", linewidth=1.5,
        )  # fmt: skip
        if (b - a).days > 12:
            ax.text(a + (b - a) / 2, 0, name, ha="center", va="center", color=INK, fontsize=9)
    ax.set_yticks([])
    ax.set_ylim(-0.6, 0.7)
    ax.grid(axis="y", visible=False)
    ax.spines["left"].set_visible(False)
    ax.set_title("What data does each part of the evaluation use?")
    ax.set_xlabel("Transaction date")
    return _save(fig, path)


def pr_curves(y, scores: dict, aps: dict, prevalence: float, path: Path) -> Path:
    """How precise is each model at each level of recall (test period)?"""
    fig, ax = plt.subplots(figsize=(7, 5.6))
    for name, (colour, ls, marker, label) in STYLE.items():
        if name != "no_skill" and name not in scores:
            continue  # a model that was not run (LightGBM before Phase 8)
        if name == "no_skill":
            ax.axhline(prevalence, color=colour, ls=ls, lw=1.5, label=f"{label} ({prevalence:.2%})")
            continue
        prec, rec, _ = precision_recall_curve(y, scores[name])
        if name == "rules":
            # A rule count takes only a few values, so the curve has a few real operating points;
            # joining them with straight lines would invent points that do not exist.
            ax.plot(
                rec, prec, marker, color=colour, ms=7, ls="none",
                label=f"{label} (PR-AUC {aps[name]:.2f}; markers = rule-count thresholds)",
            )  # fmt: skip
            continue
        ax.plot(rec, prec, color=colour, lw=2, label=f"{label} (PR-AUC {aps[name]:.2f})")
        if marker:
            idx = np.linspace(0, len(rec) - 1, 8).astype(int)
            ax.plot(rec[idx], prec[idx], marker, color=colour, ms=5, ls="none")
    ax.set_xlim(0, 1.0)
    ax.set_ylim(0, 1.02)
    ax.set_xlabel("Recall (share of fraud caught)")
    ax.set_ylabel("Precision (share of alerts that are fraud)")
    ax.set_title("How precise is each model at each level of recall?")
    ax.legend(loc="upper center", bbox_to_anchor=(0.5, -0.17), ncol=1, fontsize=9)
    return _save(fig, path)


def budgets(result: dict, models: list[str], path: Path) -> Path:
    """What does a fixed number of alerts buy? (test period, 95% customer-bootstrap interval)"""
    fig, (a, b) = plt.subplots(1, 2, figsize=(10.5, 4.2))
    x = np.array(BUDGETS_PER_10K)
    for k, name in enumerate(models):
        colour, ls, marker, label = STYLE[name]
        off = 1 + 0.03 * (k - len(models) / 2)
        for ax, stat in ((a, "precision"), (b, "recall")):
            pts = [result[name]["point"][f"{stat}@{v}"] for v in BUDGETS_PER_10K]
            lo = [result[name]["ci"][f"{stat}@{v}"][0] for v in BUDGETS_PER_10K]
            hi = [result[name]["ci"][f"{stat}@{v}"][1] for v in BUDGETS_PER_10K]
            err = [np.array(pts) - np.array(lo), np.array(hi) - np.array(pts)]
            ax.errorbar(
                x * off, pts, yerr=err, color=colour, ls=ls, marker=marker or "o", ms=5, lw=1.8,
                capsize=2, elinewidth=1, label=label,
            )  # fmt: skip
    for ax, ttl, yl in (
        (a, "Of the alerts raised, how many are fraud?", "Precision"),
        (b, "Of all fraud, how much is caught?", "Recall"),
    ):
        ax.set_xscale("log")
        ax.set_xticks(x)
        ax.set_xticklabels([str(v) for v in x])
        ax.minorticks_off()
        ax.set_ylim(0, 1.02)
        ax.set_xlabel("Alerts per 10,000 transactions (review budget)")
        ax.set_ylabel(yl)
        ax.set_title(ttl, fontsize=11)
    a.legend(loc="upper right", fontsize=9)
    return _save(fig, path)
