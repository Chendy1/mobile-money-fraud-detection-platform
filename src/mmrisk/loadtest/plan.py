"""What to send and when: load profiles turned into a list of requests with intended times.

A profile is a list of segments (start, end, rate). Arrivals are evenly spaced or Poisson (seeded).
A transaction goes to a worker chosen by its customer, so one customer's transactions are sent in
order by one worker: the API refuses a customer's transaction that is older than the last one.
"""

from __future__ import annotations

import math
import random
import zlib
from dataclasses import dataclass

PROFILES = ("constant", "step", "ramp", "spike")
ARRIVALS = ("even", "poisson")
MAX_RATE = (
    2000.0  # requests per second: a guard against typing a rate that floods a laptop (ASSUMED)
)
MAX_SECONDS = 900.0  # one profile may last at most this long (ASSUMED)


class PlanError(ValueError):
    """The profile or its numbers cannot be used."""


@dataclass(frozen=True)
class Segment:
    start: float
    end: float
    rate: float  # requests per second offered in this segment

    @property
    def seconds(self) -> float:
        return self.end - self.start


@dataclass(frozen=True)
class Request:
    index: int
    offset: float  # seconds after the run starts at which this request is meant to start
    segment: int
    worker: int
    txn: dict


def _check_rate(rate: float, what: str = "rate") -> float:
    if not (rate > 0) or math.isnan(rate):
        raise PlanError(f"{what} must be above 0")
    if rate > MAX_RATE:
        raise PlanError(f"{what} {rate:g} is above the limit of {MAX_RATE:g} requests per second")
    return float(rate)


def _check_seconds(seconds: float, what: str) -> float:
    if not (seconds >= 1) or math.isnan(seconds):
        raise PlanError(f"{what} must be at least 1 second")
    return float(seconds)


def segments_for(
    profile: str,
    *,
    rate: float = 0.0,
    rates: tuple[float, ...] = (),
    seconds: float = 0.0,
    step_seconds: float = 0.0,
    peak: float = 0.0,
) -> list[Segment]:
    if profile not in PROFILES:
        raise PlanError(f"profile must be one of {', '.join(PROFILES)}")
    if profile == "constant":
        r, s = _check_rate(rate), _check_seconds(seconds, "seconds")
        out = [Segment(0.0, s, r)]
    elif profile == "step":
        if len(rates) < 2:
            raise PlanError("a step profile needs at least two rates")
        if list(rates) != sorted(set(rates)):
            raise PlanError("step rates must increase")
        s = _check_seconds(step_seconds, "step seconds")
        out = [Segment(i * s, (i + 1) * s, _check_rate(r)) for i, r in enumerate(rates)]
    elif profile == "ramp":
        lo, hi = _check_rate(rate, "start rate"), _check_rate(peak, "end rate")
        if hi <= lo:
            raise PlanError("a ramp must end above where it starts")
        total = _check_seconds(seconds, "seconds")
        n = max(2, int(total))
        out = [
            Segment(i * total / n, (i + 1) * total / n, lo + (hi - lo) * i / (n - 1))
            for i in range(n)
        ]
    else:  # spike: base, then peak, then base again
        lo, hi = _check_rate(rate, "base rate"), _check_rate(peak, "peak rate")
        if hi <= lo:
            raise PlanError("the peak must be above the base rate")
        s = _check_seconds(step_seconds, "step seconds")
        out = [Segment(0.0, s, lo), Segment(s, 2 * s, hi), Segment(2 * s, 3 * s, lo)]
    if out[-1].end > MAX_SECONDS:
        raise PlanError(f"a profile may last at most {MAX_SECONDS:g} seconds")
    return out


def offsets(
    segments: list[Segment], arrivals: str = "even", seed: int = 42
) -> list[tuple[float, int]]:
    """(offset, segment index) for every request, in time order."""
    if arrivals not in ARRIVALS:
        raise PlanError(f"arrivals must be one of {', '.join(ARRIVALS)}")
    rng = random.Random(seed)
    out: list[tuple[float, int]] = []
    for i, seg in enumerate(segments):
        if arrivals == "even":
            n = int(round(seg.seconds * seg.rate))
            out += [(seg.start + k / seg.rate, i) for k in range(n)]
        else:
            t = seg.start + rng.expovariate(seg.rate)
            while t < seg.end:
                out.append((t, i))
                t += rng.expovariate(seg.rate)
    return out


def worker_for(customer_id: str, workers: int) -> int:
    return zlib.crc32(customer_id.encode()) % workers


def build(
    txns: list[dict], segments: list[Segment], workers: int, arrivals: str = "even", seed: int = 42
) -> list[Request]:
    if workers < 1:
        raise PlanError("workers must be at least 1")
    times = offsets(segments, arrivals, seed)
    if len(times) > len(txns):
        raise PlanError(
            f"the profile needs {len(times)} transactions and the stream has {len(txns)}; "
            "lower the rate or the time, or start the stream earlier (--last-days)"
        )
    return [
        Request(i, t, seg, worker_for(txns[i]["customer_id"], workers), txns[i])
        for i, (t, seg) in enumerate(times)
    ]
