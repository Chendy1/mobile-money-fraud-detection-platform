"""Where events are scored: in this process, or by a running API over HTTP.

Both return the same `Outcome`, with the same status names, so a run can be compared across the
two. Neither raises for a rejected transaction: a rejection is a result to count.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

from mmrisk.api.schemas import TransactionIn
from mmrisk.api.service import (
    ConflictError,
    OutOfOrderTransactionError,
    ScoringService,
    UnknownCustomerError,
)
from mmrisk.api.smoke import check_url

SCORED, DUPLICATE, INVALID = "scored", "duplicate", "invalid"
UNKNOWN_CUSTOMER, OUT_OF_ORDER, CONFLICT = "unknown_customer", "out_of_order", "conflict"
UNAVAILABLE, ERROR = "unavailable", "error"
STATUSES = (
    SCORED,
    DUPLICATE,
    INVALID,
    UNKNOWN_CUSTOMER,
    OUT_OF_ORDER,
    CONFLICT,
    UNAVAILABLE,
    ERROR,
)


@dataclass
class Outcome:
    status: str
    response: dict | None
    millis: float
    detail: str = ""


def _wire(txn: dict) -> dict:
    return {k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in txn.items()}


class LocalScorer:
    """Scores with a `ScoringService` in this process, validating exactly as the API does."""

    name = "local"

    def __init__(self, service: ScoringService):
        self.service = service

    def score(self, txn: dict) -> Outcome:
        t0 = time.perf_counter()

        def done(status, response=None, detail=""):
            return Outcome(status, response, (time.perf_counter() - t0) * 1000, detail)

        try:
            valid = TransactionIn.model_validate(txn)
        except ValueError as exc:  # pydantic's ValidationError is a ValueError
            return done(INVALID, detail=exc.__class__.__name__)
        try:
            out = self.service.score(valid.model_dump())
        except UnknownCustomerError:
            return done(UNKNOWN_CUSTOMER)
        except OutOfOrderTransactionError:
            return done(OUT_OF_ORDER)
        except ConflictError:
            return done(CONFLICT)
        return done(DUPLICATE if out["duplicate"] else SCORED, out)


class HttpScorer:
    """Scores by posting to a running API (`/v1/score`). Standard library only."""

    name = "http"

    def __init__(self, url: str, timeout: float = 10.0):
        self.base = check_url(url)
        self.timeout = timeout

    def ready(self) -> bool:
        try:
            with urllib.request.urlopen(self.base + "/ready", timeout=self.timeout) as r:  # noqa: S310
                return r.status == 200
        except (OSError, ValueError):
            return False

    def score(self, txn: dict) -> Outcome:
        t0 = time.perf_counter()

        def done(status, response=None, detail=""):
            return Outcome(status, response, (time.perf_counter() - t0) * 1000, detail)

        body = json.dumps(_wire(txn)).encode()
        req = urllib.request.Request(self.base + "/v1/score", data=body, method="POST")
        req.add_header("content-type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:  # noqa: S310
                out = json.loads(resp.read().decode())
            return done(DUPLICATE if out.get("duplicate") else SCORED, out)
        except urllib.error.HTTPError as exc:
            try:
                code = json.loads(exc.read().decode()).get("error", {}).get("code", "")
            except ValueError:
                code = ""
            if exc.code in (413, 422):
                return done(INVALID, detail=str(exc.code))
            if exc.code == 404:
                return done(UNKNOWN_CUSTOMER)
            if exc.code == 409:
                return done(OUT_OF_ORDER if code == "out_of_order" else CONFLICT)
            if exc.code == 503:
                return done(UNAVAILABLE, detail="503")
            return done(ERROR, detail=str(exc.code))
        except (OSError, ValueError) as exc:
            return done(UNAVAILABLE, detail=exc.__class__.__name__)
