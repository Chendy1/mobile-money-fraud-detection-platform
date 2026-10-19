"""One command for the whole training pipeline, with optional experiment tracking.

Stages, in order, from the ingested lake: features (computed once), baselines (Phase 7), the
advanced model with calibration and decisions (Phase 8), explanations (Phase 9) and the model
card. Each stage is the same code the single commands run; this module only orders them, times
them and records what they produced. Nothing here changes a result: the same seed gives the same
numbers with tracking on or off (a test checks that).
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from mmrisk.features import compute_features
from mmrisk.ingestion.reader import read_layer
from mmrisk.models.costs import CostModel
from mmrisk.pipeline.collect import collect
from mmrisk.pipeline.lineage import code_version, data_fingerprint, environment
from mmrisk.pipeline.tracker import NullTracker, Tracker

STAGES = ("features", "baselines", "advanced", "explain", "card")
MANIFEST = "pipeline_run.json"
EXPERIMENT = "mmrisk-risk-model"


class PipelineError(ValueError):
    """The pipeline cannot start (for example, no ingested data)."""


@dataclass(frozen=True)
class PipelineConfig:
    lake: Path = Path("data/processed")
    source: Path = Path("data/raw")
    models_dir: Path = Path("models")
    docs_dir: Path = Path("docs")
    seed: int = 42
    quick: bool = False
    bootstrap: int = 300
    use_truth: bool = True
    cost_model: CostModel = field(default_factory=CostModel)
    run_name: str | None = None


@dataclass
class PipelineResult:
    run_name: str
    run_id: str | None
    tracker: str
    params: dict
    metrics: dict[str, float]
    stage_seconds: dict[str, float]
    manifest_path: Path
    paths: dict[str, Path]


def resolve_dirs(quick: bool, models_dir: Path | None, docs_dir: Path | None) -> tuple[Path, Path]:
    """Where a run writes. A quick run gets its own folders so it cannot overwrite the full run's
    models or the committed reports; a folder given explicitly is always used as given."""
    models = Path(models_dir) if models_dir else Path("models/quick" if quick else "models")
    docs = Path(docs_dir) if docs_dir else Path("docs/quick" if quick else "docs")
    return models, docs


def _paths(cfg: PipelineConfig) -> dict[str, Path]:
    return {
        "baselines_dir": cfg.models_dir / "baselines",
        "advanced_dir": cfg.models_dir / "advanced",
        "figures": cfg.docs_dir / "figures" / "models",
        "report_baselines": cfg.docs_dir / "model-baselines.md",
        "report_advanced": cfg.docs_dir / "model-advanced.md",
        "report_explain": cfg.docs_dir / "model-explanations.md",
        "card": cfg.docs_dir / "model-card.md",
        "truth": cfg.source / "ground_truth" / "transaction_truth.parquet",
    }


def _load(lake: Path):
    if not Path(lake).exists():
        raise PipelineError(f"lake {lake} not found; run the ingestion first")
    tables = {t: read_layer(lake, "cleaned", t) for t in ("transactions", "customers", "labels")}
    empty = [k for k, v in tables.items() if v.is_empty()]
    if empty:
        raise PipelineError(f"lake has no completed data for: {', '.join(empty)}")
    return compute_features(tables["transactions"], tables["customers"]), tables["labels"]


def run_pipeline(
    cfg: PipelineConfig,
    tracker: Tracker | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    stages: dict[str, Callable] | None = None,
) -> PipelineResult:
    """Run every stage. `stages` lets a test replace one stage (for example to make it fail)."""
    from mmrisk.models.advanced import run_advanced
    from mmrisk.models.explain_run import run_explain
    from mmrisk.models.model_card import write as write_card
    from mmrisk.models.report import run as run_baselines

    tracker = tracker or NullTracker()
    p = _paths(cfg)
    if not Path(cfg.lake).exists():
        raise PipelineError(f"lake {cfg.lake} not found; run the ingestion first")
    started = now()
    name = cfg.run_name or f"pipeline-{started:%Y%m%d-%H%M%S}"
    truth = p["truth"] if cfg.use_truth and p["truth"].exists() else None
    fingerprint = data_fingerprint(cfg.lake)
    code, env = code_version(), environment()
    tags = {"synthetic_data": "true", "costs_assumed": "true", "pipeline": "mmrisk",
            **code, **{f"lib.{k}": v for k, v in env.items()}}  # fmt: skip
    tracker.start(name, tags)
    seconds: dict[str, float] = {}
    state: dict = {}
    current = {"stage": STAGES[0]}

    def stage(label: str, fn: Callable[[], object]) -> None:
        current["stage"] = label
        t0 = time.time()
        try:
            fn()
        finally:
            seconds[label] = time.time() - t0

    custom = stages or {}
    try:
        tracker.log_params(
            {
                "config/seed": cfg.seed, "config/quick": cfg.quick,
                "config/bootstrap": cfg.bootstrap, "config/ground_truth_audit": truth is not None,
                "data/fingerprint_sha256": fingerprint["sha256"],
                "data/batches": fingerprint["batches"],
                **{f"data/{t}/rows_cleaned": v["rows_cleaned"]
                   for t, v in fingerprint["tables"].items()},
            }
        )  # fmt: skip

        def features():
            state["features"], state["labels"] = _load(cfg.lake)

        def baselines():
            run_baselines(state["features"], state["labels"], p["baselines_dir"],
                          p["report_baselines"], p["figures"], truth_file=truth, seed=cfg.seed,
                          quick=cfg.quick, n_boot=cfg.bootstrap)  # fmt: skip

        def advanced():
            run_advanced(state["features"], state["labels"], p["baselines_dir"], p["advanced_dir"],
                         p["report_advanced"], p["figures"], truth_file=truth,
                         cost_model=cfg.cost_model, seed=cfg.seed, quick=cfg.quick,
                         n_boot=cfg.bootstrap)  # fmt: skip

        def explain():
            run_explain(state["features"], state["labels"], p["advanced_dir"],
                        p["report_explain"], p["figures"], truth_file=truth, seed=cfg.seed,
                        quick=cfg.quick)  # fmt: skip

        def card():
            write_card(p["advanced_dir"], p["card"])

        default = {"features": features, "baselines": baselines, "advanced": advanced,
                   "explain": explain, "card": card}  # fmt: skip
        for label in STAGES:
            stage(label, custom.get(label) or default[label])
            tracker.log_metrics({f"seconds/{label}": seconds[label]})

        params, metrics = collect(cfg.models_dir)
        metrics["seconds/total"] = sum(seconds.values())
        tracker.log_params(params)
        tracker.log_metrics(metrics)
        manifest = cfg.models_dir / MANIFEST
        manifest.write_text(
            json.dumps(
                {
                    "run_name": name,
                    "started_at": started.isoformat(timespec="seconds"),
                    "tracker": tracker.name,
                    "data": fingerprint,
                    "code": code,
                    "environment": env,
                    "config": {
                        "seed": cfg.seed,
                        "quick": cfg.quick,
                        "bootstrap": cfg.bootstrap,
                        "ground_truth_audit": truth is not None,
                    },  # fmt: skip
                    "params": params,
                    "metrics": metrics,
                    "stage_seconds": seconds,
                },
                indent=2,
                sort_keys=True,
                default=str,
            )
            + "\n",
            encoding="utf-8",
        )
        _log_artifacts(tracker, cfg, p, manifest)
    except BaseException as exc:
        tracker.set_tags({"failed_stage": current["stage"], "error_type": type(exc).__name__})
        tracker.log_metrics({f"seconds/{k}": v for k, v in seconds.items()})
        tracker.end("KILLED" if isinstance(exc, KeyboardInterrupt) else "FAILED")
        raise
    tracker.end("FINISHED")
    return PipelineResult(name, tracker.run_id, tracker.name, params, metrics, seconds, manifest, p)


def _log_artifacts(tracker: Tracker, cfg: PipelineConfig, p: dict, manifest: Path) -> None:
    for key in ("report_baselines", "report_advanced", "report_explain", "card"):
        if p[key].exists():
            tracker.log_artifact(p[key], "reports")
    if p["figures"].exists():
        tracker.log_artifacts(p["figures"], "figures")
    for sub, files in (
        ("baselines", ("baseline_metrics.json", "baseline_metadata.json")),
        (
            "advanced",
            (
                "risk_model.joblib",
                "risk_model.json",
                "advanced_metrics.json",
                "explain_metrics.json",
            ),
        ),  # fmt: skip
    ):
        for f in files:
            path = cfg.models_dir / sub / f
            if path.exists():
                tracker.log_artifact(path, f"models/{sub}")
    tracker.log_artifact(manifest)
