"""RiskModel: what a scoring service needs, in one object.

Features go in; a calibrated fraud probability, the cheapest action and a risk level come out.
The cost model and the calibrator are stored with the scorer so that a saved model and its
decision rule can never be mismatched.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import joblib
import numpy as np
import polars as pl

from mmrisk.features.registry import FEATURE_VERSION
from mmrisk.models.calibration import PlattCalibrator
from mmrisk.models.costs import ACTIONS, RISK_LEVELS, CostModel

BUNDLE_FILE = "risk_model.joblib"
META_FILE = "risk_model.json"


@dataclass
class RiskModel:
    scorer: object  # anything with .score(features_df) -> raw scores
    calibrator: PlattCalibrator
    cost_model: CostModel
    feature_version: str = FEATURE_VERSION

    def probability(self, df: pl.DataFrame) -> np.ndarray:
        return self.calibrator.transform(self.scorer.score(df))

    def assess(self, df: pl.DataFrame) -> pl.DataFrame:
        """One row per transaction: probability, expected costs, action and risk level."""
        p = self.probability(df)
        amount = df["amount_kes"].to_numpy().astype(float)
        costs = self.cost_model.expected_costs(p, amount)
        action = np.argmin(costs, axis=1)
        return pl.DataFrame(
            {
                "transaction_id": df["transaction_id"],
                "risk_probability": p,
                "expected_cost_allow": costs[:, 0],
                "expected_cost_review": costs[:, 1],
                "expected_cost_block": costs[:, 2],
                "action": [ACTIONS[i] for i in action],
                "risk_level": [RISK_LEVELS[i] for i in action],
            }
        )

    def explain(self, df: pl.DataFrame, top_k: int = 3) -> pl.DataFrame:
        """Reason codes: the features that raised each transaction's risk the most.

        Needs a scorer with exact contributions (the LightGBM model) and a logit calibration, so
        that effects add up on the calibrated log-odds scale. They describe the model, not a cause.
        """
        from mmrisk.models.explain import ExplainError, contribution_frame, reason_codes

        if not hasattr(self.scorer, "contributions") or self.calibrator.link != "logit":
            raise ExplainError("explanations need the LightGBM scorer and a logit calibration")
        phi, _ = contribution_frame(self.scorer, df)
        return reason_codes(df, phi, a=self.calibrator.a, top_k=top_k)

    def save(self, directory: Path, extra: dict | None = None) -> Path:
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        joblib.dump(self, directory / BUNDLE_FILE)
        meta = {
            "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "feature_version": self.feature_version,
            "scorer": getattr(self.scorer, "name", type(self.scorer).__name__),
            "scorer_params": getattr(self.scorer, "params", None),
            "rounds": getattr(self.scorer, "n_rounds", None),
            "calibrator": {
                "a": self.calibrator.a,
                "b": self.calibrator.b,
                "link": self.calibrator.link,
            },
            "cost_model_ASSUMED": self.cost_model.as_dict(),
            **(extra or {}),
        }
        (directory / META_FILE).write_text(json.dumps(meta, indent=2, default=str) + "\n")
        return directory / BUNDLE_FILE

    @staticmethod
    def load(directory: Path) -> RiskModel:
        return joblib.load(Path(directory) / BUNDLE_FILE)
