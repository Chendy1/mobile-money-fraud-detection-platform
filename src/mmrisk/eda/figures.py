"""Figures for the EDA report. Each one answers a single stated question."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import matplotlib

matplotlib.use("Agg")  # headless: no display needed
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import polars as pl  # noqa: E402

LEGIT, FRAUD, GREY = "#4C78A8", "#E45756", "#8A8F98"
WEEKDAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]  # DuckDB: 0 = Sunday

plt.rcParams.update(
    {
        "figure.dpi": 100,
        "savefig.dpi": 110,
        "font.size": 10,
        "axes.spines.top": False,
        "axes.spines.right": False,
        "axes.grid": True,
        "grid.alpha": 0.25,
        "axes.titlesize": 11,
        "axes.titleweight": "bold",
    }
)


def _save(fig, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.tight_layout()
    fig.savefig(path, metadata={"Software": None})
    plt.close(fig)
    return path


def class_balance(quality: dict, delays: pl.DataFrame, dq: dict, path: Path) -> Path:
    fig, (a, b) = plt.subplots(1, 2, figsize=(10, 3.8))
    legit, fraud = quality["n"] - quality["fraud"], quality["fraud"]
    bars = a.bar(["Not fraud", "Fraud-labelled"], [legit, fraud], color=[LEGIT, FRAUD])
    a.set_yscale("log")
    a.set_ylabel("Transactions (log scale)")
    a.set_title("How rare is fraud?")
    for bar, value in zip(bars, [legit, fraud], strict=True):
        a.text(bar.get_x() + bar.get_width() / 2, value * 1.15, f"{value:,}", ha="center")
    a.set_ylim(top=legit * 60)
    a.text(
        0.5,
        0.88,
        f"{quality['rate']:.2%} fraud (1 in {quality['imbalance_ratio'] + 1:,.0f})",
        transform=a.transAxes,
        ha="center",
    )
    b.bar(delays["days"], delays["n"], width=0.9, color=FRAUD)
    top = delays["n"].max()
    b.axvline(dq["median_days"], color="black", ls="--", lw=1)
    b.text(dq["median_days"] + 0.6, top * 0.92, f"median {dq['median_days']:.1f} d")
    b.axvline(dq["p90_days"], color="black", ls=":", lw=1)
    b.text(dq["p90_days"] + 0.6, top * 0.76, f"90% by {dq['p90_days']:.0f} d")
    b.set_xlabel("Days from transaction to fraud label")
    b.set_ylabel("Fraud labels")
    b.set_title("How long do fraud labels take to arrive?")
    return _save(fig, path)


def maturity(curve: pl.DataFrame, matured_cutoff, path: Path) -> Path:
    fig, ax = plt.subplots(figsize=(10, 3.8))
    weeks = curve["week"]
    ax.plot(weeks, curve["eventual_rate"] * 100, color=GREY, lw=2)
    ax.plot(weeks, curve["known_rate"] * 100, color=FRAUD, lw=2)
    ax.axvline(matured_cutoff, color="black", ls="--", lw=1)
    ax.axvspan(matured_cutoff, weeks.max(), color=FRAUD, alpha=0.08)
    ax.set_xlim(right=weeks.max() + timedelta(days=60))
    last = curve.tail(1).row(0, named=True)
    x = weeks.max() + timedelta(days=4)
    ax.text(x, last["eventual_rate"] * 100, "eventual\n(all labels)", color=GREY, va="center")
    ax.text(x, last["known_rate"] * 100, "known at\nthe cutoff", color=FRAUD, va="center")
    ax.text(matured_cutoff, ax.get_ylim()[1] * 0.97, " labels still arriving", va="top")
    ax.set_ylabel("Fraud-labelled share of transactions (%)")
    ax.set_xlabel("Week of transaction")
    ax.set_title("Does recent data look cleaner than it is?")
    return _save(fig, path)


def rhythm(by_dom: pl.DataFrame, by_dow: pl.DataFrame, path: Path) -> Path:
    fig, (a, b) = plt.subplots(1, 2, figsize=(10, 3.6), gridspec_kw={"width_ratios": [2.2, 1]})
    colours = [FRAUD if (d >= 25 or d <= 3) else LEGIT for d in by_dom["dom"]]
    a.bar(by_dom["dom"], by_dom["mean_daily"], color=colours)
    a.set_xlabel("Day of month (red = salary window: 25th to 3rd)")
    a.set_ylabel("Mean transactions per day")
    a.set_title("Is there a monthly rhythm?")
    b.bar([WEEKDAYS[d] for d in by_dow["dow"]], by_dow["mean_daily"], color=LEGIT)
    b.set_xlabel("Day of week")
    b.set_title("And a weekly one?")
    return _save(fig, path)


def hourly(shares: pl.DataFrame, night: dict, path: Path) -> Path:
    fig, ax = plt.subplots(figsize=(10, 3.8))
    for label, colour, name in ((0, LEGIT, "Not fraud"), (1, FRAUD, "Fraud-labelled")):
        d = shares.filter(pl.col("label") == label)
        ax.plot(d["hr"], d["share"] * 100, color=colour, lw=2, marker="o", ms=4, label=name)
    ax.axvspan(-0.5, 4.5, color=GREY, alpha=0.15)
    ax.text(2, ax.get_ylim()[1] * 0.95, "night", ha="center", va="top")
    ax.set_xticks(range(0, 24, 2))
    ax.set_xlabel("Hour of day (East Africa Time)")
    ax.set_ylabel("Share of the class's transactions (%)")
    ax.set_title(
        f"Does fraud happen at different hours? Night: {night['fraud_night']:.1%} of fraud "
        f"vs {night['legit_night']:.1%} of other transactions"
    )
    ax.legend(loc="upper right")
    return _save(fig, path)


def amounts(abs_hist: pl.DataFrame, rel_hist: pl.DataFrame, summary: dict, path: Path) -> Path:
    fig, (a, b) = plt.subplots(1, 2, figsize=(10, 3.8))
    panels = (
        (a, abs_hist, "Amount (KES, log scale)", "Do fraud amounts look different?"),
        (
            b,
            rel_hist,
            "Amount / customer's running average (log)",
            "...and relative to the customer's own history?",
        ),
    )
    for ax, hist, xlabel, title in panels:
        for label, colour, name in ((0, LEGIT, "Not fraud"), (1, FRAUD, "Fraud-labelled")):
            d = hist.filter(pl.col("label") == label)
            x = 10 ** d["bucket"].to_numpy()
            ax.step(x, d["share"] * 100, where="post", color=colour, lw=2, label=name)
        ax.set_xscale("log")
        ax.set_xlabel(xlabel)
        ax.set_ylabel("Share of the class (%)")
        ax.set_title(title)
    a.axvline(summary["legit_median"], color=LEGIT, ls=":", lw=1)
    a.axvline(summary["fraud_median"], color=FRAUD, ls=":", lw=1)
    b.axvline(1, color="black", ls=":", lw=1)
    a.legend()
    return _save(fig, path)


def signals(table: pl.DataFrame, path: Path) -> Path:
    t = table.sort("lift")
    fig, ax = plt.subplots(figsize=(10, 4.8))
    colours = [FRAUD if s == "any_signal" else LEGIT for s in t["signal"]]
    names = [s.removeprefix("f_").replace("_", " ") for s in t["signal"]]
    lifts = t["lift"].clip(lower_bound=0.05)
    ax.barh(names, lifts, color=colours)
    ax.set_xscale("log")
    ax.axvline(1, color="black", lw=1)
    ax.set_xlabel("Lift: fraud rate when the signal is present / overall fraud rate (log scale)")
    ax.set_title("Which behavioural signals separate fraud, and how completely?")
    for i, row in enumerate(t.iter_rows(named=True)):
        ax.text(
            max(row["lift"], 0.05) * 1.15,
            i,
            f"{row['lift']:.1f}x   precision {row['precision']:.1%}   "
            f"recall {row['recall']:.0%}   prevalence {row['prevalence']:.2%}",
            va="center",
            fontsize=8,
        )
    ax.set_xlim(0.03, lifts.max() * 80)
    return _save(fig, path)


def correlation(matrix: pl.DataFrame, path: Path) -> Path:
    cols = [c for c in matrix.columns if c != "feature"]
    values = np.array([[np.nan if v is None else v for v in matrix[c].to_list()] for c in cols]).T
    fig, ax = plt.subplots(figsize=(8.4, 6.6))
    im = ax.imshow(values, cmap="RdBu_r", vmin=-1, vmax=1)
    ax.set_xticks(range(len(cols)), cols, rotation=45, ha="right")
    ax.set_yticks(range(len(cols)), matrix["feature"].to_list())
    ax.grid(False)
    for i in range(len(cols)):
        for j in range(len(cols)):
            v = values[i, j]
            if not np.isnan(v):
                colour = "white" if abs(v) > 0.6 else "black"
                ax.text(j, i, f"{v:.2f}", ha="center", va="center", fontsize=7, color=colour)
    fig.colorbar(im, ax=ax, shrink=0.8, label="Spearman correlation")
    ax.set_title("Which candidate features overlap, and which relate to the fraud label?")
    return _save(fig, path)
