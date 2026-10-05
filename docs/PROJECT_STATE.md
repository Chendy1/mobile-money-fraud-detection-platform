# Project state (paste at the start of a new chat)

- **Repo:** mobile-money-fraud-detection-platform; GitHub user: Chendy1; author: Clyde Adili; license: Apache-2.0
- **Current phase:** 2 complete in the assistant's sandbox; Windows and CI results TO VERIFY. Phase 1 fully verified (Windows tests pass, CI green). Next: Phase 3, ingestion and data layers, on "CONTINUE TO PHASE 3".
- **Environment:** Windows, PowerShell; 8 GB RAM; i5-6300U (2 cores / 4 threads); ~23.9 GB free disk; Python 3.12.10; conda not on PATH; Docker Desktop installed (9 containers running, build cache pruned).
- **Machine rules:** native first; cap WSL2 memory with `.wslconfig` (3 GB, 2 processors); stop unused containers before training; datasets 200k-500k rows.
- **Layout:** installable package `src/mmrisk/` (ADR-006); `training/` and `apps/` entry points; Windows task runner `scripts/dev.ps1`; Makefile for Linux/macOS.
- **Stack decided:** Python 3.12 + .venv, Polars, DuckDB + Parquet, scikit-learn, LightGBM, SHAP, MLflow, FastAPI, Streamlit, Evidently, Prometheus + Grafana, Docker Compose, pytest, ruff, GitHub Actions, Terraform (optional). Runtime dependencies are added per phase.
- **Rejected:** Redpanda/Kafka (in-process queue), Kubernetes, Airflow, Spark, Feast, deep learning.
- **Verified:** Phase 1: Windows lint and 5 tests pass; GitHub Actions green. Phase 2 (sandbox only, Linux, Python 3.12.3, numpy 2.5.3, polars 1.44.2): lint clean, 37 tests pass, default dataset (5,000 customers, 200,000 rows) generated in 1.3 s with about 194 MB peak memory and 3.5 MB on disk. Windows and CI for Phase 2: TO VERIFY.
- **Data design:** observable tables, noisy delayed labels (`labels.parquet`) and hidden `ground_truth/` are separate (ADR-007). Device ids are permuted. Labels need `label_available_at <= T`. Runtime deps now: numpy, polars.
- **Open items:** commit author email check (`git log -3 --format="%an <%ae>"`) never reported; `.wslconfig` not yet created (needed by Phase 12); CI #2 (line endings) result not reported.
- **Rules:** one phase at a time; never fabricate results; no `terraform apply` without approval; synthetic data only; "M-Pesa-inspired", no logos.
