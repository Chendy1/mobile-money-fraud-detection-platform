"""Turning stored transactions into an event stream, with seeded delivery faults.

Events are produced in the order they *arrive*, which is not always the order they *happened*:
each event can be delayed (jitter), repeated (a retry), or accompanied by a corrupted copy. All
randomness comes from one seed, so a stream is exactly reproducible.
"""

from __future__ import annotations

import heapq
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta

import numpy as np
import polars as pl

from mmrisk.features.registry import INPUT_COLUMNS

KINDS = ("normal", "duplicate", "invalid")


class FaultConfigError(ValueError):
    """A fault parameter is out of range."""


@dataclass(frozen=True)
class FaultConfig:
    duplicate_rate: float = 0.0  # share of events that are delivered a second time
    bad_rate: float = 0.0  # share of events accompanied by a corrupted (amount 0) copy
    jitter_seconds: float = 0.0  # each event is delayed by a random 0..jitter seconds
    duplicate_delay_seconds: float = 30.0  # a repeat arrives up to this much later
    seed: int = 42

    def __post_init__(self):
        for name in ("duplicate_rate", "bad_rate"):
            if not 0 <= getattr(self, name) < 1:
                raise FaultConfigError(f"{name} must be in [0, 1)")
        if self.jitter_seconds < 0 or self.duplicate_delay_seconds < 0:
            raise FaultConfigError("delays must not be negative")


@dataclass(frozen=True)
class Event:
    seq: int  # position in arrival order
    arrival: float  # simulated seconds from the first event's time to this event's arrival
    txn: dict  # the ten input columns
    kind: str  # one of KINDS

    @property
    def event_time(self) -> datetime:
        return self.txn["timestamp"]

    @property
    def transaction_id(self) -> str:
        return self.txn["transaction_id"]


def iter_events(rows: Iterable[dict], faults: FaultConfig | None = None) -> Iterator[Event]:
    """Events in arrival order for `rows`, which must be in (timestamp, transaction_id) order.

    Without faults the arrival order is the input order. A row's later arrivals (its delay, its
    repeat, its corrupted copy) wait in a heap until no later row could arrive before them, so the
    whole stream is produced lazily and in arrival order.
    """
    f = faults or FaultConfig()
    rng = np.random.default_rng(f.seed)
    pending: list[tuple[float, int, dict, str]] = []
    made = 0
    emitted = 0
    t0: datetime | None = None
    last_t = float("-inf")

    def push(arrival: float, txn: dict, kind: str) -> None:
        nonlocal made
        heapq.heappush(pending, (arrival, made, txn, kind))
        made += 1

    try:
        for row in rows:
            ts = row["timestamp"]
            t0 = t0 or ts
            t = (ts - t0).total_seconds()
            if t < last_t:
                raise ValueError("rows must be in time order")
            last_t = t
            while pending and pending[0][0] <= t:
                arrival, _, txn, kind = heapq.heappop(pending)
                yield Event(emitted, arrival, txn, kind)
                emitted += 1
            delay, dup, bad, dup_delay = rng.random(4)
            base = t + (delay * f.jitter_seconds)
            push(base, row, "normal")
            if dup < f.duplicate_rate:
                push(base + dup_delay * f.duplicate_delay_seconds, dict(row), "duplicate")
            if bad < f.bad_rate:
                broken = {**row, "transaction_id": f"{row['transaction_id']}-BAD", "amount_kes": 0}
                push(base, broken, "invalid")
    except Exception:
        # a failing source still delivers what had already arrived, then reports the failure
        while pending:
            arrival, _, txn, kind = heapq.heappop(pending)
            yield Event(emitted, arrival, txn, kind)
            emitted += 1
        raise
    while pending:
        arrival, _, txn, kind = heapq.heappop(pending)
        yield Event(emitted, arrival, txn, kind)
        emitted += 1


def split_stream(
    txns: pl.DataFrame, start: datetime | None = None, last_days: float = 7.0
) -> tuple[pl.DataFrame, pl.DataFrame, datetime]:
    """(history, stream, start): transactions before `start` and from `start` on, in event order.

    `start` defaults to `last_days` before the newest transaction. The history is what the scoring
    service must already know; the stream is what is sent as events.
    """
    if txns.is_empty():
        raise ValueError("there are no transactions to stream")
    ordered = txns.sort("timestamp", "transaction_id")
    if start is None:
        start = ordered["timestamp"].max() - timedelta(days=last_days)
    ordered = ordered.select(INPUT_COLUMNS)  # the lake's lineage columns are not part of an event
    return (
        ordered.filter(pl.col("timestamp") < start),
        ordered.filter(pl.col("timestamp") >= start),
        start,
    )
