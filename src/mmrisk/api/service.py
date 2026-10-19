"""The scoring core, independent of any web framework.

It holds one trained `RiskModel` and one `OnlineFeatureState` per process. Scoring a transaction
computes its features from the history seen so far, assesses it, and only then records it, so a
failed request never changes the state. A repeated transaction id returns the first answer; the
same id with different content is refused.

Limits (kept on purpose, see ADR-016): state lives in this process's memory and is lost on restart;
one process only; a lock serialises scoring, so throughput is bounded by one core; the history per
customer is unbounded and each score recomputes over it.
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from pathlib import Path

import polars as pl

from mmrisk.features import OnlineFeatureState, OutOfOrderError
from mmrisk.features.registry import FEATURE_VERSION, INPUT_COLUMNS
from mmrisk.models.bundle import BUNDLE_FILE, RiskModel
from mmrisk.models.explain import ExplainError


class ServiceError(Exception):
    """Base for errors a caller caused. `code` is stable; `message` is safe to show."""

    code = "error"


class ModelLoadError(ServiceError):
    code = "model_load"


class UnknownCustomerError(ServiceError):
    code = "unknown_customer"


class ConflictError(ServiceError):
    code = "conflict"


class OutOfOrderTransactionError(ConflictError):
    code = "out_of_order"


class DuplicateConflictError(ConflictError):
    code = "duplicate_id_different_content"


def load_model(model_dir: Path) -> RiskModel:
    """Load a saved bundle and refuse one built for other features.

    The bundle is a pickle: only load files that you created. Loading a file from someone else
    can run their code.
    """
    path = Path(model_dir) / BUNDLE_FILE
    if not path.exists():
        raise ModelLoadError(f"no model at {path}; run the advanced step or the pipeline first")
    model = RiskModel.load(Path(model_dir))
    if model.feature_version != FEATURE_VERSION:
        raise ModelLoadError(
            f"model was built for feature version {model.feature_version}, "
            f"this code computes {FEATURE_VERSION}; retrain"
        )
    return model


def _payload(txn: dict) -> dict:
    return {k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in txn.items()}


class ScoringService:
    def __init__(
        self,
        model: RiskModel,
        customers: pl.DataFrame,
        history: pl.DataFrame | None = None,
        cache_size: int = 10_000,
        top_k: int = 3,
    ):
        if model.feature_version != FEATURE_VERSION:
            raise ModelLoadError(
                f"model feature version {model.feature_version} != code {FEATURE_VERSION}"
            )
        if cache_size < 1 or top_k < 1:
            raise ValueError("cache_size and top_k must be at least 1")
        self.model = model
        self._state = OnlineFeatureState(customers)
        self._customers = customers.height
        self._lock = threading.Lock()
        self._seen: OrderedDict[str, tuple[dict, dict]] = OrderedDict()
        self._cache_size = cache_size
        self._top_k = top_k
        self.history_transactions = 0
        if history is not None and not history.is_empty():
            self.replay(history)

    # ---- state ------------------------------------------------------------------------------

    def replay(self, history: pl.DataFrame) -> int:
        """Record earlier transactions without scoring them, in time order."""
        rows = history.select(INPUT_COLUMNS).sort("timestamp", "transaction_id").to_dicts()
        with self._lock:
            for row in rows:
                self._state.observe(row)
            self.history_transactions += len(rows)
        return len(rows)

    # ---- scoring ----------------------------------------------------------------------------

    def score(self, txn: dict) -> dict:
        """Score one transaction (a dict with the ten input columns) and record it."""
        payload = _payload(txn)
        with self._lock:
            known = self._seen.get(txn["transaction_id"])
            if known is not None:
                if known[0] != payload:
                    raise DuplicateConflictError(
                        f"transaction_id {txn['transaction_id']} was already scored "
                        "with different content"
                    )
                self._seen.move_to_end(txn["transaction_id"])
                return {**known[1], "duplicate": True}
            try:
                frame = self._state.feature_frame(txn)
            except KeyError as exc:
                raise UnknownCustomerError(f"unknown customer {txn['customer_id']}") from exc
            except OutOfOrderError as exc:
                raise OutOfOrderTransactionError(str(exc)) from exc
            result = self._respond(frame)
            self._state.observe(txn)  # last: a failure above leaves the state unchanged
            self.history_transactions += 1
            self._seen[txn["transaction_id"]] = (payload, result)
            if len(self._seen) > self._cache_size:
                self._seen.popitem(last=False)
            return {**result, "duplicate": False}

    def _respond(self, frame: pl.DataFrame) -> dict:
        row = self.model.assess(frame).row(0, named=True)
        reasons: list[dict] = []
        if row["action"] != "ALLOW":
            try:
                codes = self.model.explain(frame, top_k=self._top_k)
                reasons = [
                    {k: r[k] for k in ("rank", "feature", "effect", "text")}
                    for r in codes.sort("rank").to_dicts()
                ]
            except ExplainError:
                reasons = []  # this scorer cannot explain itself; the score is still valid
        return {
            "transaction_id": row["transaction_id"],
            "risk_probability": float(row["risk_probability"]),
            "action": row["action"],
            "risk_level": row["risk_level"],
            "expected_cost_assumed_kes": {
                "allow": float(row["expected_cost_allow"]),
                "review": float(row["expected_cost_review"]),
                "block": float(row["expected_cost_block"]),
            },
            "reasons": reasons,
            "feature_version": self.model.feature_version,
        }

    # ---- description ------------------------------------------------------------------------

    def info(self) -> dict:
        scorer = self.model.scorer
        return {
            "scorer": getattr(scorer, "name", type(scorer).__name__),
            "feature_version": self.model.feature_version,
            "calibrator_link": self.model.calibrator.link,
            "cost_model_ASSUMED": self.model.cost_model.as_dict(),
            "customers": self._customers,
            "history_transactions": self.history_transactions,
            "synthetic_data_only": True,
        }
