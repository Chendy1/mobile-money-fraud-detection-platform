# Decisions (ADR index)

| ADR | Title | Status |
|---|---|---|
| [001](adr/ADR-001-synthetic-data.md) | Synthetic data only | Accepted |
| [002](adr/ADR-002-lightgbm.md) | LightGBM as the gradient-boosting model | Accepted |
| [003](adr/ADR-003-in-process-queue.md) | In-process queue instead of Redpanda/Kafka | Accepted |
| [004](adr/ADR-004-duckdb-parquet.md) | DuckDB + Parquet instead of a warehouse | Accepted |
| [005](adr/ADR-005-compose-not-kubernetes.md) | Docker Compose instead of Kubernetes | Accepted |
| [006](adr/ADR-006-package-layout.md) | Installable `src/mmrisk` package | Accepted |
| [007](adr/ADR-007-labels-and-truth-separation.md) | Separate observable data, labels and ground truth | Accepted |
| [008](adr/ADR-008-layered-ingestion.md) | Layered ingestion with immutable raw, quarantine and a ledger | Accepted |
| [009](adr/ADR-009-two-tier-data-quality.md) | Two-tier data quality (row rules quarantine, dataset checks report) | Accepted |
| [010](adr/ADR-010-eda-report-design.md) | EDA as a reproducible report: DuckDB over the lake, observed labels on matured data only | Accepted |
| [011](adr/ADR-011-shared-point-in-time-features.md) | One shared, strictly backward-looking feature library for training and serving | Accepted |
| [012](adr/ADR-012-time-split-and-evaluation.md) | Time-based split with a label gap; PR-AUC, review budgets and customer bootstrap | Accepted |
| [013](adr/ADR-013-calibration-and-cost-based-decisions.md) | LightGBM chosen on validation, Platt calibration, decisions from an expected-cost rule | Accepted |
| [014](adr/ADR-014-explanations-and-model-card.md) | Exact tree explanations cross-checked three ways; a model card generated from saved results | Accepted |

| [015](adr/ADR-015-experiment-tracking-and-pipeline.md) | Local-only experiment tracking behind an interface; one in-process training command | Accepted |

| [016](adr/ADR-016-local-scoring-api.md) | Local scoring API: framework-free core, strict input, in-memory history, loopback only | Accepted |

Planned: ECS vs EKS.

## Rejected technologies
Kubernetes, Airflow, Spark, Feast, deep learning. None earns its cost at this scale. Revisit only with a concrete need.
