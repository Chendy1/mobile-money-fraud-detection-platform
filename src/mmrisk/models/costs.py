"""A cost model that turns a calibrated fraud probability into an action.

ALL COST NUMBERS BELOW ARE ASSUMPTIONS for this simulation. They are not measured from any
provider, and nothing in this project claims a real saving. They live in one place so they can be
changed, and the report shows how results move when they do.

Three actions, ordered by how much they get in the way of the customer:

    ALLOW   let the transaction through                          -> risk level LOW
    REVIEW  hold it for a verification or an analyst check       -> MEDIUM
    BLOCK   decline it (step-up authentication or refusal)       -> HIGH

For a transaction of `amount` KES whose fraud probability is `p`, the expected cost of each action:

    ALLOW   p * loss_rate * amount
    REVIEW  review_cost + p * (1 - review_prevents) * loss_rate * amount
    BLOCK   (1 - p) * block_cost

`loss_rate` is the share of the amount that is lost if fraud gets through (1.0 = no recovery).
`review_cost` is paid on every review. `review_prevents` is the share of fraud a review stops.
`block_cost` is the friction paid when a legitimate transaction is blocked (a blocked fraud stops
completely). The action with the lowest expected cost is chosen; ties go to the less severe action.
The decision needs `p` to be a real probability, which is why scores are calibrated first.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

import numpy as np

ACTIONS = ("ALLOW", "REVIEW", "BLOCK")
RISK_LEVELS = ("LOW", "MEDIUM", "HIGH")  # ALLOW, REVIEW, BLOCK


class CostModelError(ValueError):
    """A cost parameter is out of range."""


@dataclass(frozen=True)
class CostModel:
    loss_rate: float = 1.0  # ASSUMPTION: no recovery of fraudulent amounts
    review_cost: float = 25.0  # ASSUMPTION: KES per review (analyst time, verification message)
    review_prevents: float = 0.7  # ASSUMPTION: share of fraud a review stops
    block_cost: float = 150.0  # ASSUMPTION: KES friction per legitimate transaction blocked

    def __post_init__(self):
        if not 0 < self.loss_rate <= 1:
            raise CostModelError("loss_rate must be in (0, 1]")
        if not 0 <= self.review_prevents <= 1:
            raise CostModelError("review_prevents must be in [0, 1]")
        if self.review_cost < 0 or self.block_cost < 0:
            raise CostModelError("costs must not be negative")

    def scaled(self, factor: float) -> CostModel:
        """Both friction costs multiplied by `factor`; the fraud loss is unchanged."""
        return CostModel(
            self.loss_rate,
            self.review_cost * factor,
            self.review_prevents,
            self.block_cost * factor,
        )

    def as_dict(self) -> dict:
        return asdict(self)

    def expected_costs(self, p: np.ndarray, amount: np.ndarray) -> np.ndarray:
        """Array (n, 3): expected cost of ALLOW, REVIEW, BLOCK for each transaction."""
        p = np.asarray(p, dtype=float)
        loss = self.loss_rate * np.asarray(amount, dtype=float)
        allow = p * loss
        review = self.review_cost + p * (1 - self.review_prevents) * loss
        block = (1 - p) * self.block_cost
        return np.column_stack([allow, review, block])

    def decide(self, p: np.ndarray, amount: np.ndarray) -> np.ndarray:
        """Action index per transaction (0 ALLOW, 1 REVIEW, 2 BLOCK). Uses no labels."""
        return np.argmin(self.expected_costs(p, amount), axis=1)  # first minimum = least severe

    def realized_cost(self, action: np.ndarray, y: np.ndarray, amount: np.ndarray) -> np.ndarray:
        """Cost of each action given the label y (1 = fraud); a review stops a fixed share."""
        action = np.asarray(action)
        y = np.asarray(y, dtype=float)
        loss = self.loss_rate * np.asarray(amount, dtype=float)
        allow = y * loss
        review = self.review_cost + y * (1 - self.review_prevents) * loss
        block = (1 - y) * self.block_cost
        return np.choose(action, [allow, review, block])

    def allow_all_cost(self, y: np.ndarray, amount: np.ndarray) -> np.ndarray:
        return self.realized_cost(np.zeros(len(y), dtype=int), y, amount)

    def oracle_cost(self, y: np.ndarray, amount: np.ndarray) -> np.ndarray:
        """Cost if every fraud were blocked and nothing else touched (needs the labels)."""
        action = np.where(np.asarray(y) == 1, 2, 0)
        return self.realized_cost(action, y, amount)

    def probability_thresholds(self, amount: float) -> tuple[float | None, float | None]:
        """(p where REVIEW starts beating ALLOW, p where BLOCK starts beating the rest) at `amount`.

        Either is None if that action is never the cheapest for this amount.
        """
        grid = np.linspace(0, 1, 100_001)
        action = self.decide(grid, np.full_like(grid, float(amount)))

        def first(level: int) -> float | None:
            idx = np.flatnonzero(action >= level)
            return float(grid[idx[0]]) if idx.size else None

        return first(1), first(2)
