"""Build a ready `ScoringService` from a saved model and the data lake."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import polars as pl

from mmrisk.api.service import ModelLoadError, ScoringService, load_model
from mmrisk.ingestion import read_layer


def read_lake(lake: Path) -> tuple[pl.DataFrame, pl.DataFrame]:
    if not Path(lake).exists():
        raise ModelLoadError(f"lake {lake} not found; run the ingestion first")
    txns = read_layer(lake, "cleaned", "transactions")
    customers = read_layer(lake, "cleaned", "customers")
    if txns.is_empty() or customers.is_empty():
        raise ModelLoadError(f"lake {lake} has no completed transactions or customers")
    return txns, customers


def build_service(
    model_dir: Path,
    lake: Path,
    history_until: datetime | None = None,
    use_history: bool = True,
) -> ScoringService:
    """Model from `model_dir`; customers and (optionally) past transactions from the lake.

    History is every cleaned transaction before `history_until` (all of them when it is None).
    """
    model = load_model(model_dir)
    txns, customers = read_lake(lake)
    history = None
    if use_history:
        history = (
            txns if history_until is None else txns.filter(pl.col("timestamp") < history_until)
        )
    return ScoringService(model, customers, history)
