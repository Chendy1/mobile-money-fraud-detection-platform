# Project state (paste at the start of a new chat)

- **Repo:** mobile-money-fraud-detection-platform; GitHub user: Chendy1; author: Clyde Adili; license: Apache-2.0
- **Current phase:** 1 complete (pending the user's confirmation that tests pass on Windows). Next: Phase 2, synthetic data generator, on "CONTINUE TO PHASE 2".
- **Environment:** Windows, PowerShell; 8 GB RAM; i5-6300U (2 cores / 4 threads); ~23.9 GB free disk; Python 3.12.10; conda not on PATH; Docker Desktop installed (9 containers running, build cache pruned).
- **Machine rules:** native first; cap WSL2 memory with `.wslconfig` (3 GB, 2 processors); stop unused containers before training; datasets 200k-500k rows.
- **Layout:** installable package `src/mmrisk/` (ADR-006); `training/` and `apps/` entry points; Windows task runner `scripts/dev.ps1`; Makefile for Linux/macOS.
- **Stack decided:** Python 3.12 + .venv, Polars, DuckDB + Parquet, scikit-learn, LightGBM, SHAP, MLflow, FastAPI, Streamlit, Evidently, Prometheus + Grafana, Docker Compose, pytest, ruff, GitHub Actions, Terraform (optional). Runtime dependencies are added per phase.
- **Rejected:** Redpanda/Kafka (in-process queue), Kubernetes, Airflow, Spark, Feast, deep learning.
- **Verified:** Phase 1 tests and lint passed in the assistant's Linux sandbox only. Windows and GitHub Actions results: TO VERIFY.
- **Open items:** commit email must be the GitHub noreply address; `.wslconfig` not yet confirmed; architecture diagram direction (LR to TB) commit not yet confirmed.
- **Rules:** one phase at a time; never fabricate results; no `terraform apply` without approval; synthetic data only; "M-Pesa-inspired", no logos.
