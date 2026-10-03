# Project state (paste at the start of a new chat)

- **Repo:** mobile-money-fraud-detection-platform; GitHub user: Chendy1
- **Current phase:** 0 complete (docs only). Next: Phase 1, on "CONTINUE TO PHASE 1".
- **Environment:** Windows, PowerShell; 8 GB RAM; i5-6300U (2 cores / 4 threads); ~23.9 GB free disk (4.8 GB before cleanup); Python 3.12.10; conda not on PATH; Docker Desktop installed (9 containers running, build cache pruned).
- **Machine rules:** native first; cap WSL2 memory with `.wslconfig` (3 GB, 2 processors); stop unused containers before training.
- **Stack decided:** Python 3.12 + .venv, Polars, DuckDB + Parquet, scikit-learn, LightGBM, SHAP, MLflow, FastAPI, Streamlit, Evidently, Prometheus + Grafana, Docker Compose, pytest, ruff, GitHub Actions, Terraform (optional).
- **Rejected:** Redpanda/Kafka (in-process queue), Kubernetes, Airflow, Spark, Feast, deep learning.
- **Open decisions:** license (not MIT; Apache-2.0, GPL-3.0, or all-rights-reserved); commit email (use GitHub noreply).
- **Verified so far:** nothing beyond environment checks. No code has been run.
- **Rules:** one phase at a time; never fabricate results; no `terraform apply` without approval; synthetic data only; "M-Pesa-inspired", no logos.
