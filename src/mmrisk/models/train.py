"""Fit baseline models on the training part, choose settings on validation, score test once."""

from __future__ import annotations

import time
from dataclasses import dataclass, field

import numpy as np
import polars as pl
from sklearn.ensemble import RandomForestClassifier
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

from mmrisk.models.baselines import PriorBaseline, RuleBaseline
from mmrisk.models.metrics import (
    BUDGETS_PER_10K,
    bootstrap,
    interval,
    pr_auc,
    precision_at,
    recall_at,
    summarize,
)
from mmrisk.models.preprocess import to_matrix
from mmrisk.models.split import Split


class SklearnModel:
    """A fitted scikit-learn pipeline plus the stateless matrix builder it was trained with."""

    def __init__(self, name: str, kind: str, pipeline: Pipeline, params: dict):
        self.name, self.kind, self.pipeline, self.params = name, kind, pipeline, params

    def score(self, df: pl.DataFrame) -> np.ndarray:
        return self.pipeline.predict_proba(to_matrix(df, self.kind))[:, 1]


def make_logistic(C: float, class_weight: str | None, seed: int) -> Pipeline:
    return Pipeline(
        [
            ("impute", SimpleImputer(strategy="median", add_indicator=True)),
            ("scale", StandardScaler()),
            (
                "model",
                LogisticRegression(
                    C=C, class_weight=class_weight, max_iter=3000, random_state=seed
                ),
            ),
        ]
    )


def make_forest(min_samples_leaf: int, class_weight: str | None, n_estimators: int, seed: int):
    return Pipeline(
        [
            ("impute", SimpleImputer(strategy="constant", fill_value=-1.0, add_indicator=True)),
            (
                "model",
                RandomForestClassifier(
                    n_estimators=n_estimators,
                    min_samples_leaf=min_samples_leaf,
                    class_weight=class_weight,
                    max_features="sqrt",
                    n_jobs=2,
                    random_state=seed,
                ),
            ),
        ]
    )


def _grids(quick: bool, n_trees: int) -> dict[str, list[dict]]:
    lr = [
        {"C": c, "class_weight": w}
        for c in ([0.1, 1.0] if quick else [0.01, 0.1, 1.0, 10.0])
        for w in (["balanced"] if quick else ["balanced", None])
    ]
    rf = [
        {"min_samples_leaf": leaf, "class_weight": w, "n_estimators": n_trees}
        for leaf in ([5] if quick else [3, 10, 30])
        for w in (["balanced_subsample"] if quick else ["balanced_subsample", None])
    ]
    return {"logistic_regression": lr, "random_forest": rf}


@dataclass
class Experiment:
    split: Split
    models: dict  # name -> object with .score(df)
    grid: pl.DataFrame  # every candidate: family, params, validation PR-AUC, seconds
    seed: int
    notes: dict = field(default_factory=dict)


def fit_models(split: Split, seed: int = 42, quick: bool = False, n_trees: int = 300) -> Experiment:
    y_train = split.train["label_is_fraud"].to_numpy()
    y_val = split.val["label_is_fraud"].to_numpy()
    rows = []
    best: dict[str, tuple[float, SklearnModel]] = {}
    for family, grid in _grids(quick, 100 if quick else n_trees).items():
        kind = "linear" if family == "logistic_regression" else "tree"
        x_train = to_matrix(split.train, kind)
        x_val = to_matrix(split.val, kind)
        for params in grid:
            t0 = time.time()
            pipe = (
                make_logistic(params["C"], params["class_weight"], seed)
                if kind == "linear"
                else make_forest(
                    params["min_samples_leaf"], params["class_weight"], params["n_estimators"], seed
                )
            )
            pipe.fit(x_train, y_train)
            val_ap = pr_auc(y_val, pipe.predict_proba(x_val)[:, 1])
            rows.append(
                {
                    "family": family,
                    "params": ", ".join(f"{k}={v}" for k, v in params.items()),
                    "val_pr_auc": val_ap,
                    "seconds": time.time() - t0,
                }
            )
            if family not in best or val_ap > best[family][0]:
                best[family] = (val_ap, SklearnModel(family, kind, pipe, params))
    models = {"no_skill": PriorBaseline(), "rules": RuleBaseline()}
    models.update({fam: m for fam, (_, m) in best.items()})
    return Experiment(split, models, pl.DataFrame(rows), seed)


def evaluate(models: dict, part: pl.DataFrame, n_boot: int = 300, seed: int = 0) -> dict[str, dict]:
    """Metrics with 95% customer-bootstrap intervals for every model on one data part."""
    y = part["label_is_fraud"].to_numpy().astype(float)
    customers = part["customer_id"].to_numpy()
    scores = {name: m.score(part) for name, m in models.items()}
    point = {name: summarize(y, s) for name, s in scores.items()}
    stats = {"pr_auc": lambda y_, s_, w_: pr_auc(y_, s_, w_)}
    for b in BUDGETS_PER_10K:
        stats[f"precision@{b}"] = lambda y_, s_, w_, b=b: precision_at(y_, s_, b, w_)
        stats[f"recall@{b}"] = lambda y_, s_, w_, b=b: recall_at(y_, s_, b, w_)
    boots = bootstrap(y, scores, customers, stats, n=n_boot, seed=seed)
    out: dict[str, dict] = {}
    for name in models:
        out[name] = {
            "point": point[name],
            "ci": {s: interval(v) for s, v in boots[name].items()},
        }
    out["_scores"] = scores
    out["_boot_pr_auc"] = {m: boots[m]["pr_auc"] for m in models}
    return out


def refit_selected(split: Split, experiment: Experiment) -> dict:
    """Refit the already-selected settings on a different split (no new tuning)."""
    y = split.train["label_is_fraud"].to_numpy()
    models: dict = {"no_skill": PriorBaseline(), "rules": RuleBaseline()}
    for family, chosen in experiment.models.items():
        if not isinstance(chosen, SklearnModel):
            continue
        p = chosen.params
        pipe = (
            make_logistic(p["C"], p["class_weight"], experiment.seed)
            if chosen.kind == "linear"
            else make_forest(
                p["min_samples_leaf"], p["class_weight"], p["n_estimators"], experiment.seed
            )
        )
        pipe.fit(to_matrix(split.train, chosen.kind), y)
        models[family] = SklearnModel(family, chosen.kind, pipe, p)
    return models
