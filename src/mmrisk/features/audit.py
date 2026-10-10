"""Leak-smell audit: how well does each single feature rank fraud on its own?

A feature that separates the classes almost perfectly by itself usually means it contains the
label, or something computed from it. The audit reports the univariate AUC (Mann-Whitney) of every
numeric feature on the labelled rows. It does not prove a feature is clean; it catches the loud
mistakes.
"""

from __future__ import annotations

import polars as pl

from mmrisk.features.registry import FEATURES

SUSPICIOUS_AUC = 0.90  # distance from 0.5 above which a feature is flagged (AUC >= 0.9 or <= 0.1)


def univariate_auc(values: pl.Series, label: pl.Series) -> float | None:
    """AUC of `values` for predicting label == 1; nulls are ignored. None if undefined."""
    df = pl.DataFrame({"v": values.cast(pl.Float64), "y": label}).drop_nulls()
    n_pos = int(df["y"].sum())
    n_neg = df.height - n_pos
    if n_pos == 0 or n_neg == 0:
        return None
    ranked = df.with_columns(r=pl.col("v").rank("average"))
    rank_sum_pos = ranked.filter(pl.col("y") == 1)["r"].sum()
    return float((rank_sum_pos - n_pos * (n_pos + 1) / 2) / (n_pos * n_neg))


def audit(table: pl.DataFrame) -> pl.DataFrame:
    """One row per numeric/bool feature: AUC, null share and a suspicious flag."""
    rows = []
    label = table["label_is_fraud"]
    for f in FEATURES:
        if f.dtype == "category":
            continue
        col = table[f.name]
        auc = univariate_auc(col, label)
        rows.append(
            {
                "feature": f.name,
                "auc": auc,
                "strength": None if auc is None else abs(auc - 0.5) * 2,
                "null_share": col.null_count() / table.height,
                "suspicious": auc is not None
                and (auc >= SUSPICIOUS_AUC or auc <= 1 - SUSPICIOUS_AUC),
            }
        )
    return pl.DataFrame(rows).sort("strength", descending=True, nulls_last=True)
