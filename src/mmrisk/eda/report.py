"""Builds the EDA report (Markdown + figures) from the lake. Every number is computed."""

from __future__ import annotations

import math
import os
from dataclasses import dataclass
from pathlib import Path

import polars as pl

from mmrisk.eda import analyses as A
from mmrisk.eda import figures as F
from mmrisk.eda.data import Analysis
from mmrisk.generation.config import DISPUTE_WINDOW_DAYS

FIGURES = {
    "balance": "01-class-balance.png",
    "maturity": "02-label-maturity.png",
    "rhythm": "03-time-rhythm.png",
    "hourly": "04-hourly.png",
    "amounts": "05-amounts.png",
    "signals": "06-signals.png",
    "correlation": "07-correlation.png",
}


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """95% Wilson score interval for a proportion k / n."""
    if n <= 0:
        return 0.0, 1.0
    p = k / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return max(0.0, centre - half), min(1.0, centre + half)


def pct(x: float | None, digits: int = 1) -> str:
    return "n/a" if x is None else f"{x * 100:.{digits}f}%"


def times(x: float | None, digits: int = 1) -> str:
    return "n/a" if x is None else f"{x:.{digits}f}x"


def md_table(df: pl.DataFrame, columns: list[tuple[str, str, str]]) -> str:
    """Render selected columns as a Markdown table. columns: (column, header, format)."""
    head = "| " + " | ".join(h for _, h, _ in columns) + " |"
    rule = "|" + "|".join("---" for _ in columns) + "|"
    rows = []
    for rec in df.iter_rows(named=True):
        cells = []
        for col, _, fmt in columns:
            v = rec[col]
            if v is None:
                cells.append("n/a")
            elif fmt == "int":
                cells.append(f"{int(v):,}")
            elif fmt == "pct":
                cells.append(pct(v, 2))
            elif fmt == "pct1":
                cells.append(pct(v, 1))
            elif fmt == "x":
                cells.append(times(v))
            elif fmt == "num1":
                cells.append(f"{v:,.1f}")
            elif fmt == "num":
                cells.append(f"{v:,.0f}")
            else:
                cells.append(str(v))
        rows.append("| " + " | ".join(cells) + " |")
    return "\n".join([head, rule, *rows])


def _ratio(a: float | None, b: float | None) -> float | None:
    return a / b if a is not None and b else None


@dataclass
class ReportResult:
    report_path: Path
    figure_paths: list[Path]
    text: str


def _dimension_rows(dim: pl.DataFrame, base_rate: float) -> pl.DataFrame:
    rows = []
    for r in dim.iter_rows(named=True):
        lo, hi = wilson(r["fraud"], r["txns"])
        verdict = "higher" if lo > base_rate else "lower" if hi < base_rate else "not distinct"
        rows.append({**r, "ci": f"{pct(lo, 2)} to {pct(hi, 2)}", "verdict": verdict})
    return pl.DataFrame(rows)


def build_report(
    an: Analysis, report_path: Path, figures_dir: Path, use_truth: bool = True
) -> ReportResult:
    report_path, figures_dir = Path(report_path), Path(figures_dir)
    fig = {k: figures_dir / v for k, v in FIGURES.items()}
    rel = Path(os.path.relpath(figures_dir, report_path.parent)).as_posix()

    ov = A.overview(an)
    quality = A.label_quality(an)
    delays, dq = A.label_delays(an), A.delay_quantiles(an)
    curve, gap = A.maturity_curve(an), A.recent_label_gap(an)
    dom, dow = A.volume_by_day_of_month(an), A.volume_by_weekday(an)
    salary = A.salary_window_effect(an)
    hourly, night = A.hourly_shares(an), A.night_share(an)
    summary = A.amount_summary(an)
    by_type = A.amount_by_type(an)
    dims = _dimension_rows(A.dimension_table(an), quality["rate"])
    cust = A.customer_stats(an)
    cps = A.counterparty_stats(an)
    sig = A.signal_table(an)
    corr = A.spearman_matrix(A.corr_inputs(an), list(A.CORR_COLUMNS))
    audit = A.truth_audit(an) if use_truth else None

    F.class_balance(quality, delays, dq, fig["balance"])
    F.maturity(curve, an.matured_cutoff, fig["maturity"])
    F.rhythm(dom, dow, fig["rhythm"])
    F.hourly(hourly, night, fig["hourly"])
    F.amounts(
        A.amount_hist(an, "amount", 0.1),
        A.amount_hist(an, "rel_amount", 0.1),
        summary,
        fig["amounts"],
    )
    F.signals(sig, fig["signals"])
    F.correlation(corr, fig["correlation"])

    any_row = sig.filter(pl.col("signal") == "any_signal").row(0, named=True)
    singles = sig.filter(pl.col("signal") != "any_signal").sort("lift", descending=True)
    top3 = ", ".join(
        f"{r['signal'].removeprefix('f_').replace('_', ' ')} ({times(r['lift'], 0)})"
        for r in singles.head(3).iter_rows(named=True)
    )
    victims = cust["victims"]
    hidden = 1 - gap["known"] / gap["eventual"] if gap["eventual"] else None
    night_ratio = _ratio(night["fraud_night"], night["legit_night"])
    rel_ratio = _ratio(summary["fraud_rel_median"], summary["legit_rel_median"])
    salary_ratio = _ratio(salary["salary_mean"], salary["other_mean"])
    band = an.query(
        "SELECT final_label AS y, avg((amount BETWEEN 9500 AND 9990)::INT) AS s "
        "FROM a WHERE matured GROUP BY 1"
    )
    band_f = band.filter(pl.col("y") == 1)["s"][0]
    band_l = band.filter(pl.col("y") == 0)["s"][0]
    lab_col = corr.filter(pl.col("feature") != "fraud_label").select(["feature", "fraud_label"])
    lab_col = lab_col.with_columns(pl.col("fraud_label").abs().alias("a")).sort(
        "a", descending=True
    )
    top_f, top_c = lab_col["feature"][0].replace("_", " "), lab_col["fraud_label"][0]
    one_in = quality["imbalance_ratio"] + 1
    fraud_rel = times(summary["fraud_rel_median"], 2)
    weeks = max(1, (ov["last_ts"] - ov["first_ts"]).days / 7)
    per_week = ov["matured_n"] * quality["rate"] / weeks
    top1_lift = times(sig.filter(pl.col("signal") == "f_top1pct_amount")["lift"][0], 2)
    lineage = "; ".join(f"{t}: {', '.join(ids)}" for t, ids in sorted(an.lineage.items()))

    L: list[str] = []
    add = L.append
    add("# Exploratory data analysis")
    add("")
    add(
        "> **All data is synthetic.** Findings describe how this simulated dataset behaves, "
        "not real mobile-money traffic. This report is generated by "
        "`python -m mmrisk.eda`; every number below is computed from the data lake, so "
        "re-running it on regenerated data refreshes the report."
    )
    add("")
    add(
        f"Analysis time (end of data): **{ov['cutoff']}**. Transactions older than "
        f"{DISPUTE_WINDOW_DAYS} days at that time (up to **{ov['matured_cutoff']}**) are "
        "called *matured*: their labels are final. Label-based statistics use matured "
        "transactions only, because newer ones are still missing fraud labels (see "
        "question 2). Source batches: " + (lineage or "n/a") + "."
    )
    add("")
    add("## Key findings")
    add("")
    add(
        f"- **Scale.** {ov['n']:,} transactions from {ov['customers']:,} customers between "
        f"{ov['first_ts']:%Y-%m-%d} and {ov['last_ts']:%Y-%m-%d}; {ov['matured_n']:,} "
        f"({pct(ov['matured_n'] / ov['n'], 0)}) are matured."
    )
    add(
        f"- **Rare class.** {quality['fraud']:,} of {quality['n']:,} matured transactions are "
        f"labelled fraud ({pct(quality['rate'], 2)}, about 1 in {one_in:,.0f})."
    )
    add(
        f"- **Slow labels.** Median {dq['median_days']:.1f} days from transaction to fraud "
        f"label, 90% within {dq['p90_days']:.0f} days, up to {dq['max_days']:.0f}. In the last "
        f"14 days of data only {gap['known']} of {gap['eventual']} eventual fraud labels are "
        f"known ({pct(hidden, 0)} still hidden)."
    )
    add(
        f"- **Night.** {pct(night['fraud_night'])} of fraud-labelled transactions happen "
        f"between 00:00 and 04:59, against {pct(night['legit_night'])} of the rest "
        f"({times(night_ratio)} as often)."
    )
    add(
        f"- **Amounts.** In absolute terms fraud and non-fraud look alike (median "
        f"{summary['fraud_median']:,.0f} vs {summary['legit_median']:,.0f} KES). Relative to the "
        f"customer's own running average they differ: median {fraud_rel} "
        f"vs {times(summary['legit_rel_median'], 2)} ({times(rel_ratio)} higher)."
    )
    add(
        f"- **Signals.** Strongest single signals by lift: {top3}. All simple signals combined "
        f"flag {pct(any_row['prevalence'])} of transactions and catch {pct(any_row['recall'], 0)} "
        f"of fraud at {pct(any_row['precision'])} precision."
    )
    add(
        f"- **Clustering.** {victims['victims']:,} of {victims['customers']:,} customers have "
        f"at least one fraud-labelled transaction, and {pct(victims['repeat_share'], 0)} of fraud "
        "rows belong to customers with more than one."
    )
    add("")

    add("## 1. How rare is fraud?")
    add("")
    add(f"![class balance]({rel}/{FIGURES['balance']})")
    add("")
    add(
        f"Of {quality['n']:,} matured transactions, {quality['fraud']:,} carry a fraud label. "
        f'A model that always answers "not fraud" would be {pct(1 - quality["rate"], 2)} '
        "accurate and useless, so accuracy is not a usable metric here. A random scorer has a "
        f"precision-recall AUC equal to the prevalence ({pct(quality['rate'], 2)}); that is the "
        "floor any model must beat, which is why PR-AUC is the primary metric (ADR-008 of the "
        "plan, to be written in Phase 7)."
    )
    add("")

    add("## 2. Does recent data look cleaner than it is?")
    add("")
    add(f"![label maturity]({rel}/{FIGURES['maturity']})")
    add("")
    add(
        "Fraud labels arrive after a delay (right panel of figure 1), and non-fraud labels are "
        f"only confirmed after the {DISPUTE_WINDOW_DAYS}-day dispute window. The grey line is the "
        "fraud rate once every label has arrived; the red line is what was actually known at "
        f"the cutoff. They split in the final weeks: in the last 14 days {gap['known']} of "
        f"{gap['eventual']} eventual fraud labels were known. The grey line exists only because "
        "this simulation holds future labels; a real system would never see it."
    )
    add("")
    add(
        f"**Consequence for modelling:** training data must use only labels whose "
        f"`label_available_at` is on or before the training cutoff, and the last "
        f"~{DISPUTE_WINDOW_DAYS} days before any cutoff cannot be treated as clean negatives. "
        "Week-to-week rates are also noisy (roughly "
        f"{per_week:.0f}"
        " fraud rows per week), so small weekly movements mean little."
    )
    add("")

    add("## 3. When do transactions and fraud happen?")
    add("")
    add(f"![time rhythm]({rel}/{FIGURES['rhythm']})")
    add("")
    add(
        f"Volume rises in the salary window (25th to 3rd): {salary['salary_mean']:.0f} "
        f"transactions per day against {salary['other_mean']:.0f} on other days "
        f"({times(salary_ratio, 2)}). The weekday panel shows the weekly pattern."
    )
    add("")
    add(f"![hourly]({rel}/{FIGURES['hourly']})")
    add("")
    add(
        f"Most activity is in the daytime for both classes, but fraud has a visible night "
        f"component: {pct(night['fraud_night'])} of fraud against {pct(night['legit_night'])} "
        f"of other transactions happens before 05:00. Night is a useful but incomplete signal: "
        f"{pct(1 - night['fraud_night'], 0)} of fraud happens in daytime."
    )
    add("")

    add("## 4. Do fraud amounts look different?")
    add("")
    add(f"![amounts]({rel}/{FIGURES['amounts']})")
    add("")
    add(
        f"Absolute amounts overlap almost completely (medians "
        f"{summary['fraud_median']:,.0f} and {summary['legit_median']:,.0f} KES)."
        f" Amounts just under 10,000 KES (9,500 to 9,990, the structuring range) are "
        f"{pct(band_f)} of fraud-labelled transactions against {pct(band_l)} of the rest. "
        "Compared with the customer's own running average "
        f"(customers with at least 3 prior transactions) the fraud distribution shifts right: "
        f"median {times(summary['fraud_rel_median'], 2)} against "
        f"{times(summary['legit_rel_median'], 2)}. A feature that scales the amount by the "
        "customer's history will carry more information than the raw amount."
    )
    add("")
    add(
        md_table(
            by_type,
            [
                ("transaction_type", "Type", "s"),
                ("txns", "Transactions", "int"),
                ("median_kes", "Median KES", "num"),
                ("p99_kes", "99th pct KES", "num"),
                ("fraud_rate", "Fraud rate", "pct"),
            ],
        )
    )
    add("")

    add("## 5. Who and where?")
    add("")
    add(
        'Fraud rate by group with a 95% interval. "higher" or "lower" is shown only when '
        f"the interval excludes the overall rate ({pct(quality['rate'], 2)}); small groups "
        'usually come out "not distinct", which is the honest reading.'
    )
    add("")
    add(
        md_table(
            dims,
            [
                ("dimension", "Dimension", "s"),
                ("value", "Value", "s"),
                ("txns", "Transactions", "int"),
                ("fraud", "Fraud", "int"),
                ("rate", "Rate", "pct"),
                ("ci", "95% interval", "s"),
                ("lift", "Lift", "x"),
                ("verdict", "vs overall", "s"),
            ],
        )
    )
    add("")
    add(
        f"**Customers.** Activity is uneven: the busiest 10% of customers make "
        f"{pct(cust['top10_share'], 0)} of all transactions, and small businesses transact far "
        "more than students:"
    )
    add("")
    add(
        md_table(
            cust["spread"],
            [
                ("segment", "Segment", "s"),
                ("customers", "Customers", "int"),
                ("p50", "Median txns", "num"),
                ("p90", "90th pct", "num"),
                ("p99", "99th pct", "num"),
                ("max", "Max", "int"),
            ],
        )
    )
    add("")
    add(
        f"**Victims cluster.** {victims['victims']:,} customers have fraud-labelled "
        f"transactions; {pct(victims['repeat_share'], 0)} of fraud rows sit on customers with "
        f"more than one, and one customer has {victims['max_fraud']}. Fraud arrives as episodes "
        "(bursts, takeovers), not as isolated rows. Splitting rows at random between training "
        "and test would put parts of the same episode on both sides and overstate performance; "
        "the evaluation must split by time."
    )
    add("")
    add(
        f"**Counterparties.** The 10 busiest merchants take {pct(cps['merchant_top10_share'], 1)} "
        f"of merchant payments and the 10 busiest agents {pct(cps['agent_top10_share'], 1)} of "
        "agent transactions, so no single merchant or agent dominates. Share of fraud by "
        "counterparty type:"
    )
    add("")
    add(
        md_table(
            cps["fraud_share_by_cp"],
            [
                ("counterparty_type", "Counterparty", "s"),
                ("share_of_fraud", "Share of fraud", "pct1"),
            ],
        )
    )
    add("")

    add("## 6. Which behavioural signals separate fraud?")
    add("")
    add(f"![signals]({rel}/{FIGURES['signals']})")
    add("")
    add(
        "Each signal uses only information available at the time of the transaction (the "
        "customer's earlier history, never later rows). *Precision* is the fraud rate among "
        "transactions where the signal fires; *lift* divides that by the overall rate; "
        "*recall* is the share of all fraud the signal fires on."
    )
    add("")
    add(
        md_table(
            sig,
            [
                ("description", "Signal", "s"),
                ("prevalence", "Fires on", "pct"),
                ("precision", "Precision", "pct1"),
                ("lift", "Lift", "x"),
                ("recall", "Recall", "pct1"),
            ],
        )
    )
    add("")
    add(
        f"No single signal is both precise and complete. The best reach high lift on a small "
        f"slice of fraud; the combination of every signal reaches {pct(any_row['recall'], 0)} "
        f"recall but flags {pct(any_row['prevalence'])} of all transactions and is only "
        f"{pct(any_row['precision'])} precise. A model has to combine signals, which is the "
        "job of Phases 7 and 8. Large absolute amounts are not a fraud signal here: "
        f"the top 1% of amounts has lift {top1_lift}."
    )
    add("")

    add("## 7. Which candidate features overlap?")
    add("")
    add(f"![correlation]({rel}/{FIGURES['correlation']})")
    add("")
    add(
        "Spearman rank correlations on matured transactions. Features measuring the same thing "
        "overlap (amount and relative amount; the 10-minute and 1-hour counts), so a linear "
        "model would see collinearity and tree models would split importance between them. No "
        f"single feature correlates strongly with the fraud label (largest: {top_f}, "
        f"{top_c:.2f}), which is expected for a rare event driven by combinations of "
        "behaviours; rank correlation with a binary label this rare understates usefulness."
    )
    add("")

    if audit is not None:
        lab = audit["labels"]
        add("## 8. Audit against ground truth")
        add("")
        add(
            "> **This section uses the hidden ground truth, which a real analyst would not "
            "have.** It exists to check the simulation and the labels. Ground truth is never "
            "used for features, labels or any other analysis in this report."
        )
        add("")
        add(
            f"Among matured transactions {lab['true_fraud']:,} are truly fraudulent, of which "
            f"{lab['reported']:,} ({pct(_ratio(lab['reported'], lab['true_fraud']), 0)}) were "
            f"reported. {lab['false_reports']:,} legitimate transactions were reported as fraud, "
            f"so {pct(_ratio(lab['false_reports'], lab['labelled_positive']), 1)} of the "
            "fraud-labelled set is not fraud. With a rare positive class even a tiny false-report "
            "rate is a noticeable share of the positives; this puts a ceiling on achievable "
            "precision measured against the observed labels."
        )
        add("")
        add(
            "Share of each true fraud pattern that at least one simple signal fires on "
            "(lowest first):"
        )
        add("")
        add(
            md_table(
                audit["patterns"],
                [
                    ("pattern", "Pattern", "s"),
                    ("rows", "Rows", "int"),
                    ("reported_share", "Reported", "pct1"),
                    ("any_signal_share", "Any signal fires", "pct1"),
                    ("mean_signals", "Mean signals", "num1"),
                ],
            ).replace("| num |", "| num |")
        )
        add("")
        add(
            "Patterns near the top of this table are the ones simple per-transaction signals "
            "miss most; they are where a learned model has the most to add. The "
            "cash-out-after-inbound signal was designed knowing how the simulator creates mule "
            "cash-outs, so its coverage of that pattern is optimistic compared with real data."
        )
        add("")

    add("## Implications for the next phases")
    add("")
    add(
        "1. **Time-based splits with a label gap.** Train on earlier data, validate and test on "
        "later data, and apply only labels available at the training cutoff. Keep the final "
        f"~{DISPUTE_WINDOW_DAYS} days out of any test set that needs final labels."
    )
    add(
        "2. **Primary metric: PR-AUC**, with the prevalence "
        f"({pct(quality['rate'], 2)}) as the no-skill baseline, plus precision at a fixed "
        "review budget. Accuracy and ROC-AUC alone would flatter weak models."
    )
    add(
        "3. **Features that need history.** Amount relative to the customer, counts in the last "
        "10 minutes, 1 hour and 24 hours, time since the previous transaction, first-time device "
        "and recipient, county change, and cash-out shortly after an inbound transfer, all "
        "computed from earlier rows only."
    )
    add(
        "4. **A rule baseline is worth building first.** The combination of simple signals "
        f"already reaches {pct(any_row['recall'], 0)} recall at {pct(any_row['precision'])} "
        "precision; a model must beat that trade-off to justify itself (Phase 7)."
    )
    add(
        "5. **Expect label noise.** The observed labels miss some fraud and include some "
        "non-fraud; evaluation should say so and not treat them as truth."
    )
    add("")
    add("## Limitations")
    add("")
    add(
        "- The data is simulated; relationships here reflect the generator's design. Do not "
        "read them as facts about real mobile-money fraud."
    )
    add(
        "- Signal thresholds (5x average, 2 minutes, 10 minutes, 1 hour, 2 hours) are "
        "illustrative choices, not tuned."
    )
    add(
        f"- Matured statistics ignore the most recent {DISPUTE_WINDOW_DAYS} days, so any drift "
        "in that window is not visible here."
    )
    add(
        "- Group rates have wide intervals when fraud counts are small; the table marks which "
        "differences survive."
    )
    add("")

    text = "\n".join(L) + "\n"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(text, encoding="utf-8", newline="\n")
    return ReportResult(report_path, list(fig.values()), text)
