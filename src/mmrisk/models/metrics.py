"""Evaluation metrics for a rare-event ranking problem, with customer-level bootstrap intervals.

Every metric accepts optional row weights, so one bootstrap resample (each customer drawn a random
number of times) is just a weight vector. Resampling customers rather than rows respects that a
customer's transactions, and a fraud episode, are not independent.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping

import numpy as np
from sklearn.metrics import average_precision_score, roc_auc_score

# Review budgets as alerts per 10,000 transactions.
BUDGETS_PER_10K = (10, 25, 50, 100, 200, 500)


def pr_auc(y: np.ndarray, score: np.ndarray, w: np.ndarray | None = None) -> float:
    """Average precision (area under the precision-recall curve). No-skill = fraud rate."""
    return float(average_precision_score(y, score, sample_weight=w))


def roc_auc(y: np.ndarray, score: np.ndarray, w: np.ndarray | None = None) -> float:
    return float(roc_auc_score(y, score, sample_weight=w))


def budget_point(
    y: np.ndarray, score: np.ndarray, per_10k: float, w: np.ndarray | None = None
) -> tuple[float, float]:
    """(precision, recall) when the highest-scored `per_10k` of every 10,000 rows are flagged.

    Rows with equal scores are taken in proportion (the expected result under random tie-breaking),
    so a rule engine with many ties is not helped or hurt by row order.
    """
    w = np.ones(len(y)) if w is None else np.asarray(w, dtype=float)
    total_w = w.sum()
    total_fraud = float((w * y).sum())
    if total_w <= 0 or total_fraud <= 0:
        return float("nan"), float("nan")
    target = per_10k / 10_000 * total_w
    uniq, inv = np.unique(-score, return_inverse=True)  # ascending in -score = descending score
    gw = np.bincount(inv, weights=w)
    gf = np.bincount(inv, weights=w * y)
    cum_w = np.cumsum(gw)
    cum_f = np.cumsum(gf)
    i = int(np.searchsorted(cum_w, target, side="left"))
    if i >= len(gw):
        return total_fraud / total_w, 1.0
    before_w = cum_w[i - 1] if i else 0.0
    before_f = cum_f[i - 1] if i else 0.0
    share = (target - before_w) / gw[i] if gw[i] > 0 else 0.0
    caught = before_f + share * gf[i]
    return float(caught / target), float(caught / total_fraud)


def precision_at(y, score, per_10k, w=None) -> float:
    return budget_point(y, score, per_10k, w)[0]


def recall_at(y, score, per_10k, w=None) -> float:
    return budget_point(y, score, per_10k, w)[1]


def summarize(y: np.ndarray, score: np.ndarray, w: np.ndarray | None = None) -> dict[str, float]:
    out = {"pr_auc": pr_auc(y, score, w), "roc_auc": roc_auc(y, score, w)}
    for b in BUDGETS_PER_10K:
        p, r = budget_point(y, score, b, w)
        out[f"precision@{b}"] = p
        out[f"recall@{b}"] = r
    return out


def customer_weights(customers: np.ndarray, rng: np.random.Generator) -> np.ndarray:
    """Row weights for one customer-level bootstrap resample."""
    uniq, inv = np.unique(customers, return_inverse=True)
    counts = rng.multinomial(len(uniq), np.full(len(uniq), 1.0 / len(uniq)))
    return counts[inv].astype(float)


def bootstrap(
    y: np.ndarray,
    scores: Mapping[str, np.ndarray],
    customers: np.ndarray,
    stats: Mapping[str, Callable[[np.ndarray, np.ndarray, np.ndarray], float]],
    n: int = 300,
    seed: int = 0,
) -> dict[str, dict[str, np.ndarray]]:
    """{model: {stat: array of n values}} with every model scored on the same resamples."""
    rng = np.random.default_rng(seed)
    out = {m: {s: np.empty(n) for s in stats} for m in scores}
    for k in range(n):
        w = customer_weights(customers, rng)
        for m, sc in scores.items():
            for s, fn in stats.items():
                out[m][s][k] = fn(y, sc, w)
    return out


def interval(values: np.ndarray, level: float = 0.95) -> tuple[float, float]:
    values = values[~np.isnan(values)]
    lo, hi = (1 - level) / 2, 1 - (1 - level) / 2
    return float(np.quantile(values, lo)), float(np.quantile(values, hi))
