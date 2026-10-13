"""Runs the baseline experiment end to end and writes models, metrics and a Markdown report."""

from __future__ import annotations

import json
import platform
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import joblib
import numpy as np
import polars as pl
import sklearn

from mmrisk.features.registry import FEATURE_VERSION, feature_names
from mmrisk.models import figures as F
from mmrisk.models.baselines import RuleBaseline
from mmrisk.models.metrics import BUDGETS_PER_10K, interval, pr_auc
from mmrisk.models.split import Split, SplitConfig, make_split
from mmrisk.models.train import SklearnModel, evaluate, fit_models, refit_selected

LABELS = {m: v[3] for m, v in F.STYLE.items()}
ORDER = ["no_skill", "rules", "logistic_regression", "random_forest"]
FIG = {
    "timeline": "01-split-timeline.png",
    "pr": "02-precision-recall.png",
    "budget": "03-review-budget.png",
}


def pct(x: float, d: int = 1) -> str:
    return f"{x * 100:.{d}f}%"


def ci_text(point: float, ci: tuple[float, float], d: int = 3) -> str:
    return f"{point:.{d}f} ({ci[0]:.{d}f} to {ci[1]:.{d}f})"


def md(headers: list[str], rows: list[list[str]]) -> str:
    out = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    out += ["| " + " | ".join(r) + " |" for r in rows]
    return "\n".join(out)


@dataclass
class RunResult:
    report_path: Path
    metrics: dict
    text: str


def _truth_audit(split: Split, models: dict, truth_file: Path) -> dict | None:
    if truth_file is None or not Path(truth_file).exists():
        return None
    truth = pl.read_parquet(truth_file).select("transaction_id", "true_is_fraud", "fraud_pattern")
    test = split.test.join(truth, on="transaction_id", how="left")
    y_true = test["true_is_fraud"].fill_null(False).cast(pl.Int64).to_numpy()
    out: dict = {"true_fraud": int(y_true.sum()), "rows": test.height, "pr_auc": {}, "patterns": []}
    scores = {n: m.score(test) for n, m in models.items()}
    for n in ORDER:
        out["pr_auc"][n] = pr_auc(y_true.astype(float), scores[n])
    budget = max(1, round(100 / 10_000 * test.height))
    pats = [p for p in test["fraud_pattern"].drop_nulls().unique().sort().to_list()]
    for pat in pats:
        mask = (test["fraud_pattern"] == pat).fill_null(False).to_numpy()
        row = {"pattern": pat, "rows": int(mask.sum())}
        for n in ("logistic_regression", "random_forest"):
            top = np.argsort(-scores[n], kind="stable")[:budget]
            flagged = np.zeros(test.height, dtype=bool)
            flagged[top] = True
            row[n] = float((flagged & mask).sum() / mask.sum())
        out["patterns"].append(row)
    out["budget_rows"] = budget
    return out


def run(
    features: pl.DataFrame,
    labels: pl.DataFrame,
    models_dir: Path,
    report_path: Path,
    figures_dir: Path,
    truth_file: Path | None = None,
    seed: int = 42,
    quick: bool = False,
    n_boot: int = 300,
) -> RunResult:
    report_path, figures_dir, models_dir = Path(report_path), Path(figures_dir), Path(models_dir)
    split = make_split(features, labels, SplitConfig())
    exp = fit_models(split, seed=seed, quick=quick)
    models = exp.models
    val = evaluate(models, split.val, n_boot=n_boot, seed=1)
    # The test part is scored exactly once, after every setting above has been chosen on validation.
    test = evaluate(models, split.test, n_boot=n_boot, seed=2)

    naive_split = make_split(features, labels, SplitConfig(naive=True))
    naive_models = refit_selected(naive_split, exp)
    naive_val = evaluate(naive_models, naive_split.val, n_boot=max(50, n_boot // 3), seed=3)
    naive_test = evaluate(naive_models, naive_split.test, n_boot=max(50, n_boot // 3), seed=4)
    audit = _truth_audit(split, models, truth_file)

    # ---- artefacts ------------------------------------------------------------------------
    models_dir.mkdir(parents=True, exist_ok=True)
    meta = {
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "feature_version": FEATURE_VERSION,
        "features": feature_names(),
        "seed": seed,
        "split": {k: str(v) for k, v in split.bounds.items()},
        "selected": {n: m.params for n, m in models.items() if isinstance(m, SklearnModel)},
        "test_pr_auc": {n: test[n]["point"]["pr_auc"] for n in ORDER},
        "versions": {"scikit-learn": sklearn.__version__, "python": platform.python_version()},
    }
    for n, m in models.items():
        if isinstance(m, SklearnModel):
            joblib.dump(m, models_dir / f"{n}.joblib")
    (models_dir / "baseline_metadata.json").write_text(json.dumps(meta, indent=2) + "\n")

    fig = {k: figures_dir / v for k, v in FIG.items()}
    F.timeline(split.bounds, split.train["timestamp"].min(), fig["timeline"])
    y_test = split.test["label_is_fraud"].to_numpy()
    prevalence = float(y_test.mean())
    aps = {n: test[n]["point"]["pr_auc"] for n in ORDER}
    F.pr_curves(y_test, test["_scores"], aps, prevalence, fig["pr"])
    F.budgets(test, ORDER[1:], fig["budget"])

    # ---- report text ----------------------------------------------------------------------
    rel = Path(figures_dir).relative_to(report_path.parent).as_posix() if (
        figures_dir.is_relative_to(report_path.parent)
    ) else figures_dir.as_posix()  # fmt: skip
    text = _write(
        split, exp, models, val, test, naive_val, naive_test, audit, prevalence, rel, seed
    )
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(text, encoding="utf-8", newline="\n")
    metrics = {"validation": _plain(val), "test": _plain(test), "meta": meta}
    (models_dir / "baseline_metrics.json").write_text(
        json.dumps(metrics, indent=2, default=str) + "\n"
    )
    return RunResult(report_path, metrics, text)


def _params_text(params: dict) -> str:
    return ", ".join(f"{k}={v}" for k, v in params.items())


def _plain(res: dict) -> dict:
    return {k: v for k, v in res.items() if not k.startswith("_")}


def _diff(boot: dict, a: str, b: str) -> tuple[float, tuple[float, float]]:
    d = boot[a] - boot[b]
    return float(np.mean(d)), interval(d)


def _write(
    split, exp, models, val, test, naive_val, naive_test, audit, prevalence, rel, seed
) -> str:
    b = split.bounds
    summ = split.summary()
    L: list[str] = []
    add = L.append
    test_fraud = int(summ.filter(pl.col("part") == "test")["fraud"][0])
    rf_lr = _diff(test["_boot_pr_auc"], "random_forest", "logistic_regression")
    rf_rules = _diff(test["_boot_pr_auc"], "random_forest", "rules")
    lr_rules = _diff(test["_boot_pr_auc"], "logistic_regression", "rules")

    def excludes_zero(ci):
        return ci[0] > 0 or ci[1] < 0

    add("# Baseline models")
    add("")
    add(
        "> **All data is synthetic.** These numbers describe how simple models do on a simulated "
        "dataset, not how they would do on real mobile-money fraud. Generated by "
        "`python -m mmrisk.models train`; every number below is computed by that run."
    )
    add("")
    add("## Key results")
    add("")
    t = {n: test[n] for n in ORDER}
    add(
        f"- **Setup.** Train on transactions up to {b['train_end']:%Y-%m-%d}, tune on "
        f"{b['val_start']:%Y-%m-%d} to {b['test_start']:%Y-%m-%d}, test on "
        f"{b['test_start']:%Y-%m-%d} to {b['matured_end']:%Y-%m-%d}, with a {b['gap_days']}-day "
        "gap between training and validation so that labels and fraud episodes cannot cross the "
        "boundary."
    )
    add(
        f"- **Test PR-AUC** (primary metric; a random scorer scores the fraud rate, "
        f"{pct(prevalence, 2)}): rules {t['rules']['point']['pr_auc']:.2f}, logistic regression "
        f"{t['logistic_regression']['point']['pr_auc']:.2f}, random forest "
        f"{t['random_forest']['point']['pr_auc']:.2f}."
    )
    add(
        f"- **Learned models vs the rules.** Random forest minus rules: {rf_rules[0]:+.2f} "
        f"(95% interval {rf_rules[1][0]:+.2f} to {rf_rules[1][1]:+.2f}); logistic regression minus "
        f"rules: {lr_rules[0]:+.2f} ({lr_rules[1][0]:+.2f} to {lr_rules[1][1]:+.2f}). "
        + (
            "Both intervals exclude zero."
            if excludes_zero(rf_rules[1]) and excludes_zero(lr_rules[1])
            else "At least one interval includes zero."
        )
    )
    add(
        f"- **Random forest vs logistic regression.** Difference {rf_lr[0]:+.2f} "
        f"({rf_lr[1][0]:+.2f} to {rf_lr[1][1]:+.2f}): "
        + (
            "the test period separates them."
            if excludes_zero(rf_lr[1])
            else "the test period cannot separate them; do not claim one is better."
        )
    )
    p100 = {n: t[n]["point"]["precision@100"] for n in ORDER}
    r100 = {n: t[n]["point"]["recall@100"] for n in ORDER}
    add(
        f"- **At 100 alerts per 10,000 transactions** the random forest's alerts are "
        f"{pct(p100['random_forest'], 0)} fraud and catch {pct(r100['random_forest'], 0)} of fraud "
        f"(logistic regression: {pct(p100['logistic_regression'], 0)} and "
        f"{pct(r100['logistic_regression'], 0)}; rules: {pct(p100['rules'], 0)} and "
        f"{pct(r100['rules'], 0)})."
    )
    add("")

    add("## 1. What data does each part of the evaluation use?")
    add("")
    add(f"![split timeline]({rel}/{FIG['timeline']})")
    add("")
    add(
        md(
            ["Part", "Rows", "Fraud", "Fraud rate", "First", "Last", "Customers"],
            [
                [
                    r["part"],
                    f"{r['rows']:,}",
                    f"{r['fraud']:,}",
                    pct(r["fraud_rate"], 2),
                    f"{r['first']:%Y-%m-%d}",
                    f"{r['last']:%Y-%m-%d}",
                    f"{r['customers']:,}",
                ]  # fmt: skip
                for r in summ.iter_rows(named=True)
            ],
        )
    )
    add("")
    add(
        f"The model is imagined to be deployed on {b['val_start']:%Y-%m-%d}. Fraud takes up to "
        f"45 days to be reported, and legitimate transactions are only confirmed after 45 days, so "
        f"a transaction newer than {b['train_end']:%Y-%m-%d} would have an incomplete label on "
        "that day. Training therefore uses only transactions older than that and only the labels "
        "known on deployment day. Validation and test rows are scored against their final labels, "
        "because that is how a model would be judged later. The last 45 days of data are not "
        "evaluated at all: their labels are still arriving. Everyone in the test period was "
        "already a customer earlier; the evaluation says nothing about brand-new customers."
    )
    add("")
    add(
        f"The test part has {test_fraud} fraud-labelled "
        "transactions, so every metric carries wide uncertainty. Intervals below are 95% "
        "bootstrap intervals that resample whole customers (a customer's transactions, and a "
        "fraud episode, are not independent)."
    )
    add("")

    add("## 2. The models")
    add("")
    rules = RuleBaseline().rule_table(split.val)
    add(
        "**No skill** gives every transaction the same score; its PR-AUC equals the fraud rate. "
        "**Hand-written rules** score a transaction by how many of eight fixed rules fire. The "
        "thresholds come from the exploratory analysis and are not fitted or tuned on any split "
        "(validation figures shown):"
    )
    add("")
    add(
        md(
            ["Rule", "Meaning", "Fires on", "Precision", "Recall"],
            [
                [
                    r["rule"],
                    r["description"],
                    pct(r["fires_on"], 2),
                    "n/a" if r["precision"] is None else pct(r["precision"]),
                    pct(r["recall"]),
                ]  # fmt: skip
                for r in rules.iter_rows(named=True)
            ],
        )
    )
    add("")
    add(
        "**Logistic regression** (log-transformed counts and amounts, missing values replaced by "
        "the training median plus a missing-indicator, standardised) and **random forest** (raw "
        "values, "
        "missing values replaced by -1 plus an indicator) are fitted on the training part only. "
        "Settings were chosen by validation PR-AUC from a small grid:"
    )
    add("")
    grid = exp.grid
    add(
        md(
            ["Model", "Settings", "Validation PR-AUC", "Chosen"],
            [
                [
                    r["family"].replace("_", " "),
                    r["params"].replace("class_weight=", "weights="),
                    f"{r['val_pr_auc']:.3f}",
                    "yes" if r["params"] == _params_text(models[r["family"]].params) else "",
                ]  # fmt: skip
                for r in grid.iter_rows(named=True)
            ],
        )
    )
    add("")
    best_w = (
        grid.with_columns(
            w=pl.when(pl.col("params").str.contains("class_weight=None"))
            .then(pl.lit("none"))
            .otherwise(pl.lit("balanced"))
        )
        .group_by("family", "w")
        .agg(pl.col("val_pr_auc").max().alias("best"))
        .sort("family", "w")
    )
    parts, deltas = [], []
    for fam in ("logistic_regression", "random_forest"):
        sub = best_w.filter(pl.col("family") == fam)
        d = {r["w"]: r["best"] for r in sub.iter_rows(named=True)}
        if "none" in d and "balanced" in d:
            deltas.append(d["balanced"] - d["none"])
            parts.append(
                f"{fam.replace('_', ' ')}: best validation PR-AUC {d['none']:.3f} without class "
                f"weights against {d['balanced']:.3f} with them"
            )
    if parts:
        verb = (
            "lowered" if all(x < 0 for x in deltas)
            else "raised" if all(x > 0 for x in deltas) else "had mixed effects on"
        )  # fmt: skip
        add(
            f"Class weighting is a common reflex for rare events. Here it {verb} ranking quality: "
            + "; ".join(parts)
            + ". Weights change the probabilities a model outputs and can change the ranking, so "
            "they are a setting to test and not a default."
        )
        add("")
    edge = [
        n for n, m in models.items()
        if isinstance(m, SklearnModel) and "min_samples_leaf" in m.params
        and m.params["min_samples_leaf"] == min(
            int(p.split("min_samples_leaf=")[1].split(",")[0])
            for p in grid.filter(pl.col("family") == n)["params"].to_list()
        )
    ]  # fmt: skip
    if edge:
        add(
            "The chosen random forest sits at the smallest `min_samples_leaf` in the grid, "
            "so a smaller value might do better; the grid was not extended."
        )
        add("")

    add("## 3. How well do they rank fraud on the test period?")
    add("")
    add(f"![precision-recall]({rel}/{FIG['pr']})")
    add("")
    add(
        md(
            ["Model", "PR-AUC (95% interval)", "ROC-AUC", "Validation PR-AUC"],
            [
                [
                    LABELS[n],
                    ci_text(t[n]["point"]["pr_auc"], t[n]["ci"]["pr_auc"]),
                    f"{t[n]['point']['roc_auc']:.3f}",
                    f"{val[n]['point']['pr_auc']:.3f}",
                ]  # fmt: skip
                for n in ORDER
            ],
        )
    )
    add("")
    add(
        f"PR-AUC is the primary metric because fraud is {pct(prevalence, 2)} of transactions; "
        "ROC-AUC is shown only for completeness and looks excellent for almost any model on rare "
        "events. PR-AUC rises with the fraud rate (validation "
        f"{pct(summ.filter(pl.col('part') == 'validation')['fraud_rate'][0], 2)}, test "
        f"{pct(prevalence, 2)}), so compare models within one period, not across periods."
    )
    add("")
    add("## 4. What does a fixed review budget buy?")
    add("")
    add(f"![review budget]({rel}/{FIG['budget']})")
    add("")
    add(
        "Analysts can review a limited number of alerts. Flagging the top N per 10,000 "
        "transactions (test period; 95% interval in brackets):"
    )
    add("")
    rows = []
    for n in ORDER[1:]:
        for stat in ("precision", "recall"):
            rows.append(
                [LABELS[n], stat]
                + [
                    ci_text(t[n]["point"][f"{stat}@{v}"], t[n]["ci"][f"{stat}@{v}"], 2)
                    for v in BUDGETS_PER_10K
                ]
            )
    add(md(["Model", "Metric"] + [f"{v} alerts" for v in BUDGETS_PER_10K], rows))
    add("")
    add(
        "Ties in the rule engine's score (many transactions fire the same number of rules) are "
        "handled by taking tied transactions in proportion, the expected result of random "
        "tie-breaking."
    )
    add("")
    add("## 5. Does a careless split inflate the scores?")
    add("")
    nv, nt = naive_val, naive_test
    rows = []
    for n in ("logistic_regression", "random_forest"):
        for name, a, bb in (("validation", val, nv), ("test", test, nt)):
            diff = bb[n]["point"]["pr_auc"] - a[n]["point"]["pr_auc"]
            diff = round(diff, 3) + 0.0
            rows.append(
                [LABELS[n], name, f"{a[n]['point']['pr_auc']:.3f}",
                 f"{bb[n]['point']['pr_auc']:.3f}",
                 f"{diff:+.3f}"]
            )  # fmt: skip
    add(
        "The same settings, refitted with no gap and with the final label of every training row "
        "(the usual shortcut):"
    )
    add("")
    add(md(["Model", "Part", "Correct split", "No gap, final labels", "Change"], rows))
    add("")
    changes = [float(r[-1]) + 0.0 for r in rows]
    half_width = min(
        (test[n]["ci"]["pr_auc"][1] - test[n]["ci"]["pr_auc"][0]) / 2
        for n in ("logistic_regression", "random_forest")
    )
    biggest = max(abs(c) for c in changes)
    verdict = (
        "That is small next to the bootstrap intervals above (half-width about "
        f"{half_width:.2f}), so on this simulated data the careless split would not have been "
        "detected by its scores."
        if biggest < half_width
        else "That is larger than the half-width of the bootstrap intervals above (about "
        f"{half_width:.2f}), so the careless split visibly changes the picture."
    )
    add(
        f"The shortcut changed PR-AUC by between {min(changes):+.3f} and {max(changes):+.3f}. "
        f"{verdict} The gap is kept because the labels it avoids are not available in reality; "
        "real data with stronger drift or longer fraud episodes may behave differently."
    )
    add("")

    if audit is not None:
        add("## 6. Audit against ground truth")
        add("")
        add(
            "> **This section uses the hidden ground truth, which a real team would not have.** It "
            "is for checking the simulation and the label noise. Nothing here influenced training, "
            "tuning or selection."
        )
        add("")
        add(
            f"Test-period PR-AUC measured against the *true* fraud flag ({audit['true_fraud']} "
            "true fraud rows) instead of the noisy labels: "
            + ", ".join(f"{LABELS[n].lower()} {audit['pr_auc'][n]:.2f}" for n in ORDER[1:])
            + ". Observed-label PR-AUC for comparison: "
            + ", ".join(f"{t[n]['point']['pr_auc']:.2f}" for n in ORDER[1:])
            + (
                ". Scores against the true flag are higher for every model, so label noise "
                "(missed fraud and false reports) holds the observed-label scores down."
                if all(audit["pr_auc"][n] > t[n]["point"]["pr_auc"] for n in ORDER[1:])
                else ". Scores against the true flag are not uniformly higher."
            )
        )
        add("")
        add(
            f"Share of each true fraud pattern inside the top {audit['budget_rows']} alerts "
            "(100 per 10,000) of the learned models, test period:"
        )
        add("")
        add(
            md(
                ["Pattern", "Rows", "Logistic regression", "Random forest"],
                [
                    [
                        r["pattern"],
                        str(r["rows"]),
                        pct(r["logistic_regression"], 0),
                        pct(r["random_forest"], 0),
                    ]
                    for r in audit["patterns"]
                ],
            )
        )
        add("")
        rated = [r for r in audit["patterns"] if r["rows"] >= 10]
        if rated:
            weak = sorted(rated, key=lambda r: r["random_forest"])[:2]
            add(
                "Among patterns with at least 10 test rows, the random forest finds the smallest "
                "share of "
                + " and ".join(f"{r['pattern']} ({pct(r['random_forest'], 0)})" for r in weak)
                + ": these are the patterns the current features describe least well, and where "
                "better features or a stronger model would have the most to add."
            )
            add("")

    add("## What this does not show")
    add("")
    add(
        "- The data is simulated, and the signals the models use were built into the simulator. "
        "Absolute scores say nothing about real fraud."
    )
    add(
        "- The test period holds a small number of fraud cases; differences smaller than the "
        "intervals are noise."
    )
    add(
        "- The grid is small and was tuned on one validation window"
        + ("; the random forest's best setting is at an edge of the grid." if edge else ".")
    )
    add(
        "- Probabilities are not calibrated and no decision threshold or cost model is applied; "
        "that is Phase 8."
    )
    add(
        "- Test numbers were produced once by this command after settings were fixed on "
        "validation, but the author also looked at test scores in earlier development runs of the "
        "same code; no setting was changed on that basis."
    )
    add("")
    return "\n".join(L) + "\n"
