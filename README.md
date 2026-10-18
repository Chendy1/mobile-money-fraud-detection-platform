# Mobile Money Transaction Risk Intelligence Platform

An **M-Pesa-inspired** mobile-money transaction risk scoring platform, built as a local-first, near-zero-cost portfolio project.

> **Disclaimer.** This is an independent simulation. It is **not affiliated with, endorsed by, or connected to Safaricom or M-Pesa**. It uses **entirely synthetic data**. No real customer or transaction data is requested, scraped, or used. Metrics on synthetic data demonstrate engineering capability, not real-world fraud-detection performance.

**Status:** Phase 9 complete (explanations and a model card). MLflow tracking and a single training command come next. See [docs/roadmap.md](docs/roadmap.md).

## Business problem
Mobile-money providers must stop fraud (SIM-swap, social engineering, mule accounts, agent cash-out) without blocking legitimate customers. The platform outputs a risk **score** (probability) and a LOW / MEDIUM / HIGH level with an explanation. It never claims certainty. Thresholds come from an explicit cost model (false-positive friction vs missed-fraud loss).

## Planned capabilities
Synthetic data generator, data lake (raw/validated/cleaned), data quality checks, EDA, point-in-time-safe features, model comparison (rules, Logistic Regression, Random Forest, LightGBM), SHAP explainability, MLflow tracking, FastAPI inference, event simulation, Docker Compose, monitoring and drift detection, gated retraining, Streamlit dashboard, CI, and optional AWS design (Terraform, plan-only).

## Stack (summary)
Python 3.12, Polars, DuckDB + Parquet, scikit-learn, LightGBM, SHAP, MLflow, FastAPI, Streamlit, Evidently, Prometheus + Grafana, Docker Compose, pytest, ruff, GitHub Actions, Terraform (optional). Rationale: [docs/decisions.md](docs/decisions.md).

## Local setup (Windows PowerShell)
Run from the repository root. Requires Python 3.11+ and Git.
```powershell
.\scripts\dev.ps1 setup    # creates .venv and installs the project with dev, EDA and ML tools
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

## Baseline models
A leak-safe time split (train, a 45-day label gap, validation, test), then three baselines scored on the same held-out period: hand-written rules, logistic regression and a random forest. The primary metric is PR-AUC (a random scorer scores the fraud rate); every metric has a 95% interval that resamples whole customers; the report also shows what a fixed review budget buys and how much a careless split would have inflated the scores.
```powershell
.\scripts\dev.ps1 train                                   # about 3 to 10 minutes; writes docs/model-baselines.md
.\.venv\Scripts\python -m mmrisk.models train --quick     # smaller grid, faster
```
Results and caveats: [docs/model-baselines.md](docs/model-baselines.md). Design: [ADR-012](docs/adr/ADR-012-time-split-and-evaluation.md). Trained models are written to `models/baselines/` (git-ignored). Results are on synthetic data and say nothing about real fraud.

## Advanced model and cost-based decisions
LightGBM is tuned on the validation window and compared with the baselines. Every scorer is calibrated (Platt scaling, fitted on validation only), then an expected-cost rule picks ALLOW, REVIEW or BLOCK (risk level LOW, MEDIUM, HIGH) from the probability and the amount. The costs are **assumptions**, reported with sensitivity tables, intervals and a comparison with a fixed 0.5 cut-off.
```powershell
.\scripts\dev.ps1 train          # Phase 7 baselines first (the advanced step reuses them)
.\scripts\dev.ps1 advanced       # writes docs/model-advanced.md and four figures
.\.venv\Scripts\python -m mmrisk.models advanced --quick --bootstrap 100   # faster
```
Results and caveats: [docs/model-advanced.md](docs/model-advanced.md). Assumptions: [docs/cost-model.md](docs/cost-model.md). Design: [ADR-013](docs/adr/ADR-013-calibration-and-cost-based-decisions.md). The decision bundle is written to `models/advanced/` (git-ignored). Synthetic data only; the savings say nothing about real money.

## Explanations and model card
Exact tree explanations (TreeSHAP) for the LightGBM model, cross-checked by shuffling features, by refitting without them, and by comparing two time windows. Reason codes for flagged transactions, a table of who gets flagged, and a model card whose numbers are read from the saved results. The report states where the measures disagree.
```powershell
.\scripts\dev.ps1 explain        # needs `advanced` first; about 2 to 6 minutes; writes docs/model-explanations.md
.\scripts\dev.ps1 card           # writes docs/model-card.md from the saved results
.\.venv\Scripts\python -m mmrisk.models explain --quick   # faster, fewer shuffles and one seed
```
Results: [docs/model-explanations.md](docs/model-explanations.md). Model card: [docs/model-card.md](docs/model-card.md). Design: [ADR-014](docs/adr/ADR-014-explanations-and-model-card.md). In code, `RiskModel.explain(df)` returns reason codes. Explanations describe the model, not causes; synthetic data only.

## Documentation
- [Architecture](docs/architecture.md)
- [Decisions (ADR index)](docs/decisions.md)
- [Roadmap](docs/roadmap.md)
- [Threat model](docs/threat-model.md)
- [Data quality](docs/data-quality.md)
- [EDA report](docs/eda-report.md)
- [Feature catalog](docs/feature-catalog.md)
- [Baseline models](docs/model-baselines.md)
- [Advanced model and decisions](docs/model-advanced.md)
- [Cost model (assumed)](docs/cost-model.md)
- [Model explanations](docs/model-explanations.md)
- [Model card](docs/model-card.md)
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
