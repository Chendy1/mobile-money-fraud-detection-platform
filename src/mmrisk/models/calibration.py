"""Turn model scores into probabilities, and measure how trustworthy those probabilities are.

Platt scaling fits a two-parameter logistic curve, p = sigmoid(a * x + b), on a held-out part (the
validation window), never on the data used to train the model or to report results. With only a
few hundred fraud cases a two-parameter curve is steadier than isotonic regression, which would
chase noise. `link="logit"` takes a model probability and uses log(p / (1 - p)) as x;
`link="identity"` uses the score as it is (for example the number of rules that fired).
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from sklearn.linear_model import LogisticRegression

EPS = 1e-6
# Fixed probability bins for rare events (a quantile split would put almost everything near zero).
RELIABILITY_EDGES = (0.0, 0.001, 0.003, 0.01, 0.03, 0.1, 0.3, 1.0000001)


def _x(score: np.ndarray, link: str) -> np.ndarray:
    s = np.asarray(score, dtype=float)
    if link == "logit":
        s = np.clip(s, EPS, 1 - EPS)
        return np.log(s / (1 - s))
    if link == "identity":
        return s
    raise ValueError("link must be 'logit' or 'identity'")


@dataclass(frozen=True)
class PlattCalibrator:
    a: float
    b: float
    link: str = "logit"

    @classmethod
    def fit(cls, score: np.ndarray, y: np.ndarray, link: str = "logit") -> PlattCalibrator:
        y = np.asarray(y).astype(int)
        if y.min() == y.max():
            raise ValueError("calibration needs both classes in the fitting data")
        x = _x(score, link).reshape(-1, 1)
        lr = LogisticRegression(C=1e6, max_iter=5000).fit(x, y)  # effectively unregularised
        return cls(float(lr.coef_[0, 0]), float(lr.intercept_[0]), link)

    def transform(self, score: np.ndarray) -> np.ndarray:
        z = self.a * _x(score, self.link) + self.b
        return 1.0 / (1.0 + np.exp(-z))


def brier(y: np.ndarray, p: np.ndarray) -> float:
    return float(np.mean((np.asarray(p, dtype=float) - np.asarray(y, dtype=float)) ** 2))


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n <= 0:
        return 0.0, 1.0
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * np.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return max(0.0, float(c - h)), min(1.0, float(c + h))


def reliability_table(y: np.ndarray, p: np.ndarray, edges=RELIABILITY_EDGES) -> list[dict]:
    """Per probability bin: rows, mean predicted, observed fraud rate and its 95% interval."""
    y = np.asarray(y, dtype=float)
    p = np.asarray(p, dtype=float)
    rows = []
    for lo, hi in zip(edges[:-1], edges[1:], strict=True):
        m = (p >= lo) & (p < hi)
        n = int(m.sum())
        if n == 0:
            continue
        k = int(y[m].sum())
        ci = wilson(k, n)
        rows.append(
            {
                "low": lo, "high": min(hi, 1.0), "rows": n, "fraud": k,
                "predicted": float(p[m].mean()), "observed": k / n,
                "ci_low": ci[0], "ci_high": ci[1],
            }
        )  # fmt: skip
    return rows


def ece(y: np.ndarray, p: np.ndarray, edges=RELIABILITY_EDGES) -> float:
    """Expected calibration error: row-weighted gap between predicted and observed, fixed bins."""
    table = reliability_table(y, p, edges)
    total = sum(r["rows"] for r in table)
    return float(sum(r["rows"] * abs(r["predicted"] - r["observed"]) for r in table) / total)
