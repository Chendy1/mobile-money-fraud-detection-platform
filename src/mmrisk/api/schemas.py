"""Request and response shapes for the scoring API.

Allowed values come from the same constants the ingestion rules use, so a transaction the API
accepts is one the data pipeline would also accept.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator

from mmrisk.generation.reference import COUNTY_NAMES
from mmrisk.validation import config as cfg

ID_PATTERN = r"^[A-Za-z0-9_-]{1,64}$"

TransactionType = Literal[*cfg.TRANSACTION_TYPES]
Channel = Literal[*cfg.CHANNELS]
County = Literal[*COUNTY_NAMES]
CounterpartyType = Literal["CUSTOMER", "MERCHANT", "AGENT"]


class TransactionIn(BaseModel):
    """One transaction to score. Unknown fields are refused, not ignored."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    transaction_id: str = Field(pattern=ID_PATTERN)
    customer_id: str = Field(pattern=ID_PATTERN)
    timestamp: datetime = Field(
        description="Naive local East Africa Time, e.g. 2025-06-01T14:30:00"
    )
    transaction_type: TransactionType
    amount_kes: StrictInt = Field(gt=0, le=cfg.MAX_AMOUNT_KES)
    counterparty_type: CounterpartyType | None = None
    counterparty_id: str | None = Field(default=None, pattern=ID_PATTERN)
    device_id: str = Field(pattern=ID_PATTERN)
    location_county: County
    channel: Channel

    @model_validator(mode="after")
    def _check(self):
        if self.timestamp.tzinfo is not None:
            raise ValueError("timestamp must not carry a timezone (use local East Africa Time)")
        if self.timestamp < cfg.PLAUSIBLE_AFTER:
            raise ValueError("timestamp is implausibly old")
        expected = cfg.COUNTERPARTY_FOR_TYPE.get(self.transaction_type)
        if expected is None:  # AIRTIME has no counterparty
            if self.counterparty_type is not None or self.counterparty_id is not None:
                raise ValueError(f"{self.transaction_type} must not have a counterparty")
        elif self.counterparty_type != expected or self.counterparty_id is None:
            raise ValueError(
                f"{self.transaction_type} needs counterparty_type {expected} and a counterparty_id"
            )
        return self


class Reason(BaseModel):
    rank: int
    feature: str
    effect: float = Field(description="Log-odds the feature added to the calibrated score")
    text: str


class ExpectedCosts(BaseModel):
    """Expected cost in KES of each action. The cost parameters are ASSUMED, not measured."""

    allow: float
    review: float
    block: float


class ScoreOut(BaseModel):
    transaction_id: str
    risk_probability: float
    action: Literal["ALLOW", "REVIEW", "BLOCK"]
    risk_level: Literal["LOW", "MEDIUM", "HIGH"]
    expected_cost_assumed_kes: ExpectedCosts
    reasons: list[Reason] = Field(
        description="Why the score is high; empty when the action is ALLOW. Describes the model, "
        "not the cause."
    )
    feature_version: str
    duplicate: bool = Field(default=False, description="True when this id was already scored")


class ModelInfo(BaseModel):
    scorer: str
    feature_version: str
    calibrator_link: str
    cost_model_ASSUMED: dict
    customers: int
    history_transactions: int
    synthetic_data_only: bool = True


class ErrorBody(BaseModel):
    code: str
    message: str


class ErrorOut(BaseModel):
    error: ErrorBody
