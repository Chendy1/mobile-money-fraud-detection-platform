"""LightGBM gradient boosting, tuned on the validation window only.

The booster is trained once per setting for a generous number of rounds; the number of rounds is
then chosen by scoring the validation window at several stopping points (no early stopping inside
training, so the training part never sees validation). The test window is not used here.
"""

from __future__ import annotations

import time

import lightgbm as lgb
import numpy as np
import polars as pl

from mmrisk.models.metrics import pr_auc
from mmrisk.models.preprocess import to_matrix
from mmrisk.models.split import Split

MAX_ROUNDS = 400
STAGES = (50, 100, 150, 200, 300, 400)


def lgbm_params(num_leaves: int, min_child_samples: int, pos_weight: float, seed: int,
                n_jobs: int = 2) -> dict:  # fmt: skip
    return {
        "objective": "binary", "learning_rate": 0.05, "num_leaves": num_leaves,
        "min_child_samples": min_child_samples, "feature_fraction": 0.8,
        "bagging_fraction": 0.8, "bagging_freq": 1, "lambda_l2": 1.0,
        "scale_pos_weight": pos_weight, "seed": seed, "deterministic": True,
        "force_row_wise": True, "num_threads": n_jobs, "verbose": -1,
    }  # fmt: skip


class LGBMModel:
    """A trained booster stored as text (portable across LightGBM versions), plus its settings."""

    name = "lightgbm"
    kind = "tree"

    def __init__(self, model_str: str, params: dict, n_rounds: int):
        self.model_str, self.params, self.n_rounds = model_str, params, n_rounds
        self._booster = lgb.Booster(model_str=model_str)

    def __getstate__(self):
        return {"model_str": self.model_str, "params": self.params, "n_rounds": self.n_rounds}

    def __setstate__(self, state):
        self.__init__(state["model_str"], state["params"], state["n_rounds"])

    def score(self, df: pl.DataFrame) -> np.ndarray:
        """Raw model probability (not calibrated)."""
        return self._booster.predict(to_matrix(df, "tree"), num_iteration=self.n_rounds)

    def gain_importance(self) -> dict[str, float]:
        from mmrisk.models.preprocess import column_names

        gains = self._booster.feature_importance(importance_type="gain")
        names = column_names("tree")
        total = float(gains.sum()) or 1.0
        return {n: float(g) / total for n, g in zip(names, gains, strict=True)}


def _grid(quick: bool, n_pos: int, n_neg: int) -> list[dict]:
    balanced = n_neg / max(1, n_pos)
    leaves = (4, 8) if quick else (4, 8, 16)
    child = (60,) if quick else (30, 60, 120)
    weights = (1.0,) if quick else (1.0, balanced)
    return [
        {"num_leaves": nl, "min_child_samples": mc, "pos_weight": w}
        for nl in leaves
        for mc in child
        for w in weights
    ]


def fit_lightgbm(
    split: Split, seed: int = 42, quick: bool = False, n_jobs: int = 2
) -> tuple[LGBMModel, pl.DataFrame]:
    """Returns the best model on validation PR-AUC and a table of every setting tried."""
    x_train, x_val = to_matrix(split.train, "tree"), to_matrix(split.val, "tree")
    y_train = split.train["label_is_fraud"].to_numpy()
    y_val = split.val["label_is_fraud"].to_numpy()
    n_pos = int(y_train.sum())
    rounds = 100 if quick else MAX_ROUNDS
    stages = (25, 50, 100) if quick else STAGES
    dtrain = lgb.Dataset(x_train, label=y_train, free_raw_data=False)
    rows, best, best_ap = [], None, -1.0
    for cfg in _grid(quick, n_pos, len(y_train) - n_pos):
        params = lgbm_params(
            cfg["num_leaves"], cfg["min_child_samples"], cfg["pos_weight"], seed, n_jobs
        )
        t0 = time.time()
        booster = lgb.train(params, dtrain, num_boost_round=rounds)
        seconds = time.time() - t0
        scored = [(k, pr_auc(y_val, booster.predict(x_val, num_iteration=k))) for k in stages]
        k_best, ap = max(scored, key=lambda t: t[1])
        rows.append(
            {
                "num_leaves": cfg["num_leaves"],
                "min_child_samples": cfg["min_child_samples"],
                "class_weight": "balanced" if cfg["pos_weight"] != 1.0 else "none",
                "rounds": k_best,
                "val_pr_auc": ap,
                "seconds": seconds,
            }
        )
        if ap > best_ap:
            best_ap = ap
            best = LGBMModel(booster.model_to_string(num_iteration=k_best), cfg, k_best)
    return best, pl.DataFrame(rows)
