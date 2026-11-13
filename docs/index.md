# Documentation index

Every document in `docs/`, grouped by what you want to know. Start with the first group.

## Read first
- [Verification status](verification-status.md): what was checked, where (sandbox, the author's machine, CI) and what was not
- [Limitations and open items](limitations.md)
- [Model card](model-card.md): what the model is, what it was tested on, what it must not be used for
- [Architecture](architecture.md): diagram, data flow, AWS mapping
- [Decisions](decisions.md): the ADR index with one line per decision
- [Roadmap](roadmap.md) and [project state](PROJECT_STATE.md)

## Data and features
- [Data dictionary](data-dictionary.md): tables, columns, the lake layers
- [Data quality](data-quality.md): quarantine, checks, "what happens when bad data enters"
- [EDA report](eda-report.md): generated; every number computed from the lake
- [Feature catalog](feature-catalog.md): the 28 features

## Models and decisions
- [Baseline models](model-baselines.md)
- [Advanced model and decisions](model-advanced.md)
- [Cost model (assumed)](cost-model.md)
- [Model explanations](model-explanations.md)
- [Prediction store](prediction-store.md)

## Running and watching it
- [Monitoring](monitoring.md)
- [Drift](drift.md)
- [Retraining](retraining.md)
- [Dashboard](dashboard.md)
- [Load testing](load-testing.md)
- [Demo scripts](demo.md): a 5-minute and a 15-minute guided demo that runs the real commands

## Security and deployment
- [Security guide](security.md) and [threat model](threat-model.md)
- [Production-like local deployment](deployment.md)

## AWS (design and plan only; nothing was applied)
- [AWS design](aws-design.md)
- [Terraform, plan-only](terraform.md)
- [Cloud costs and controls](cloud-costs.md)
- [Cloud deployment runbook](cloud-deployment.md)

## Working on the project
- [Testing](testing.md)
- [Troubleshooting](troubleshooting.md)

## Architecture decision records
- [ADR-001: Synthetic data only](adr/ADR-001-synthetic-data.md)
- [ADR-002: LightGBM](adr/ADR-002-lightgbm.md)
- [ADR-003: In-process queue instead of Redpanda/Kafka](adr/ADR-003-in-process-queue.md)
- [ADR-004: DuckDB + Parquet](adr/ADR-004-duckdb-parquet.md)
- [ADR-005: Docker Compose instead of Kubernetes](adr/ADR-005-compose-not-kubernetes.md)
- [ADR-006: Installable `src/mmrisk` package](adr/ADR-006-package-layout.md)
- [ADR-007: Separate observable data, observed labels and ground truth](adr/ADR-007-labels-and-truth-separation.md)
- [ADR-008: Layered ingestion with immutable raw, quarantine and a ledger](adr/ADR-008-layered-ingestion.md)
- [ADR-009: Two-tier data quality (row rules quarantine, dataset checks report)](adr/ADR-009-two-tier-data-quality.md)
- [ADR-010: EDA as a reproducible report (DuckDB over the lake, observed labels on matured data only)](adr/ADR-010-eda-report-design.md)
- [ADR-011: One shared, strictly backward-looking feature library for training and serving](adr/ADR-011-shared-point-in-time-features.md)
- [ADR-012: Time-based split with a label gap; PR-AUC, review budgets and customer bootstrap](adr/ADR-012-time-split-and-evaluation.md)
- [ADR-013: LightGBM chosen on validation, Platt calibration, and decisions from an expected-cost rule](adr/ADR-013-calibration-and-cost-based-decisions.md)
- [ADR-014: Exact tree explanations, cross-checked three ways, and a model card generated from saved results](adr/ADR-014-explanations-and-model-card.md)
- [ADR-015: Local experiment tracking behind an interface, and one in-process training command](adr/ADR-015-experiment-tracking-and-pipeline.md)
- [ADR-016: A local scoring API with a framework-free core, strict input rules and in-memory history](adr/ADR-016-local-scoring-api.md)
- [ADR-017: Package the scoring API as one hardened container, run by Docker Compose on loopback only](adr/ADR-017-container-packaging.md)
- [ADR-018: A streaming simulation with a bounded in-process queue, a reorder buffer and an append-only prediction store](adr/ADR-018-streaming-simulation.md)
- [ADR-019: Monitoring with API metrics, Prometheus and Grafana, and checks on stored predictions](adr/ADR-019-monitoring.md)
- [ADR-020: Drift detection with population stability index, against a frozen reference](adr/ADR-020-drift-detection.md)
- [ADR-021: Gated retraining with a file-based model registry](adr/ADR-021-gated-retraining.md)
- [ADR-022: A read-only local dashboard on a tested data layer](adr/ADR-022-dashboard.md)
- [ADR-023: Security hardening](adr/ADR-023-security-hardening.md)
- [ADR-024: Load testing](adr/ADR-024-load-testing.md)
- [ADR-025: Production-like local deployment](adr/ADR-025-local-deployment.md)
- [ADR-026: AWS design (design only)](adr/ADR-026-aws-design.md)
- [ADR-027: Terraform, plan-only](adr/ADR-027-terraform-plan-only.md)
- [ADR-028: Cloud cost controls and an estimator with sourced prices](adr/ADR-028-cloud-cost-controls.md)
- [ADR-029: A guided demo that runs the real commands and checks what they show](adr/ADR-029-guided-demo.md)
