"""Batch feature computation. The single definition of every feature.

Rules that make it point-in-time safe:
- A row's features use only rows that come strictly *before* it in (timestamp, transaction_id)
  order. Time windows use `closed="left"`, so they never include the row itself or any row with
  the same timestamp.
- Nothing here reads labels or ground truth, and no statistic is computed over the whole frame.
- The result for a row does not depend on rows that come after it (tested by truncation).

The online path calls this same function, so training and serving cannot drift apart.
"""

from __future__ import annotations

import warnings

import polars as pl

from mmrisk.features.registry import (
    CUSTOMER_COLUMNS,
    INPUT_COLUMNS,
    KEY_COLUMNS,
    feature_names,
)

ORDER = ["timestamp", "transaction_id"]
CASHOUT_WINDOW_SECONDS = 2 * 3600
LOCATION_JUMP_SECONDS = 3600
MIN_BASELINE_TXNS = 3


class FeatureInputError(ValueError):
    """The input frames lack required columns or hold duplicate keys."""


def _require(df: pl.DataFrame, columns: tuple[str, ...], name: str) -> None:
    missing = [c for c in columns if c not in df.columns]
    if missing:
        raise FeatureInputError(f"{name} is missing columns: {', '.join(missing)}")


def inbound_events(txns: pl.DataFrame) -> pl.DataFrame:
    """Transfers between customers, seen from the recipient's side."""
    return (
        txns.filter(
            (pl.col("transaction_type") == "SEND_MONEY")
            & (pl.col("counterparty_type") == "CUSTOMER")
        )
        .select(pl.col("counterparty_id").alias("recipient"), pl.col("timestamp").alias("in_ts"))
        .sort("in_ts")
    )


def compute_features(
    txns: pl.DataFrame, customers: pl.DataFrame, inbound: pl.DataFrame | None = None
) -> pl.DataFrame:
    """Features for every row of `txns`, ordered by (timestamp, transaction_id).

    `inbound` (recipient, in_ts) defaults to the transfers found in `txns`; the online path passes
    the events it has seen for one customer.
    """
    _require(txns, INPUT_COLUMNS, "transactions")
    _require(customers, CUSTOMER_COLUMNS, "customers")
    if txns["transaction_id"].n_unique() != txns.height:
        raise FeatureInputError("transaction_id must be unique")
    if customers["customer_id"].n_unique() != customers.height:
        raise FeatureInputError("customer_id must be unique in customers")

    inb = inbound_events(txns) if inbound is None else inbound.sort("in_ts")
    cust = customers.select(CUSTOMER_COLUMNS)
    df = txns.select(INPUT_COLUMNS).sort(ORDER)

    # Last inbound transfer strictly before this transaction (other customers' history).
    # "Strictly before" is "at or before one microsecond earlier"; this avoids the
    # `allow_exact_matches` argument, which older Polars versions do not have.
    df = df.with_columns(_asof_ts=pl.col("timestamp") - pl.duration(microseconds=1))
    with warnings.catch_warnings():
        # Both sides are sorted by time above; Polars cannot verify that when `by` is given.
        warnings.filterwarnings("ignore", message="Sortedness of columns cannot be checked")
        df = df.join_asof(
            inb,
            left_on="_asof_ts",
            right_on="in_ts",
            by_left="customer_id",
            by_right="recipient",
            strategy="backward",
        ).drop("_asof_ts")
    df = df.join(cust, on="customer_id", how="left")

    df = df.sort(["customer_id", *ORDER]).with_columns(_one=pl.lit(1.0))
    one = pl.col("_one")
    amt = pl.col("amount_kes").cast(pl.Float64)

    def window(expr: pl.Expr, size: str) -> pl.Expr:
        return expr.rolling_sum_by("timestamp", size, closed="left").over("customer_id")

    by_cust = "customer_id"
    df = df.with_columns(
        n_prior_txns=pl.int_range(pl.len()).over(by_cust).cast(pl.Int64),
        secs_since_prev_txn=(pl.col("timestamp") - pl.col("timestamp").shift(1).over(by_cust))
        .dt.total_microseconds()
        .cast(pl.Float64)
        / 1e6,
        prev_county=pl.col("location_county").shift(1).over(by_cust),
        device_seq=pl.int_range(pl.len()).over([by_cust, "device_id"]),
        recipient_seq=pl.int_range(pl.len()).over([by_cust, "counterparty_id"]),
        txn_count_10m=window(one, "10m"),
        txn_count_1h=window(one, "1h"),
        txn_count_24h=window(one, "24h"),
        txn_count_7d=window(one, "7d"),
        txn_count_30d=window(one, "30d"),
        amount_sum_1h=window(amt, "1h"),
        amount_sum_24h=window(amt, "24h"),
        sum_30d=window(amt, "30d"),
    )
    df = df.with_columns(
        pl.col("txn_count_10m", "txn_count_1h", "txn_count_24h", "txn_count_7d", "txn_count_30d")
        .fill_null(0.0)
        .cast(pl.Int64),
        pl.col("amount_sum_1h", "amount_sum_24h").fill_null(0.0),
    )
    has_history = pl.col("n_prior_txns") > 0
    since_inbound = (pl.col("timestamp") - pl.col("in_ts")).dt.total_microseconds().cast(
        pl.Float64
    ) / 1e6
    out = df.with_columns(
        amount_log=(amt + 1.0).log(),
        hour=pl.col("timestamp").dt.hour().cast(pl.Int64),
        day_of_week=pl.col("timestamp").dt.weekday().cast(pl.Int64),
        is_night=pl.col("timestamp").dt.hour() < 5,
        is_salary_window=(pl.col("timestamp").dt.day() >= 25) | (pl.col("timestamp").dt.day() <= 3),
        account_age_days=(pl.col("timestamp").dt.date() - pl.col("registration_date"))
        .dt.total_days()
        .cast(pl.Int64),
        is_away_from_home=pl.col("location_county") != pl.col("home_county"),
        avg_amount_30d=pl.when(pl.col("txn_count_30d") > 0)
        .then(pl.col("sum_30d") / pl.col("txn_count_30d"))
        .otherwise(None),
        is_new_device=(pl.col("device_seq") == 0) & has_history,
        is_new_recipient=(pl.col("transaction_type") == "SEND_MONEY")
        & (pl.col("recipient_seq") == 0)
        & has_history,
        county_changed=(pl.col("prev_county").is_not_null())
        & (pl.col("location_county") != pl.col("prev_county")),
        secs_since_last_inbound=since_inbound,
    ).with_columns(
        amount_to_avg_30d=pl.when(pl.col("txn_count_30d") >= MIN_BASELINE_TXNS)
        .then(amt / pl.col("avg_amount_30d"))
        .otherwise(None),
        location_jump_1h=pl.col("county_changed")
        & (pl.col("secs_since_prev_txn") < LOCATION_JUMP_SECONDS),
        cashout_after_inbound=(pl.col("transaction_type") == "AGENT_WITHDRAWAL")
        & (pl.col("secs_since_last_inbound") <= CASHOUT_WINDOW_SECONDS),
    )
    out = out.with_columns(
        pl.col(
            "is_new_device", "is_new_recipient", "location_jump_1h", "cashout_after_inbound"
        ).fill_null(False)
    )
    return out.select(*KEY_COLUMNS, *feature_names()).sort(ORDER)
