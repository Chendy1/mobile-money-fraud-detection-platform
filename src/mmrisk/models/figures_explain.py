"""Phase 9 figures: what the model leans on, how, and three worked examples."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import polars as pl

from mmrisk.models.explain import LABELS
from mmrisk.models.figures import INK, MUTED, _save, plt

# One fixed colour per measure (Okabe-Ito): the same measure keeps its colour in every figure.
C_ALL, C_FRAUD, C_PERM = "#CC79A7", "#D55E00", "#0072B2"
C_UP, C_DOWN = "#D55E00", "#56B4E9"  # raises / lowers risk (two hues, not red and green)


def importance(glob_all: pl.DataFrame, glob_fraud: pl.DataFrame, perm: pl.DataFrame,
               path: Path, top: int = 8) -> Path:  # fmt: skip
    """Three measures of importance for the same features, side by side."""
    sel = list(dict.fromkeys(
        glob_all.head(top)["unit"].to_list()
        + glob_fraud.head(top)["unit"].to_list()
        + perm.sort("drop", descending=True).head(top)["unit"].to_list()
    ))  # fmt: skip
    a = dict(zip(glob_all["unit"], glob_all["mean_abs"], strict=True))
    f = dict(zip(glob_fraud["unit"], glob_fraud["mean_abs"], strict=True))
    p = {r["unit"]: (r["drop"], r["sd"]) for r in perm.to_dicts()}
    sel.sort(key=lambda u: -p[u][0])
    y = np.arange(len(sel))[::-1]
    fig, axes = plt.subplots(1, 3, figsize=(12.5, 0.38 * len(sel) + 1.9), sharey=True)
    panels = (
        ("Effect on all test transactions", [a[u] for u in sel], C_ALL, "Mean |effect| (log-odds)"),
        ("Effect on fraud-labelled rows", [f[u] for u in sel], C_FRAUD, "Mean |effect| (log-odds)"),
        ("Ranking lost when shuffled", [p[u][0] for u in sel], C_PERM, "PR-AUC lost"),
    )
    for ax, (title, vals, colour, xlabel) in zip(axes, panels, strict=True):
        ax.barh(y, vals, color=colour, height=0.62)
        if colour == C_PERM:
            ax.errorbar([p[u][0] for u in sel], y, xerr=[p[u][1] for u in sel], fmt="none",
                        ecolor=INK, lw=1, capsize=2)  # fmt: skip
        ax.set_axisbelow(True)
        ax.set_title(title, fontsize=11)
        ax.set_xlabel(xlabel)
        ax.grid(axis="y", visible=False)
    axes[0].set_yticks(y, [LABELS[u] for u in sel])
    fig.suptitle("What does the model lean on? Three answers, ordered by ranking lost",
                 x=0.01, ha="left", fontsize=12, fontweight="bold", color=INK)  # fmt: skip
    return _save(fig, path)


def _short(v: float) -> str:
    if abs(v) >= 1e6:
        return f"{v / 1e6:.3g}M"
    if abs(v) >= 1e4:
        return f"{v / 1e3:.3g}k"
    return f"{v:,.0f}" if float(v).is_integer() else f"{v:.3g}"


def _tick(row: dict) -> str:
    if np.isnan(row["lo"]):
        return row["bin"]
    if row["lo"] == row["hi"]:
        return _short(row["lo"])
    return f"{_short(row['lo'])}\nto {_short(row['hi'])}"


def dependence(dep: dict[str, pl.DataFrame], focus: list[str], path: Path) -> Path:
    """For the features that matter on fraud rows: the average effect at each value."""
    n = len(focus)
    cols = 3
    rows = int(np.ceil(n / cols))
    fig, axes = plt.subplots(rows, cols, figsize=(12.5, 3.0 * rows), sharey=True)
    axes = np.atleast_1d(axes).ravel()
    for ax, u in zip(axes, focus, strict=False):
        d = dep[u]
        x = np.arange(d.height)
        eff = d["mean_effect"].to_numpy()
        ax.axhline(0, color=MUTED, lw=1)
        ax.bar(x, eff, color=[C_UP if e > 0 else C_DOWN for e in eff], width=0.65)
        ax.set_xticks(x, [_tick(r) for r in d.to_dicts()], fontsize=7.5)
        ax.set_title(LABELS[u], fontsize=10.5)
        ax.grid(axis="x", visible=False)
    for ax in axes[n:]:
        ax.axis("off")
    for ax in axes[::cols][:rows]:
        ax.set_ylabel("Mean effect (log-odds)")
    fig.suptitle("How the effect changes with the value (orange raises risk, blue lowers it)",
                 x=0.01, ha="left", fontsize=12, fontweight="bold", color=INK)  # fmt: skip
    return _save(fig, path)


def examples(cases: list[dict], path: Path, top: int = 6) -> Path:
    """Reason codes for three transactions: one caught, one false alarm, one missed."""
    fig, axes = plt.subplots(1, len(cases), figsize=(5.2 * len(cases), 4.3), sharex=True)
    axes = np.atleast_1d(axes)
    for ax, c in zip(axes, cases, strict=True):
        eff = c["effects"]
        order = sorted(eff, key=lambda k: -abs(eff[k]))[:top]
        order = sorted(order, key=lambda k: eff[k])
        vals = [eff[k] for k in order]
        y = np.arange(len(order))
        ax.axvline(0, color=MUTED, lw=1)
        ax.barh(y, vals, color=[C_UP if v > 0 else C_DOWN for v in vals], height=0.62)
        ax.set_yticks(y, [c["texts"][k] for k in order], fontsize=8)
        ax.set_title(c["title"], fontsize=10)
        ax.set_xlabel("Effect on the log-odds of fraud")
        ax.grid(axis="y", visible=False)
    fig.suptitle("Reason codes for three transactions (the six largest effects each)",
                 x=0.01, ha="left", fontsize=12, fontweight="bold", color=INK)  # fmt: skip
    return _save(fig, path)
