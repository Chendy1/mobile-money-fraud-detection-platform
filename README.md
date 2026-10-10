# Mobile Money Transaction Risk Intelligence Platform

An **M-Pesa-inspired** mobile-money transaction risk scoring platform, built as a local-first, near-zero-cost portfolio project.

> **Disclaimer.** This is an independent simulation. It is **not affiliated with, endorsed by, or connected to Safaricom or M-Pesa**. It uses **entirely synthetic data**. No real customer or transaction data is requested, scraped, or used. Metrics on synthetic data demonstrate engineering capability, not real-world fraud-detection performance.

**Status:** Phase 6 complete (features). Models come in later phases. See [docs/roadmap.md](docs/roadmap.md).

## Business problem
Mobile-money providers must stop fraud (SIM-swap, social engineering, mule accounts, agent cash-out) without blocking legitimate customers. The platform outputs a risk **score** (probability) and a LOW / MEDIUM / HIGH level with an explanation. It never claims certainty. Thresholds come from an explicit cost model (false-positive friction vs missed-fraud loss).

## Planned capabilities
Synthetic data generator, data lake (raw/validated/cleaned), data quality checks, EDA, point-in-time-safe features, model comparison (rules, Logistic Regression, Random Forest, LightGBM), SHAP explainability, MLflow tracking, FastAPI inference, event simulation, Docker Compose, monitoring and drift detection, gated retraining, Streamlit dashboard, CI, and optional AWS design (Terraform, plan-only).

## Stack (summary)
Python 3.12, Polars, DuckDB + Parquet, scikit-learn, LightGBM, SHAP, MLflow, FastAPI, Streamlit, Evidently, Prometheus + Grafana, Docker Compose, pytest, ruff, GitHub Actions, Terraform (optional). Rationale: [docs/decisions.md](docs/decisions.md).

## Local setup (Windows PowerShell)
Run from the repository root. Requires Python 3.11+ and Git.
```powershell
.\scripts\dev.ps1 setup    # creates .venv and installs the project with dev and EDA tools
.\scripts\dev.ps1 test     # runs pytest
.\scripts\dev.ps1 lint     # ruff check + format check
```
If script execution is blocked: `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`.
Linux/macOS: `make setup`, `make test`, `make lint`.

## Data generation
Reproducible, seeded, synthetic. Run from the repository root (after `.\scripts\dev.ps1 setup`):
```powershell
.\scripts\dev.ps1 generate
# or with options:
.\.venv\Scripts\python -m mmrisk.generation --num-customers 5000 --num-transactions 200000 `
  --fraud-rate 0.005 --seed 42 --start-date 2025-01-01 --end-date 2025-12-31 --output-dir data/raw
```
Writes Parquet tables, noisy delayed labels, hidden ground truth and `manifest.json` to `data/raw/` (git-ignored; regenerate any time). Details: [data dictionary](docs/data-dictionary.md).

## Ingestion (data lake layers)
Builds raw, validated, quarantine and cleaned layers from the generated files. Safe to re-run: unchanged files are skipped.
```powershell
.\scripts\dev.ps1 ingest                       # data/raw -> data/processed
.\.venv\Scripts\python -m mmrisk.ingestion status   # ledger + consistency check
```
See [data dictionary](docs/data-dictionary.md#data-lake-layers-phase-3) and [ADR-008](docs/adr/ADR-008-layered-ingestion.md). Problems? [Troubleshooting](docs/troubleshooting.md).

## Data quality
Bad rows are quarantined with a reason during ingestion; a separate command checks the whole lake and reports PASS / WARN / FAIL.
```powershell
.\scripts\dev.ps1 quality                                  # show WARN / FAIL checks
.\.venv\Scripts\python -m mmrisk.validation check --verbose   # show every check
```
Rules, thresholds and "what happens when bad data enters the system?": [docs/data-quality.md](docs/data-quality.md).

## Exploratory data analysis
Seven figures, each answering a stated question, plus a Markdown report in which every number is computed from the lake (nothing is typed by hand). Label statistics use only transactions old enough to have final labels. A clearly marked optional section compares the observed labels with the hidden ground truth.
```powershell
.\scripts\dev.ps1 eda        # writes docs/eda-report.md and docs/figures/eda/*.png
.\.venv\Scripts\python -m mmrisk.eda --no-truth   # skip the ground-truth audit
```
Read the report: [docs/eda-report.md](docs/eda-report.md). Design: [ADR-010](docs/adr/ADR-010-eda-report-design.md).

## Features
One feature library, used for training now and for scoring later. Every feature uses only the transaction and strictly earlier history (no labels, no future rows), and the online path runs the same code as the batch path.
```powershell
.\scripts\dev.ps1 features                                # data/features/transaction_features.parquet
.\.venv\Scripts\python -m mmrisk.features audit            # leak-smell check on matured labels
.\.venv\Scripts\python -m mmrisk.features catalog          # regenerate docs/feature-catalog.md
```
28 features (velocity, baseline, novelty, inbound-transfer and time features): [feature catalog](docs/feature-catalog.md). Design and what was left out on purpose: [ADR-011](docs/adr/ADR-011-shared-point-in-time-features.md).

## Documentation
- [Architecture](docs/architecture.md)
- [Decisions (ADR index)](docs/decisions.md)
- [Roadmap](docs/roadmap.md)
- [Threat model](docs/threat-model.md)
- [Data quality](docs/data-quality.md)
- [EDA report](docs/eda-report.md)
- [Feature catalog](docs/feature-catalog.md)
- [Data dictionary](docs/data-dictionary.md)
- [Testing](docs/testing.md)
- [Troubleshooting](docs/troubleshooting.md)
- [Project state](docs/PROJECT_STATE.md)

## Results, API, demo
To be added as phases are completed. Nothing is claimed until it has been run and verified.

## License
Apache License 2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE). Copyright 2026 Clyde Adili.

## Author
Clyde Adili, GitHub: [Chendy1](https://github.com/Chendy1)
