"""Phase 8 experiment: LightGBM, calibration, and cost-based decisions, compared with the baselines.

Protocol (the same split as Phase 7):
  1. LightGBM settings are chosen on the validation window only.
  2. Every scorer is calibrated on the validation window (Platt scaling).
  3. Actions come from the cost model and the calibrated probability alone; no labels are used.
  4. The test window is then scored and costed once.
The cost numbers are assumptions (see mmrisk.models.costs); results are reported as "under these
assumptions", with a sensitivity analysis.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from pathlib import Path

import joblib
import numpy as np
import polars as pl

from mmrisk.features.registry import FEATURE_VERSION
from mmrisk.models.boosting import LGBMModel, fit_lightgbm
from mmrisk.models.bundle import RiskModel
from mmrisk.models.calibration import PlattCalibrator, brier, ece, reliability_table
from mmrisk.models.costs import CostModel
from mmrisk.models.metrics import customer_weights, interval, pr_auc
from mmrisk.models.split import Split, SplitConfig, make_split
from mmrisk.models.train import evaluate

SCORERS = ("rules", "logistic_regression", "random_forest", "lightgbm")
FRICTION_FACTORS = (0.25, 0.5, 1.0, 2.0, 4.0)
PREVENT_LEVELS = (0.5, 0.7, 0.9)
NAIVE_CUTOFF = 0.5


class BaselineError(RuntimeError):
    """Saved Phase 7 baselines are missing or were trained on a different split."""


def load_baselines(baselines_dir: Path, split: Split) -> dict:
    """The tuned Phase 7 models, refused if they do not belong to this split and feature set."""
    from mmrisk.models.baselines import PriorBaseline, RuleBaseline

    d = Path(baselines_dir)
    meta_file = d / "baseline_metadata.json"
    needed = [d / "logistic_regression.joblib", d / "random_forest.joblib", meta_file]
    missing = [p.name for p in needed if not p.exists()]
    if missing:
        raise BaselineError(
            f"missing {', '.join(missing)} in {d}; run `python -m mmrisk.models train` first"
        )
    meta = json.loads(meta_file.read_text())
    expected = {k: str(v) for k, v in split.bounds.items()}
    if meta.get("split") != expected or meta.get("feature_version") != FEATURE_VERSION:
        raise BaselineError(
            "the saved baselines were trained on a different split or feature set; "
            "re-run `python -m mmrisk.models train` on the current data"
        )
    return {
        "no_skill": PriorBaseline(),
        "rules": RuleBaseline(),
        "logistic_regression": joblib.load(d / "logistic_regression.joblib"),
        "random_forest": joblib.load(d / "random_forest.joblib"),
    }


@dataclass
class AdvancedResult:
    report_path: Path
    metrics: dict
    text: str


def _calibrators(models: dict, y_val: np.ndarray, val_scores: dict) -> dict:
    return {
        n: PlattCalibrator.fit(val_scores[n], y_val, "identity" if n == "rules" else "logit")
        for n in SCORERS
    }


def _policy_rows(cm: CostModel, p: dict, y: np.ndarray, amount: np.ndarray) -> dict:
    """Per-row realized cost for every policy (needs labels, which only the evaluation uses)."""
    rows = {"allow_all": cm.allow_all_cost(y, amount), "oracle": cm.oracle_cost(y, amount)}
    for n in SCORERS:
        rows[n] = cm.realized_cost(cm.decide(p[n], amount), y, amount)
    return rows


def cost_bootstrap(costs: dict, customers: np.ndarray, n: int, seed: int) -> dict:
    """Customer-level bootstrap of cost per 10,000 transactions for every policy, shared draws."""
    rng = np.random.default_rng(seed)
    out = {k: np.empty(n) for k in costs}
    for i in range(n):
        w = customer_weights(customers, rng)
        for k, c in costs.items():
            out[k][i] = float((w * c).sum() / w.sum() * 10_000)
    return out


def _action_stats(cm: CostModel, p: np.ndarray, y: np.ndarray, amount: np.ndarray) -> dict:
    a = cm.decide(p, amount)
    n, fraud = len(y), max(1.0, float(y.sum()))
    return {
        "reviews_per_10k": float((a == 1).sum() / n * 10_000),
        "blocks_per_10k": float((a == 2).sum() / n * 10_000),
        "fraud_blocked": float(((a == 2) & (y == 1)).sum() / fraud),
        "fraud_reviewed": float(((a == 1) & (y == 1)).sum() / fraud),
        "legit_blocked": int(((a == 2) & (y == 0)).sum()),
        "fraud_missed": float(((a == 0) & (y == 1)).sum() / fraud),
    }


def _savings(cm: CostModel, p: dict, y: np.ndarray, amount: np.ndarray) -> dict:
    base = cm.allow_all_cost(y, amount).sum()
    return {
        n: float(1 - cm.realized_cost(cm.decide(p[n], amount), y, amount).sum() / base)
        for n in SCORERS
    }


def truth_audit(split: Split, models: dict, cals: dict, cm: CostModel, truth_file) -> dict | None:
    if truth_file is None or not Path(truth_file).exists():
        return None
    truth = pl.read_parquet(truth_file).select("transaction_id", "true_is_fraud", "fraud_pattern")
    test = split.test.join(truth, on="transaction_id", how="left")
    y = test["true_is_fraud"].fill_null(False).cast(pl.Int64).to_numpy()
    amount = test["amount_kes"].to_numpy().astype(float)
    out: dict = {"true_fraud": int(y.sum()), "pr_auc": {}, "savings": {}, "patterns": []}
    p = {}
    for n in SCORERS:
        s = models[n].score(test)
        p[n] = cals[n].transform(s)
        out["pr_auc"][n] = pr_auc(y.astype(float), s)
    out["savings"] = _savings(cm, p, y, amount)
    action = cm.decide(p["lightgbm"], amount)
    pats = test["fraud_pattern"].drop_nulls().unique().sort().to_list()
    for pat in pats:
        m = (test["fraud_pattern"] == pat).fill_null(False).to_numpy()
        out["patterns"].append(
            {
                "pattern": pat,
                "rows": int(m.sum()),
                "blocked": float(((action == 2) & m).sum() / m.sum()),
                "reviewed": float(((action == 1) & m).sum() / m.sum()),
                "allowed": float(((action == 0) & m).sum() / m.sum()),
            }
        )
    return out


def run_advanced(
    features: pl.DataFrame,
    labels: pl.DataFrame,
    baselines_dir: Path | None,
    models_dir: Path,
    report_path: Path,
    figures_dir: Path,
    truth_file: Path | None = None,
    cost_model: CostModel | None = None,
    seed: int = 42,
    quick: bool = False,
    n_boot: int = 300,
    baselines: dict | None = None,
) -> AdvancedResult:
    from mmrisk.models import advanced_report as R

    cm = cost_model or CostModel()
    split = make_split(features, labels, SplitConfig())
    models = dict(baselines) if baselines is not None else load_baselines(baselines_dir, split)
    lgbm, lgbm_grid = fit_lightgbm(split, seed=seed, quick=quick)
    models["lightgbm"] = lgbm

    y_val = split.val["label_is_fraud"].to_numpy()
    y_test = split.test["label_is_fraud"].to_numpy()
    val_scores = {n: models[n].score(split.val) for n in SCORERS}
    cals = _calibrators(models, y_val, val_scores)

    val = evaluate(models, split.val, n_boot=n_boot, seed=1)
    test = evaluate(models, split.test, n_boot=n_boot, seed=2)  # scored once, after all choices

    test_scores = test["_scores"]
    p_test = {n: cals[n].transform(test_scores[n]) for n in SCORERS}
    amount = split.test["amount_kes"].to_numpy().astype(float)
    customers = split.test["customer_id"].to_numpy()
    y = y_test.astype(float)

    # ---- calibration on the test window ------------------------------------------------------
    raw = test_scores["lightgbm"]
    prevalence_train = float(split.train["label_is_fraud"].mean())
    calib = {
        "raw_rows": reliability_table(y, raw),
        "cal_rows": reliability_table(y, p_test["lightgbm"]),
        "brier": {
            "constant (training fraud rate)": brier(y, np.full(len(y), prevalence_train)),
            "lightgbm raw": brier(y, raw),
            **{f"{n} calibrated": brier(y, p_test[n]) for n in SCORERS},
        },
        "ece": {"lightgbm raw": ece(y, raw), **{n: ece(y, p_test[n]) for n in SCORERS}},
        "val_rate": float(y_val.mean()),
        "test_rate": float(y.mean()),
        "platt": {n: (cals[n].a, cals[n].b) for n in SCORERS},
    }

    # ---- what acting on the scores costs ---------------------------------------------------
    per_row = _policy_rows(cm, p_test, y, amount)
    per_row["naive"] = cm.realized_cost(
        np.where(raw >= NAIVE_CUTOFF, 2, 0), y, amount
    )  # block when the raw score is at least 0.5, otherwise allow
    boot = cost_bootstrap(per_row, customers, n_boot, seed=5)
    cost = {
        k: {
            "point": float(per_row[k].mean() * 10_000),
            "ci": interval(boot[k]),
            "saving": float(1 - per_row[k].sum() / per_row["allow_all"].sum()),
        }
        for k in per_row
    }
    stats = {n: _action_stats(cm, p_test[n], y_test, amount) for n in SCORERS}
    naive_blocks = int((raw >= NAIVE_CUTOFF).sum())
    sensitivity = {
        "factors": list(FRICTION_FACTORS),
        "savings": {
            n: [_savings(cm.scaled(f), p_test, y_test, amount)[n] for f in FRICTION_FACTORS]
            for n in SCORERS
        },
    }
    prevents = {
        pv: _savings(replace(cm, review_prevents=pv), p_test, y_test, amount)
        for pv in PREVENT_LEVELS
    }
    audit = truth_audit(split, models, cals, cm, truth_file)
    fraud_amounts = np.sort(amount[y_test == 1])[::-1]
    concentration = {
        "top5_share": float(fraud_amounts[:5].sum() / fraud_amounts.sum()),
        "median": float(np.median(fraud_amounts)),
        "largest": float(fraud_amounts[0]),
        "total": float(fraud_amounts.sum()),
    }

    # ---- artefacts -------------------------------------------------------------------------
    models_dir = Path(models_dir)
    bundle = RiskModel(lgbm, cals["lightgbm"], cm)
    bundle.save(
        models_dir,
        extra={
            "split": {k: str(v) for k, v in split.bounds.items()},
            "seed": seed,
            "validation_pr_auc": val["lightgbm"]["point"]["pr_auc"],
            "test_pr_auc": test["lightgbm"]["point"]["pr_auc"],
            "note": "cost model values are ASSUMPTIONS for this simulation, not measurements",
        },
    )
    figures_dir = Path(figures_dir)
    result = R.write(
        split=split, models=models, lgbm_grid=lgbm_grid, val=val, test=test, calib=calib,
        cm=cm, cost=cost, stats=stats, sensitivity=sensitivity, prevents=prevents, audit=audit,
        naive_blocks=naive_blocks, n_test=len(y_test), cost_boot=boot, concentration=concentration,
        report_path=Path(report_path),
        figures_dir=figures_dir, seed=seed, quick=quick, y_test=y_test,
    )  # fmt: skip
    result.metrics["calibrators"] = {
        n: {"a": cals[n].a, "b": cals[n].b, "link": cals[n].link, "fitted_on": "validation"}
        for n in SCORERS
    }
    (models_dir / "advanced_metrics.json").write_text(
        json.dumps(result.metrics, indent=2, default=str) + "\n"
    )
    return result


__all__ = ["BaselineError", "LGBMModel", "load_baselines", "run_advanced"]
