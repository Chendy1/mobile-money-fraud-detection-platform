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

Planned: time-based validation, PR-AUC, shared feature library vs feature store, ECS vs EKS.

## Rejected technologies
Kubernetes, Airflow, Spark, Feast, deep learning. None earns its cost at this scale. Revisit only with a concrete need.
