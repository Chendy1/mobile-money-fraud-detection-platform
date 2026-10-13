"""Time-based train / validation / test split that respects when labels become known.

Timeline, counted back from the end of the data (E):

    matured_end = E - 45 days          labels after this are still incomplete
    test        = (matured_end - test_days, matured_end]
    validation  = (test_start - val_days, test_start]
    train_end   = validation_start - gap_days
    train       = transactions up to train_end, with the labels known at validation_start

Why a gap of 45 days: the model is "deployed" at validation_start. Fraud takes up to 45 days to be
reported and legitimate transactions are only confirmed after 45 days, so a transaction newer than
validation_start - 45 days has an incomplete label at that moment. Fraud episodes also run across
days; the gap keeps one episode from sitting on both sides of the split.

`naive=True` reproduces the common mistake (no gap, final labels for every training row) so the
report can show how much it inflates the scores. Never use it for real decisions.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

import polars as pl

from mmrisk.features.training import training_table
from mmrisk.generation.config import DISPUTE_WINDOW_DAYS


class SplitError(ValueError):
    """The data cannot be split as configured (too short, or a part has no fraud)."""


@dataclass(frozen=True)
class SplitConfig:
    test_days: int = 45
    val_days: int = 45
    gap_days: int = DISPUTE_WINDOW_DAYS
    naive: bool = False  # True = no gap and final labels (the mistake; for comparison only)


@dataclass(frozen=True)
class Split:
    train: pl.DataFrame
    val: pl.DataFrame
    test: pl.DataFrame
    bounds: dict  # train_end, val_start, test_start, matured_end, data_end

    def summary(self) -> pl.DataFrame:
        rows = []
        for name, df in (("train", self.train), ("validation", self.val), ("test", self.test)):
            rows.append(
                {
                    "part": name,
                    "rows": df.height,
                    "fraud": int(df["label_is_fraud"].sum()),
                    "fraud_rate": df["label_is_fraud"].mean(),
                    "first": df["timestamp"].min(),
                    "last": df["timestamp"].max(),
                    "customers": df["customer_id"].n_unique(),
                }
            )
        return pl.DataFrame(rows)


def _window(df: pl.DataFrame, after: datetime, upto: datetime) -> pl.DataFrame:
    return df.filter((pl.col("timestamp") > after) & (pl.col("timestamp") <= upto))


def make_split(
    features: pl.DataFrame, labels: pl.DataFrame, cfg: SplitConfig | None = None
) -> Split:
    cfg = cfg or SplitConfig()
    data_end = features["timestamp"].max()
    matured_end = data_end - timedelta(days=DISPUTE_WINDOW_DAYS)
    test_start = matured_end - timedelta(days=cfg.test_days)
    val_start = test_start - timedelta(days=cfg.val_days)
    gap = 0 if cfg.naive else cfg.gap_days
    train_end = val_start - timedelta(days=gap)

    final = training_table(features, labels, data_end)  # every label that eventually arrives
    if cfg.naive:
        train = final.filter(pl.col("timestamp") <= train_end)
    else:
        # Only what could have been known when the model is deployed (at val_start).
        train = training_table(features, labels, val_start).filter(pl.col("timestamp") <= train_end)
    val = _window(final, val_start, test_start)
    test = _window(final, test_start, matured_end)
    bounds = {
        "train_end": train_end,
        "val_start": val_start,
        "test_start": test_start,
        "matured_end": matured_end,
        "data_end": data_end,
        "gap_days": gap,
        "naive": cfg.naive,
    }
    for name, part in (("train", train), ("validation", val), ("test", test)):
        if part.is_empty():
            raise SplitError(f"{name} part is empty; the data is too short for this configuration")
        if part["label_is_fraud"].sum() < 1:
            raise SplitError(f"{name} part contains no fraud")
    return Split(train, val, test, bounds)
