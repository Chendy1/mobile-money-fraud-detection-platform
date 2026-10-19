"""Online feature state: scores one transaction at a time using the same code as the batch path.

For each customer it keeps the transactions seen so far and the transfers received. `features()`
builds a small frame from that history plus the new transaction and calls `compute_features`,
so batch and online results are identical by construction (and tested to be).

Limitation: the history is unbounded and held in memory. A production system would keep bounded
aggregates in a low-latency store; the simulation does not need that.
"""

from __future__ import annotations

from datetime import datetime

import polars as pl

from mmrisk.features.compute import compute_features
from mmrisk.features.registry import CUSTOMER_COLUMNS, INPUT_COLUMNS, feature_names

_SCHEMA = {
    "transaction_id": pl.Utf8,
    "customer_id": pl.Utf8,
    "timestamp": pl.Datetime("us"),
    "transaction_type": pl.Utf8,
    "amount_kes": pl.Int64,
    "counterparty_type": pl.Utf8,
    "counterparty_id": pl.Utf8,
    "device_id": pl.Utf8,
    "location_county": pl.Utf8,
    "channel": pl.Utf8,
}


class OutOfOrderError(ValueError):
    """A transaction arrived for a customer that is not later than the last one seen."""


class OnlineFeatureState:
    def __init__(self, customers: pl.DataFrame):
        missing = [c for c in CUSTOMER_COLUMNS if c not in customers.columns]
        if missing:
            raise ValueError(f"customers is missing columns: {', '.join(missing)}")
        self._customers = {
            r["customer_id"]: r for r in customers.select(CUSTOMER_COLUMNS).to_dicts()
        }
        self._history: dict[str, list[dict]] = {}
        self._inbound: dict[str, list[datetime]] = {}

    def _check_order(self, txn: dict) -> None:
        past = self._history.get(txn["customer_id"])
        if past:
            last = past[-1]
            if (txn["timestamp"], txn["transaction_id"]) <= (
                last["timestamp"],
                last["transaction_id"],
            ):
                raise OutOfOrderError(
                    f"{txn['transaction_id']} is not later than {last['transaction_id']} "
                    f"for customer {txn['customer_id']}"
                )

    def features(self, txn: dict) -> dict:
        """Features for `txn`, from what has been observed so far. Does not record `txn`."""
        row = self.feature_frame(txn).row(0, named=True)
        return {name: row[name] for name in feature_names()}

    def feature_frame(self, txn: dict) -> pl.DataFrame:
        """The same features as a one-row frame with the batch path's column types.

        The key columns come first, so the frame can go straight to a model.
        """
        cid = txn["customer_id"]
        if cid not in self._customers:
            raise KeyError(f"unknown customer {cid}")
        self._check_order(txn)
        rows = [*self._history.get(cid, []), {k: txn[k] for k in INPUT_COLUMNS}]
        frame = pl.DataFrame(rows, schema=_SCHEMA)
        events = self._inbound.get(cid, [])
        inbound = pl.DataFrame(
            {"recipient": [cid] * len(events), "in_ts": events},
            schema={"recipient": pl.Utf8, "in_ts": pl.Datetime("us")},
        )
        customer = pl.DataFrame([self._customers[cid]])
        result = compute_features(frame, customer, inbound)
        return result.filter(pl.col("transaction_id") == txn["transaction_id"])

    def observe(self, txn: dict) -> None:
        """Record a transaction so later ones can see it (call after scoring it)."""
        self._check_order(txn)
        record = {k: txn[k] for k in INPUT_COLUMNS}
        self._history.setdefault(txn["customer_id"], []).append(record)
        if txn["transaction_type"] == "SEND_MONEY" and txn["counterparty_type"] == "CUSTOMER":
            self._inbound.setdefault(txn["counterparty_id"], []).append(txn["timestamp"])
