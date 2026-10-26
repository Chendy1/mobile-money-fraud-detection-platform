"""Train a candidate on the newest data; gates decide whether it may replace the champion."""

from __future__ import annotations

import json
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import polars as pl

from mmrisk.api.service import ModelLoadError, load_model
from mmrisk.models.boosting import fit_lightgbm
from mmrisk.models.bundle import RiskModel
from mmrisk.models.calibration import PlattCalibrator
from mmrisk.models.metrics import pr_auc
from mmrisk.models.split import SplitConfig, SplitError, make_split
from mmrisk.registry import Registry, RegistryError
from mmrisk.retrain import gates as G
from mmrisk.retrain.config import Gates


class RetrainError(ValueError):
    """The retraining step cannot run (and why)."""


@dataclass
class ChampionInfo:
    version: str
    train_end: datetime | None
    val_start: datetime | None
    data_end: datetime | None


@dataclass
class Outcome:
    approved: bool
    trained: bool
    candidate: str | None
    promoted: bool
    gates: list[G.Gate]
    numbers: dict
    split: dict
    champion: str
    quick: bool


def _when(meta: dict, key: str) -> datetime | None:
    try:
        return datetime.fromisoformat(meta["split"][key])
    except (KeyError, TypeError, ValueError):
        return None


def champion_info(registry: Registry) -> ChampionInfo:
    prod = registry.production()
    if prod is None:
        raise RetrainError(
            f"no production model in {registry.root}; run python -m mmrisk.registry bootstrap"
        )
    try:
        folder = registry.production_dir()
    except RegistryError as exc:
        raise RetrainError(f"the production model cannot be used: {exc}") from None
    meta = json.loads((folder / "risk_model.json").read_text(encoding="utf-8"))
    return ChampionInfo(prod["version"], _when(meta, "train_end"), _when(meta, "val_start"),
                        _when(meta, "data_end"))  # fmt: skip


def new_fraud_labels(
    labels: pl.DataFrame, txns: pl.DataFrame, since: datetime, as_of: datetime
) -> int:
    """Fraud labels, known by `as_of`, on transactions made after `since`.

    `since` is the end of the data the champion was built from, so these are frauds it could not
    have learned from. A retrain on the same data end would add nothing, and counts zero here.
    """
    when = txns.select("transaction_id", "timestamp")
    return int(
        labels.join(when, on="transaction_id", how="inner")
        .filter(
            (pl.col("label_is_fraud") == 1)
            & (pl.col("timestamp") > since)
            & (pl.col("label_available_at") <= as_of)
        )
        .height
    )


def run_retrain(
    features: pl.DataFrame,
    labels: pl.DataFrame,
    registry: Registry,
    *,
    seed: int = 42,
    quick: bool = False,
    th: Gates | None = None,
    auto_promote: bool = False,
) -> Outcome:
    th = th or Gates()
    champ = champion_info(registry)
    try:
        champion = load_model(registry.root)
    except ModelLoadError as exc:
        raise RetrainError(f"cannot load the champion: {exc}") from None
    try:
        split = make_split(features, labels, SplitConfig())
    except SplitError as exc:
        raise RetrainError(f"cannot split the data: {exc}") from None
    bounds = {k: str(v) for k, v in split.bounds.items()}

    def fraud(df: pl.DataFrame) -> int:
        return int(df["label_is_fraud"].sum())

    gates = G.data_gates(fraud(split.train), fraud(split.val), fraud(split.test), th)
    gates.append(G.fair_comparison_gate(champ.train_end, split.bounds["test_start"]))
    if not G.approved(gates):  # no point training on data that fails these
        return Outcome(False, False, None, False, gates, {}, bounds, champ.version, quick)

    lgbm, _grid = fit_lightgbm(split, seed=seed, quick=quick)
    y_val = split.val["label_is_fraud"].to_numpy()
    calibrator = PlattCalibrator.fit(lgbm.score(split.val), y_val, "logit")
    candidate = RiskModel(lgbm, calibrator, champion.cost_model)

    test = split.test
    y = test["label_is_fraud"].to_numpy().astype(float)
    p_new = candidate.probability(test)
    p_old = champion.probability(test)
    metric_gates, numbers = G.compare(
        y=y,
        amount=test["amount_kes"].to_numpy().astype(float),
        customers=test["customer_id"].to_numpy(),
        champion_p=p_old,
        candidate_p=p_new,
        cost_model=champion.cost_model,
        th=th,
        seed=seed + 7,
    )
    gates += metric_gates

    with tempfile.TemporaryDirectory() as tmp:
        saved = Path(tmp) / "model"
        candidate.save(saved, extra={
            "split": bounds, "seed": seed, "quick": quick, "parent": champ.version,
            "validation_pr_auc": pr_auc(y_val, candidate.probability(split.val)),
            "test_pr_auc": numbers["pr_auc"]["candidate"],
            "note": "cost model values are ASSUMPTIONS for this simulation, not measurements",
        })  # fmt: skip
        head = test.head(500)
        reloaded = load_model(saved)  # raises if the saved file is not a usable model
        gates.append(G.roundtrip_gate(candidate.probability(head), reloaded.probability(head)))
        ok = G.approved(gates)
        version = registry.register(
            saved, source="retrain", stage="approved" if ok else "rejected", parent=champ.version,
            quick=quick, metrics=numbers, gates=[g.as_dict() for g in gates],
            note=f"trained to {bounds['train_end']}" + (" (quick)" if quick else ""),
        )  # fmt: skip

    promoted = False
    if ok and auto_promote and not quick:
        registry.promote(version, "automatic: every required gate passed")
        promoted = True
    return Outcome(ok, True, version, promoted, gates, numbers, bounds, champ.version, quick)


def _gate_rows(gates: list[G.Gate]) -> str:
    rows = ["| gate | kind | result | value | rule | detail |", "|---|---|---|---|---|---|"]
    for g in gates:
        result = "PASS" if g.passed else ("FAIL" if g.required else "WARN")
        value = "" if g.value is None else f"{g.value:.4g}"
        rows.append(f"| {g.name} | {'required' if g.required else 'advisory'} | {result} | "
                    f"{value} | {g.threshold} | {g.detail} |")  # fmt: skip
    return "\n".join(rows)


def markdown(o: Outcome) -> str:
    if not o.trained:
        head = "No candidate was trained: a gate on the data failed."
    elif o.approved:
        head = f"Candidate {o.candidate} passed every required gate." + (
            " It was promoted to production."
            if o.promoted
            else " It is registered as approved and waits for an explicit promote."
        )
    else:
        head = f"Candidate {o.candidate} FAILED a required gate and is registered as rejected."
    lines = [
        "# Gated retraining report", "", f"**{head}**", "",
        f"Champion: {o.champion}. Split ends {o.split.get('data_end')}; test window "
        f"({o.split.get('test_start')}, {o.split.get('matured_end')}].",
        "",
        _gate_rows(o.gates), "",
    ]  # fmt: skip
    if o.quick:
        lines += ["> Quick mode (a smoke test): this candidate cannot be promoted without "
                  "--force.", ""]  # fmt: skip
    lines += ["All thresholds are ASSUMED values for this simulation (see `retrain/config.py`). "
              "The cost model is assumed too. Nothing here is measured from real mobile-money "
              "data.", ""]  # fmt: skip
    return "\n".join(lines)


def payload(o: Outcome) -> dict:
    return {
        "approved": o.approved, "trained": o.trained, "candidate": o.candidate,
        "promoted": o.promoted, "champion": o.champion, "quick": o.quick, "split": o.split,
        "numbers": o.numbers, "gates": [g.as_dict() for g in o.gates],
    }  # fmt: skip


__all__ = ["Outcome", "RetrainError", "run_retrain", "champion_info", "new_fraud_labels"]
