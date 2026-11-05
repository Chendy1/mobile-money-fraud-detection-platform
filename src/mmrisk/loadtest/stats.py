"""Turning raw samples into percentiles, rates, error classes, an SLO verdict and the knee."""

from __future__ import annotations

import math
from dataclasses import dataclass

# Latency and error targets. ASSUMED: nobody has stated a requirement for this synthetic service.
SLO_P95_MS = 100.0
SLO_P99_MS = 250.0
SLO_MAX_ERROR_RATE = 0.01
SLO_MIN_ACHIEVED = 0.95  # of the offered rate
SATURATED_LAG_P95_MS = 250.0  # requests start this late (95th percentile): workers cannot keep up


@dataclass(frozen=True)
class Slo:
    p95_ms: float = SLO_P95_MS
    p99_ms: float = SLO_P99_MS
    max_error_rate: float = SLO_MAX_ERROR_RATE
    min_achieved: float = SLO_MIN_ACHIEVED


@dataclass(frozen=True)
class Sample:
    index: int
    segment: int
    intended: float  # seconds since the run started
    start: float  # when the request was actually sent
    end: float
    status: int  # HTTP status; 0 when no response came (timeout, connection refused)
    duplicate: bool = False


def classify(status: int) -> str:
    if 200 <= status < 300:
        return "ok"
    if status == 0:
        return "network"
    if status == 429:
        return "rate_limited"
    if status in (401, 403):
        return "auth"
    if status == 409:
        return "conflict"
    if 400 <= status < 500:
        return "client_error"
    return "server_error"


def percentile(values: list[float], q: float) -> float | None:
    """Nearest-rank percentile (q in 0..100); None for no values."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(q / 100 * len(ordered)))
    return ordered[min(rank, len(ordered)) - 1]


def _ms(values: list[float]) -> dict:
    return {
        "p50": percentile(values, 50), "p95": percentile(values, 95),
        "p99": percentile(values, 99), "max": max(values) if values else None,
    }  # fmt: skip


def summarize_segment(
    samples: list[Sample], seconds: float, planned: int, rate: float, slo: Slo, start: float = 0.0
) -> dict:
    errors: dict[str, int] = {}
    ok = [s for s in samples if classify(s.status) == "ok"]
    for s in samples:
        c = classify(s.status)
        if c != "ok":
            errors[c] = errors.get(c, 0) + 1
    sent = len(samples)
    from_intended = [(s.end - s.intended) * 1000 for s in ok]
    service = [(s.end - s.start) * 1000 for s in ok]
    lag = [(s.start - s.intended) * 1000 for s in samples]
    error_rate = (sent - len(ok)) / sent if sent else 0.0
    # Answers that arrive after the segment ended still count, but the time they took does too:
    # a server that is 2 s behind has not kept up with the rate, even if every request succeeded.
    span = max([seconds] + [s.end - start for s in samples])
    achieved = len(ok) / span if span else 0.0
    lag_p95 = percentile(lag, 95)
    out = {
        "offered_rate": rate, "planned": planned, "sent": sent, "ok": len(ok),
        "achieved_rate": achieved, "errors": errors, "error_rate": error_rate,
        "duplicates": sum(1 for s in samples if s.duplicate),
        "latency_ms_from_intended_start": _ms(from_intended),
        "latency_ms_service": _ms(service),
        "start_lag_ms_p95": lag_p95,
        "saturated": bool(lag_p95 is not None and lag_p95 > SATURATED_LAG_P95_MS),
    }  # fmt: skip
    out["slo"] = verdict(out, slo)
    return out


def verdict(seg: dict, slo: Slo) -> dict:
    lat = seg["latency_ms_from_intended_start"]
    checks = {
        "p95": lat["p95"] is not None and lat["p95"] <= slo.p95_ms,
        "p99": lat["p99"] is not None and lat["p99"] <= slo.p99_ms,
        "errors": seg["sent"] > 0 and seg["error_rate"] <= slo.max_error_rate,
        "throughput": seg["achieved_rate"] >= slo.min_achieved * seg["offered_rate"],
    }
    return {"met": all(checks.values()), **checks}


def knee(segments: list[dict]) -> dict | None:
    """The highest offered rate reached before the first segment that misses the SLO.

    None when the first segment already misses. `limited` is False when every segment met it
    (the service was not pushed far enough to find a limit).
    """
    best = None
    for seg in segments:
        if not seg["slo"]["met"]:
            return {"offered_rate": best, "limited": True} if best is not None else None
        best = seg["offered_rate"]
    return {"offered_rate": best, "limited": False} if best is not None else None
