"""The simulation itself: a producer thread, a bounded queue, a reorder buffer and a scorer.

    rows -> iter_events (faults) -> [producer thread] -> bounded queue -> [consumer]
         -> reorder buffer -> scorer (local or HTTP) -> prediction store + counters

The queue is bounded, so a slow scorer slows the producer down instead of growing memory (the
time the producer spent waiting is reported). Scoring happens in one consumer, in order, because
the scoring service needs each customer's transactions in time order and takes a lock anyway.
With no faults the result is identical to batch scoring; the tests check that.
"""

from __future__ import annotations

import queue
import threading
import time
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime

import numpy as np

from mmrisk.streaming.buffer import ReorderBuffer
from mmrisk.streaming.events import KINDS, FaultConfig, iter_events
from mmrisk.streaming.scorers import SCORED, STATUSES, UNAVAILABLE
from mmrisk.streaming.store import PredictionStore

_END = object()


class StreamError(RuntimeError):
    """The run could not continue (the service went away, or the source failed)."""


@dataclass(frozen=True)
class StreamConfig:
    speed: float = 0.0  # simulated seconds per real second; 0 = as fast as possible
    queue_size: int = 1000
    max_lateness: float = 60.0  # seconds of event time an event may arrive late and still be placed
    max_unavailable: int = 20  # consecutive failed calls before the run gives up
    faults: FaultConfig = field(default_factory=FaultConfig)

    def __post_init__(self):
        if self.speed < 0 or self.max_lateness < 0:
            raise StreamError("speed and max_lateness must not be negative")
        if self.queue_size < 1 or self.max_unavailable < 1:
            raise StreamError("queue_size and max_unavailable must be at least 1")


def _utc_now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def run_stream(
    rows: Iterable[dict],
    scorer,
    cfg: StreamConfig | None = None,
    store: PredictionStore | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], datetime] = _utc_now,
) -> dict:
    """Stream `rows` (in time order) through `scorer`; return a report of what happened."""
    cfg = cfg or StreamConfig()
    q: queue.Queue = queue.Queue(maxsize=cfg.queue_size)
    stop = threading.Event()
    producer_error: list[BaseException] = []
    generated: Counter = Counter()
    gauge = {"blocked": 0.0, "max_depth": 0}

    def put(item) -> None:
        t0 = clock()
        while not stop.is_set():
            try:
                q.put(item, timeout=0.05)
                gauge["max_depth"] = max(gauge["max_depth"], q.qsize())
                break
            except queue.Full:
                pass
        gauge["blocked"] += clock() - t0  # time inside put is time spent waiting for room

    def produce() -> None:
        try:
            start = clock()
            for event in iter_events(rows, cfg.faults):
                if stop.is_set():
                    return
                if cfg.speed > 0:
                    wait = start + event.arrival / cfg.speed - clock()
                    if wait > 0:
                        sleep(wait)
                generated[event.kind] += 1
                put(event)
        except BaseException as exc:  # reported by the consumer, never lost
            producer_error.append(exc)
        finally:
            put(_END)

    outcomes: Counter = Counter()
    actions: Counter = Counter()
    dropped: Counter = Counter()
    millis: list[float] = []
    seen = {"in": 0, "first": None, "last": None, "run": 0}
    buffer = ReorderBuffer(cfg.max_lateness)

    def process(event) -> None:
        out = scorer.score(event.txn)
        outcomes[out.status] += 1
        if out.status == UNAVAILABLE:
            seen["run"] += 1
            if seen["run"] >= cfg.max_unavailable:
                raise StreamError(
                    f"the scoring service did not answer {seen['run']} times in a row; stopping"
                )
            return
        seen["run"] = 0
        millis.append(out.millis)
        if out.status == SCORED:
            actions[out.response["action"]] += 1
            if store is not None:
                store.add(event.seq, event.txn, out.response, now())

    producer = threading.Thread(target=produce, name="producer", daemon=True)
    t_start = clock()
    producer.start()
    try:
        while True:
            item = q.get()
            if item is _END:
                break
            seen["in"] += 1
            t = item.event_time
            seen["first"] = t if seen["first"] is None or t < seen["first"] else seen["first"]
            seen["last"] = t if seen["last"] is None or t > seen["last"] else seen["last"]
            released, late = buffer.push(item)
            for _, reason in late:
                dropped[reason] += 1
            for event in released:
                process(event)
        for event in buffer.flush():
            process(event)
    finally:
        stop.set()
        while True:  # unblock a producer that is waiting on a full queue
            try:
                q.get_nowait()
            except queue.Empty:
                break
        producer.join(timeout=10)
        if store is not None:
            store.close()
    if producer_error:
        raise StreamError(f"the event source failed: {producer_error[0]!r}") from producer_error[0]
    wall = clock() - t_start

    scored = outcomes[SCORED]
    flagged = actions["REVIEW"] + actions["BLOCK"]
    lat = np.percentile(millis, [50, 95, 99]) if millis else [0.0, 0.0, 0.0]
    span = (seen["last"] - seen["first"]).total_seconds() if seen["first"] else 0.0
    accounted = sum(outcomes.values()) + sum(dropped.values())
    return {
        "scorer": scorer.name,
        "config": {
            "speed": cfg.speed,
            "queue_size": cfg.queue_size,
            "max_lateness_seconds": cfg.max_lateness,
            "faults": asdict(cfg.faults),
        },
        "events": {"in": seen["in"], "by_kind": {k: generated[k] for k in KINDS}},
        "buffer": {
            "max_waiting": buffer.max_waiting,
            "dropped_duplicate": dropped["duplicate"],
            "dropped_late": dropped["late"],
        },
        "outcomes": {s: outcomes[s] for s in STATUSES},
        "actions": {a: actions[a] for a in ("ALLOW", "REVIEW", "BLOCK")},
        "flagged_share": (flagged / scored) if scored else 0.0,
        "latency_ms": {
            "p50": float(lat[0]),
            "p95": float(lat[1]),
            "p99": float(lat[2]),
            "max": float(max(millis)) if millis else 0.0,
        },  # fmt: skip
        "queue": {
            "capacity": cfg.queue_size,
            "max_depth": gauge["max_depth"],
            "producer_blocked_seconds": round(gauge["blocked"], 3),
        },
        "timing": {
            "wall_seconds": round(wall, 3),
            "scored_per_second": (scored / wall) if wall > 0 else 0.0,
            "simulated_seconds": span,
        },
        "stream_from": seen["first"].isoformat() if seen["first"] else None,
        "stream_to": seen["last"].isoformat() if seen["last"] else None,
        "balanced": accounted == seen["in"],  # every event is scored, rejected or dropped
    }
