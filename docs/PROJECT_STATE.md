# Project state (paste at the start of a new chat)

- **Repo:** mobile-money-fraud-detection-platform; GitHub user: Chendy1; author: Clyde Adili; license: Apache-2.0
- **Current phase:** 3 complete in the assistant's sandbox; Windows and CI results for Phases 2 and 3 TO VERIFY (not reported by the user). Phase 1 fully verified. Next: Phase 4, data quality and validation, on "CONTINUE TO PHASE 4".
- **Environment:** Windows, PowerShell; 8 GB RAM; i5-6300U (2 cores / 4 threads); ~23.9 GB free disk; Python 3.12.10; conda not on PATH; Docker Desktop installed (9 containers running, build cache pruned).
- **Machine rules:** native first; cap WSL2 memory with `.wslconfig` (3 GB, 2 processors); stop unused containers before training; datasets 200k-500k rows.
- **Layout:** installable package `src/mmrisk/` (ADR-006); `training/` and `apps/` entry points; Windows task runner `scripts/dev.ps1`; Makefile for Linux/macOS.
- **Stack decided:** Python 3.12 + .venv, Polars, DuckDB + Parquet, scikit-learn, LightGBM, SHAP, MLflow, FastAPI, Streamlit, Evidently, Prometheus + Grafana, Docker Compose, pytest, ruff, GitHub Actions, Terraform (optional). Runtime dependencies are added per phase.
- **Rejected:** Redpanda/Kafka (in-process queue), Kubernetes, Airflow, Spark, Feast, deep learning.
- **Verified:** Phase 1: Windows lint and 5 tests pass; GitHub Actions green. Phases 2 and 3 (sandbox only, Linux, Python 3.12.3): lint clean, 73 tests pass (also with deprecation warnings as errors); mutation checks fail the right tests. Default dataset: generation 1.3 s; ingestion 5.9 s, about 244 MB peak, 28 MB and 1,550 files in the lake (365 daily partitions per layer, about 16 KB each); a repeat run skips all batches in about 2 s. Windows and CI for Phases 2 and 3: TO VERIFY.
- **Lake design (ADR-008):** `data/raw` = source extract; `data/processed/{raw,validated,quarantine,cleaned,_meta}`; batch = (table, SHA-256); ledger written last; readers see only COMPLETE batches; first-write-wins dedup, conflicts quarantined; structural gates only (domain rules = Phase 4); label revisions treated as conflicts. DuckDB deferred to Phase 5 (EDA).
- **Data design:** observable tables, noisy delayed labels (`labels.parquet`) and hidden `ground_truth/` are separate (ADR-007). Device ids are permuted. Labels need `label_available_at <= T`. Runtime deps now: numpy, polars.
- **Open items:** commit author email check (`git log -3 --format="%an <%ae>"`) never reported; `.wslconfig` not yet created (needed by Phase 12); CI #2 (line endings) result not reported.
- **Rules:** one phase at a time; never fabricate results; no `terraform apply` without approval; synthetic data only; "M-Pesa-inspired", no logos.
