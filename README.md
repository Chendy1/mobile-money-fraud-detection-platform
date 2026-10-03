# Mobile Money Transaction Risk Intelligence Platform

An **M-Pesa-inspired** mobile-money transaction risk scoring platform, built as a local-first, near-zero-cost portfolio project.

> **Disclaimer.** This is an independent simulation. It is **not affiliated with, endorsed by, or connected to Safaricom or M-Pesa**. It uses **entirely synthetic data**. No real customer or transaction data is requested, scraped, or used. Metrics on synthetic data demonstrate engineering capability, not real-world fraud-detection performance.

**Status:** Phase 0 complete (architecture and plan). No code exists yet. See [docs/roadmap.md](docs/roadmap.md).

## Business problem
Mobile-money providers must stop fraud (SIM-swap, social engineering, mule accounts, agent cash-out) without blocking legitimate customers. The platform outputs a risk **score** (probability) and a LOW / MEDIUM / HIGH level with an explanation. It never claims certainty. Thresholds come from an explicit cost model (false-positive friction vs missed-fraud loss).

## Planned capabilities
Synthetic data generator, data lake (raw/validated/cleaned), data quality checks, EDA, point-in-time-safe features, model comparison (rules, Logistic Regression, Random Forest, LightGBM), SHAP explainability, MLflow tracking, FastAPI inference, event simulation, Docker Compose, monitoring and drift detection, gated retraining, Streamlit dashboard, CI, and optional AWS design (Terraform, plan-only).

## Stack (summary)
Python 3.12, Polars, DuckDB + Parquet, scikit-learn, LightGBM, SHAP, MLflow, FastAPI, Streamlit, Evidently, Prometheus + Grafana, Docker Compose, pytest, ruff, GitHub Actions, Terraform (optional). Rationale: [docs/decisions.md](docs/decisions.md).

## Documentation
- [Architecture](docs/architecture.md)
- [Decisions (ADR index)](docs/decisions.md)
- [Roadmap](docs/roadmap.md)
- [Threat model](docs/threat-model.md)
- [Project state](docs/PROJECT_STATE.md)

## Local setup, results, demo
To be added as phases are completed. Nothing is claimed until it has been run and verified.

## License
TO BE DECIDED (Phase 1).

## Author
GitHub: [Chendy1](https://github.com/Chendy1)
