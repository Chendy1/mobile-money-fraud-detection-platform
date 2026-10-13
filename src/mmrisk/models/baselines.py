"""Baselines that need no learning: a constant score and a hand-written rule engine."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import polars as pl


@dataclass(frozen=True)
class Rule:
    name: str
    description: str
    expr: pl.Expr


# Fixed thresholds taken from the exploratory analysis, never fitted or tuned on any split.
RULES: tuple[Rule, ...] = (
    Rule("night", "Between 00:00 and 04:59", pl.col("is_night")),
    Rule("new_device", "First use of this device", pl.col("is_new_device")),
    Rule("new_recipient", "First transfer to this recipient", pl.col("is_new_recipient")),
    Rule(
        "large_vs_baseline",
        "Amount above 5x the 30-day average",
        (pl.col("amount_to_avg_30d") > 5).fill_null(False),
    ),
    Rule(
        "rapid_repeat",
        "Within 2 minutes of the previous transaction",
        (pl.col("secs_since_prev_txn") <= 120).fill_null(False),
    ),
    Rule("burst", "3 or more transactions in 10 minutes", pl.col("txn_count_10m") >= 3),
    Rule("location_jump", "County change within 1 hour", pl.col("location_jump_1h")),
    Rule(
        "cashout_after_inbound",
        "Agent withdrawal within 2 hours of a transfer in",
        pl.col("cashout_after_inbound"),
    ),
)


class PriorBaseline:
    """Every transaction gets the same score: the no-skill reference (PR-AUC = fraud rate)."""

    name = "no_skill"

    def score(self, df: pl.DataFrame) -> np.ndarray:
        return np.zeros(df.height)


class RuleBaseline:
    """Score = number of rules that fire. Ties are common; the metrics handle them exactly."""

    name = "rules"

    def hits(self, df: pl.DataFrame) -> pl.DataFrame:
        return df.select([r.expr.cast(pl.Int64).alias(r.name) for r in RULES])

    def score(self, df: pl.DataFrame) -> np.ndarray:
        return self.hits(df).sum_horizontal().to_numpy().astype(np.float64)

    def rule_table(self, df: pl.DataFrame) -> pl.DataFrame:
        """Per-rule firing rate, precision and recall on `df` (needs label_is_fraud)."""
        hits = self.hits(df)
        y = df["label_is_fraud"].to_numpy()
        total = max(1, int(y.sum()))
        rows = []
        for r in RULES:
            h = hits[r.name].to_numpy().astype(bool)
            fired = int(h.sum())
            caught = int((h & (y == 1)).sum())
            rows.append(
                {
                    "rule": r.name,
                    "description": r.description,
                    "fires_on": fired / df.height,
                    "precision": caught / fired if fired else None,
                    "recall": caught / total,
                }
            )
        any_h = hits.sum_horizontal().to_numpy() > 0
        fired = int(any_h.sum())
        caught = int((any_h & (y == 1)).sum())
        rows.append(
            {
                "rule": "any_rule",
                "description": "At least one rule fires",
                "fires_on": fired / df.height,
                "precision": caught / fired if fired else None,
                "recall": caught / total,
            }
        )
        return pl.DataFrame(rows)
