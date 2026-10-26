"""Every threshold in the retraining step. All are ASSUMED values for this simulation.

They are conventions chosen to be sensible for the size of the generated data, not numbers
measured from a real mobile-money operation. A real deployment would set them from the cost of a
bad model versus the cost of delay and would revisit them as data accumulates.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Triggers:
    max_model_age_days: float = 90.0  # ASSUMED: retrain at least about every quarter
    min_new_fraud_labels: int = (
        200  # ASSUMED: fraud labels on transactions newer than the champion's data
    )


@dataclass(frozen=True)
class Gates:
    """Why the two margins are as wide as they are: on the generated year, two models that agree
    on 98.6% of actions (same data, different tuning) still differed by an interval of about
    [-0.02, +0.03] in PR-AUC and [-3,700, +1,100] KES per 10,000 in cost, because the 45-day test
    window holds only about a hundred frauds. A margin tighter than that noise would reject a
    candidate that is no worse. The values were set after seeing that one comparison."""

    min_train_fraud: int = 100  # ASSUMED
    min_val_fraud: int = 30  # ASSUMED
    min_test_fraud: int = 30  # ASSUMED
    pr_auc_margin: float = 0.03  # ASSUMED: lower 95% bound of (candidate - champion) >= -margin
    cost_margin: float = 0.05  # ASSUMED: upper 95% bound of the cost difference <= 5% of champion
    ece_margin: float = 0.005  # ASSUMED: candidate ECE at most this much above the champion's
    flagged_ratio_min: float = 0.5  # ASSUMED: share of non-ALLOW actions vs the champion
    flagged_ratio_max: float = 2.0  # ASSUMED
    min_agreement: float = 0.70  # ASSUMED, advisory only: same action as the champion
    n_boot: int = 300
