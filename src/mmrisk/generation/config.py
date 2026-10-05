"""Generator configuration and documented modelling assumptions."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path

# ---- Modelling assumptions (synthetic; NOT verified real-world rules) -------------------------
MAX_TXN_KES = 250_000  # ASSUMPTION (TO VERIFY): single-transaction cap
DISPUTE_WINDOW_DAYS = 45  # ASSUMPTION: legit transactions are "confirmed" after this window
DETECTION_RATE = 0.88  # ASSUMPTION: share of true fraud that is ever reported
FALSE_REPORT_RATE = 0.0005  # ASSUMPTION: legit transactions wrongly labelled fraud
MULE_SHARE = 0.02  # share of customers acting as mule accounts
STRUCTURING_LOW, STRUCTURING_HIGH = (
    9_500,
    9_990,
)  # ASSUMPTION: "just under 10,000" amounts


@dataclass(frozen=True)
class GeneratorConfig:
    num_customers: int = 5_000
    num_transactions: int = 200_000
    fraud_rate: float = 0.005
    seed: int = 42
    start_date: date = date(2025, 1, 1)
    end_date: date = date(2025, 12, 31)
    output_dir: Path = Path("data/raw")

    @property
    def num_days(self) -> int:
        return (self.end_date - self.start_date).days + 1

    @property
    def num_fraud(self) -> int:
        return int(round(self.fraud_rate * self.num_transactions))

    def validate(self) -> None:
        if self.num_customers < 20:
            raise ValueError("num_customers must be >= 20")
        if self.num_transactions < 500:
            raise ValueError("num_transactions must be >= 500")
        if not 0.0 <= self.fraud_rate <= 0.2:
            raise ValueError("fraud_rate must be between 0 and 0.2")
        if self.end_date < self.start_date:
            raise ValueError("end_date must not be before start_date")
        if self.num_days < 30:
            raise ValueError("the period must cover at least 30 days")
