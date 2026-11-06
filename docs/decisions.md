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

| [017](adr/ADR-017-container-packaging.md) | One hardened API container on loopback, model and data mounted read-only | Accepted |

| [018](adr/ADR-018-streaming-simulation.md) | Streaming simulation: bounded queue, reorder buffer, append-only prediction store | Accepted |

| [019](adr/ADR-019-monitoring.md) | Monitoring: API metrics, Prometheus and Grafana, checks on stored predictions | Accepted |
| [020](adr/ADR-020-drift-detection.md) | Drift detection: PSI against a frozen validation-window reference; Evidently not used | Accepted |
| [021](adr/ADR-021-gated-retraining.md) | Gated retraining: file-based registry, trigger check, candidate-vs-champion gates; promotion explicit | Accepted |
| [022](adr/ADR-022-dashboard.md) | Dashboard: read-only Streamlit page on a tested, Streamlit-free data layer; loopback only | Accepted |
| [023](adr/ADR-023-security-hardening.md) | Security hardening: hashed API keys with roles, TLS, audit log, allow-list loader, secret scan | Accepted |
| [024](adr/ADR-024-load-testing.md) | Load testing: own open-loop tool, latency from intended start, ASSUMED targets | Accepted |
| [025](adr/ADR-025-local-deployment.md) | Production-like local deployment: secrets as files, hardened compose, preflight, verify, backup and restore | Accepted |

Planned: ECS vs EKS.

## Security (Phase 18, ADR-023)
Keys with roles for the API; external binding needs keys, TLS and a flag; allow-list unpickling (not a sandbox); shared-password dashboard login; own secret scanner plus non-blocking `pip-audit`. All limits ASSUMED.

## Load testing (Phase 19, ADR-024)
Own small open-loop tool, standard library only; latency measured from the intended start; one worker per customer; `bench` owns its server; loopback only; ASSUMED targets printed on every report; not run in CI.

## Production-like local deployment (Phase 20, ADR-025)
Second compose file with Docker secrets as files, hardened containers, loopback ports; a preflight that reads the compose file as data; `verify` from outside (boundaries, not only the happy path); strict backup and restore; one self-signed certificate used as its own trust anchor; retraining stays on the host; model changes need an API restart. Docker never run by the assistant: TO VERIFY.

## Rejected technologies
Kubernetes, Airflow, Spark, Feast, deep learning. None earns its cost at this scale. Revisit only with a concrete need.
