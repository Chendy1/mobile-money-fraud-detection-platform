"""Turn the feature table into model matrices.

`to_matrix` is stateless: it only transforms values row by row (logs, flags, fixed one-hot
vocabularies), so it cannot learn anything from the data it sees. Everything that is *learned*
(medians for missing values, scaling) lives in the scikit-learn pipeline and is fitted on the
training part only.
"""

from __future__ import annotations

import numpy as np
import polars as pl

from mmrisk.features.registry import FEATURES, feature_names
from mmrisk.validation import config as vcfg

CATEGORIES = {
    "transaction_type": sorted(vcfg.TRANSACTION_TYPES),
    "channel": sorted(vcfg.CHANNELS),
    "segment": sorted(vcfg.SEGMENTS),
}
LOG1P = {
    "amount_kes", "secs_since_prev_txn", "txn_count_10m", "txn_count_1h", "txn_count_24h",
    "txn_count_7d", "txn_count_30d", "amount_sum_1h", "amount_sum_24h", "avg_amount_30d",
    "amount_to_avg_30d", "n_prior_txns", "secs_since_last_inbound",
}  # fmt: skip
CYCLIC = {"hour": 24.0}  # replaced by sin and cos so that 23:00 and 00:00 are close


def column_names(kind: str) -> list[str]:
    """Names of the matrix columns, in order."""
    names: list[str] = []
    for f in FEATURES:
        if f.dtype == "category":
            names += [f"{f.name}={v}" for v in CATEGORIES[f.name]]
        elif f.name in CYCLIC:
            names += [f"{f.name}_sin", f"{f.name}_cos"]
        elif f.name == "amount_log":
            if kind == "tree":
                names.append(f.name)  # linear models already get log1p(amount_kes)
        else:
            names.append(f.name)
    return names


def to_matrix(df: pl.DataFrame, kind: str = "linear") -> np.ndarray:
    """Float matrix (NaN where a feature is missing). kind: "linear" or "tree".

    Trees get raw values (they do not care about scale); linear models get log1p of skewed
    quantities. Booleans become 0/1; categories become one-hot columns over a fixed vocabulary.
    """
    if kind not in ("linear", "tree"):
        raise ValueError("kind must be 'linear' or 'tree'")
    missing = [n for n in feature_names() if n not in df.columns]
    if missing:
        raise ValueError(f"missing feature columns: {', '.join(missing)}")
    cols: list[pl.Expr] = []
    for f in FEATURES:
        c = pl.col(f.name)
        if f.dtype == "category":
            cols += [
                (c == v).fill_null(False).cast(pl.Float64).alias(f"{f.name}={v}")
                for v in CATEGORIES[f.name]
            ]
        elif f.name in CYCLIC:
            angle = c.cast(pl.Float64) * (2 * np.pi / CYCLIC[f.name])
            cols += [angle.sin().alias(f"{f.name}_sin"), angle.cos().alias(f"{f.name}_cos")]
        elif f.name == "amount_log":
            if kind == "tree":
                cols.append(c.cast(pl.Float64))
        elif f.dtype == "bool":
            cols.append(c.cast(pl.Float64))
        elif kind == "linear" and f.name in LOG1P:
            cols.append(c.cast(pl.Float64).log1p())
        else:
            cols.append(c.cast(pl.Float64))
    return df.select(cols).to_numpy().astype(np.float64)
