"""Turn the files the model steps saved into flat parameters and metrics for tracking.

Everything is read from those files, so a tracked run records exactly what the reports show.
Metric names are stable, use '/' as a separator and contain only letters, digits and _ - . space /.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

BASELINE_METRICS = ("pr_auc", "roc_auc", "precision@100", "recall@100")


def sanitize(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_\-. /]", "_", name)[:240]


def _load(path: Path) -> dict | None:
    path = Path(path)
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def _num(v) -> float | None:
    return float(v) if isinstance(v, int | float) and not isinstance(v, bool) else None


def collect(models_dir: Path) -> tuple[dict, dict[str, float]]:
    """(parameters, metrics) from `models/baselines` and `models/advanced`."""
    models_dir = Path(models_dir)
    params: dict = {}
    metrics: dict[str, float] = {}

    base = _load(models_dir / "baselines" / "baseline_metrics.json")
    if base:
        for part in ("validation", "test"):
            for model, d in base.get(part, {}).items():
                for k in BASELINE_METRICS:
                    v = _num(d.get("point", {}).get(k))
                    if v is not None:
                        metrics[sanitize(f"baseline/{part}/{model}/{k}")] = v
        for model, sel in (base.get("meta", {}).get("selected") or {}).items():
            for k, v in sel.items():
                params[sanitize(f"baseline_selected/{model}/{k}")] = v

    adv = _load(models_dir / "advanced" / "advanced_metrics.json")
    if adv:
        for part, key in (("test", "test_pr_auc"), ("validation", "validation_pr_auc")):
            for model, v in adv.get(key, {}).items():
                metrics[sanitize(f"{part}_pr_auc/{model}")] = float(v)
        for name, d in adv.get("cost_per_10k", {}).items():
            metrics[sanitize(f"assumed_cost_per_10k/{name}")] = float(d["point"])
        for kind in ("ece", "brier"):
            for name, v in adv.get("calibration", {}).get(kind, {}).items():
                metrics[sanitize(f"calibration_{kind}/{name}")] = float(v)
        for name, c in adv.get("calibrators", {}).items():
            metrics[sanitize(f"calibrator_a/{name}")] = float(c["a"])
            metrics[sanitize(f"calibrator_b/{name}")] = float(c["b"])
            params[sanitize(f"calibrator_link/{name}")] = c["link"]

    meta = _load(models_dir / "advanced" / "risk_model.json")
    if meta:
        for k, v in (meta.get("scorer_params") or {}).items():
            params[sanitize(f"lightgbm/{k}")] = v
        params["lightgbm/rounds"] = meta.get("rounds")
        params["feature_version"] = meta.get("feature_version")
        for k, v in (meta.get("cost_model_ASSUMED") or {}).items():
            params[sanitize(f"assumed_{k}")] = v

    exp = _load(models_dir / "advanced" / "explain_metrics.json")
    if exp:
        for k in ("additivity_error_max", "n_flagged", "reason_code_coverage", "n_test"):
            if _num(exp.get(k)) is not None:
                metrics[sanitize(f"explain/{k}")] = float(exp[k])
        for k, v in exp.get("agreement", {}).items():
            if _num(v) is not None:
                metrics[sanitize(f"explain/agreement/{k}")] = float(v)
        if exp.get("permutation"):
            metrics["explain/permutation_top_drop"] = float(exp["permutation"][0]["drop"])
        for r in exp.get("ablation", []):
            metrics[sanitize(f"explain/ablation_delta/{r['unit']}")] = float(r["delta"])
        for part in exp.get("split_summary", []):
            for k in ("rows", "fraud"):
                params[sanitize(f"split/{part['part']}/{k}")] = part[k]
        for k, v in exp.get("split_bounds", {}).items():
            params[sanitize(f"split/{k}")] = v
    return params, metrics
