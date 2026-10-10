"""The feature catalogue: one entry per feature, with its meaning and what it may look at.

`history` says what information a feature is allowed to use:
  none      the transaction itself (and static customer attributes)
  customer  the same customer's strictly earlier transactions
  network   other customers' strictly earlier transactions (inbound transfers)
"""

from __future__ import annotations

from dataclasses import dataclass

FEATURE_VERSION = "1.0"


@dataclass(frozen=True)
class Feature:
    name: str
    dtype: str  # "float", "int", "bool" or "category"
    group: str
    history: str  # none | customer | network
    description: str


FEATURES: tuple[Feature, ...] = (
    # --- the transaction itself ---------------------------------------------------------
    Feature("amount_kes", "int", "transaction", "none", "Amount in KES."),
    Feature("amount_log", "float", "transaction", "none", "natural log of (1 + amount)."),
    Feature("transaction_type", "category", "transaction", "none", "SEND_MONEY, PAYBILL, ..."),
    Feature("channel", "category", "transaction", "none", "USSD or APP."),
    Feature("hour", "int", "time", "none", "Hour of day, 0 to 23 (local time)."),
    Feature("day_of_week", "int", "time", "none", "Monday = 1 to Sunday = 7."),
    Feature("is_night", "bool", "time", "none", "Hour is 00 to 04."),
    Feature("is_salary_window", "bool", "time", "none", "Day of month is 25 to 31 or 1 to 3."),
    # --- static customer attributes -----------------------------------------------------
    Feature("segment", "category", "customer", "none", "Customer segment."),
    Feature("account_age_days", "int", "customer", "none", "Days since registration."),
    Feature("is_away_from_home", "bool", "customer", "none", "County differs from home county."),
    # --- the customer's own earlier activity --------------------------------------------
    Feature("n_prior_txns", "int", "history", "customer", "Earlier transactions by the customer."),
    Feature(
        "secs_since_prev_txn",
        "float",
        "history",
        "customer",
        "Seconds since the customer's previous transaction; null for the first.",
    ),
    Feature(
        "txn_count_10m",
        "int",
        "velocity",
        "customer",
        "Earlier transactions in the last 10 minutes.",
    ),
    Feature(
        "txn_count_1h", "int", "velocity", "customer", "Earlier transactions in the last hour."
    ),
    Feature(
        "txn_count_24h", "int", "velocity", "customer", "Earlier transactions in the last 24 hours."
    ),
    Feature(
        "txn_count_7d", "int", "velocity", "customer", "Earlier transactions in the last 7 days."
    ),
    Feature(
        "amount_sum_1h", "float", "velocity", "customer", "Sum of earlier amounts in the last hour."
    ),
    Feature(
        "amount_sum_24h",
        "float",
        "velocity",
        "customer",
        "Sum of earlier amounts in the last 24 hours.",
    ),
    Feature(
        "txn_count_30d", "int", "baseline", "customer", "Earlier transactions in the last 30 days."
    ),
    Feature(
        "avg_amount_30d",
        "float",
        "baseline",
        "customer",
        "Mean earlier amount in the last 30 days; null if none.",
    ),
    Feature(
        "amount_to_avg_30d",
        "float",
        "baseline",
        "customer",
        "Amount divided by avg_amount_30d; null unless at least 3 earlier transactions in 30 days.",
    ),
    Feature(
        "is_new_device",
        "bool",
        "novelty",
        "customer",
        "First time the customer uses this device (and the customer has earlier history).",
    ),
    Feature(
        "is_new_recipient",
        "bool",
        "novelty",
        "customer",
        "SEND_MONEY to a recipient the customer never paid before (and has earlier history).",
    ),
    Feature(
        "county_changed",
        "bool",
        "novelty",
        "customer",
        "County differs from the previous transaction.",
    ),
    Feature(
        "location_jump_1h",
        "bool",
        "novelty",
        "customer",
        "County changed and the previous transaction was under an hour ago.",
    ),
    # --- other customers' earlier activity (money flowing in) ---------------------------
    Feature(
        "secs_since_last_inbound",
        "float",
        "network",
        "network",
        "Seconds since someone last sent this customer money; null if never.",
    ),
    Feature(
        "cashout_after_inbound",
        "bool",
        "network",
        "network",
        "AGENT_WITHDRAWAL within 2 hours of receiving a transfer.",
    ),
)

INPUT_COLUMNS = (
    "transaction_id", "customer_id", "timestamp", "transaction_type", "amount_kes",
    "counterparty_type", "counterparty_id", "device_id", "location_county", "channel",
)  # fmt: skip
CUSTOMER_COLUMNS = ("customer_id", "segment", "home_county", "registration_date")
KEY_COLUMNS = ("transaction_id", "customer_id", "timestamp")

# Deliberately NOT features (see ADR-011):
EXCLUDED = {
    "device_id, counterparty_id, customer_id": (
        "identifiers: high cardinality and no meaning across customers"
    ),
    "merchant or agent fraud rate": (
        "target encoding: leaks labels unless built with strict time cut-offs"
    ),
    "labels, label_available_at, ground truth": "the target, never an input",
    "global amount percentile": (
        "depends on future transactions (a quantile over the whole file)"
    ),
}  # fmt: skip


def feature_names(dtypes: tuple[str, ...] | None = None) -> list[str]:
    return [f.name for f in FEATURES if dtypes is None or f.dtype in dtypes]
