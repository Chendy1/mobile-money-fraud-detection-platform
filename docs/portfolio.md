# Portfolio guide (Phase 26)

How to present this project, to whom, and what each claim rests on. Everything here points to a file, a test or a command; nothing is a recollection. The resume wording is in [resume.md](resume.md), the interview answers in [interview-prep.md](interview-prep.md).

Author: Clyde Adili ([Chendy1](https://github.com/Chendy1)). Repository: https://github.com/Chendy1/mobile-money-fraud-detection-platform

## The project in three lengths
**One line.** A local-first, M-Pesa-style fraud-risk scoring platform on synthetic data: data lake, point-in-time-safe features, calibrated models with an expected-cost decision rule, a secured scoring API, monitoring, drift detection, gated retraining, a hardened Docker deployment, and a plan-only AWS design.

**Thirty seconds.** I built an end-to-end fraud-risk system for mobile-money transactions, phase by phase, on synthetic data so that nothing real is at risk. The part I care about most is honesty in the evaluation: a time split with a label gap, intervals that resample whole customers, and a result that says LightGBM did not beat a random forest. The same feature code scores in training and in the API, the security boundaries are tested from outside, and the cloud design is plan-only with a cost estimator that uses prices read from AWS pages.

**Two minutes.** Add, in this order: (1) the problem: labels arrive up to 45 days late and about a tenth of fraud-labelled rows are not truly fraud, so a random split would flatter any model; (2) the evaluation design and the negative result; (3) calibration and a cost rule that turns a probability and an amount into ALLOW, REVIEW or BLOCK under costs I state are assumed, with a sensitivity table; (4) the engineering around the model: one feature library for training and serving, an API with hashed keys, TLS and an audit log, a stream simulator with seeded faults, PSI drift detection against a frozen reference, gated retraining with explicit promotion, and an open-loop load tester; (5) the operational layer: a hardened compose file with a preflight, a verifier that checks the boundaries from outside, backup and restore with a hash manifest, and CI with four jobs; (6) what is not done: nothing runs in the cloud, the data is synthetic, and the list in [limitations.md](limitations.md).

## A ten-minute path for a reviewer
1. [README](../README.md): status, results table with its caveat, architecture diagram.
2. [verification-status.md](verification-status.md): what was checked in CI, on my machine and in a sandbox, and what was not. Read this before trusting anything else.
3. [demo.md](demo.md) or `.\scripts\dev.ps1 demo`: five minutes of real output.
4. Two ADRs that show the thinking: [ADR-012](adr/ADR-012-time-split-and-evaluation.md) (evaluation) and [ADR-013](adr/ADR-013-calibration-and-cost-based-decisions.md) (calibration and costs, including the negative result).
5. [limitations.md](limitations.md).

## What it demonstrates, and the evidence
"Status" says where the claim was checked: **CI** (GitHub Actions, Ubuntu), **author** (my Windows machine), **sandbox** (the assistant's Linux container), **design** (documents or code never run for real).

| Capability | Where | Evidence | Status |
|---|---|---|---|
| Synthetic data with delayed, noisy labels and separate ground truth | `mmrisk.generation`, [ADR-001](adr/ADR-001-synthetic-data.md), [ADR-007](adr/ADR-007-labels-and-truth-separation.md) | tests; [eda-report.md](eda-report.md) quantifies the label noise | CI |
| Layered lake with quarantine, ledger and two-tier quality checks | `mmrisk.ingestion`, `mmrisk.validation`, [data-quality.md](data-quality.md) | tests; the demo's `quality` step | CI, sandbox |
| Leak-safe features, one library for training and serving | `mmrisk.features`, [ADR-011](adr/ADR-011-shared-point-in-time-features.md), [feature-catalog.md](feature-catalog.md) | truncation tests, offline/online parity test, the demo's `apicheck` step | CI |
| Honest evaluation (time split with gap, customer bootstrap, PR-AUC) | `mmrisk.models`, [ADR-012](adr/ADR-012-time-split-and-evaluation.md), [model-card.md](model-card.md) | the generated model card | sandbox |
| Calibration and an expected-cost decision rule with sensitivity | [ADR-013](adr/ADR-013-calibration-and-cost-based-decisions.md), [cost-model.md](cost-model.md) | the model card; costs are ASSUMED | sandbox |
| Exact explanations cross-checked three ways | [ADR-014](adr/ADR-014-explanations-and-model-card.md), [model-explanations.md](model-explanations.md) | tests | CI |
| Scoring API with strict input, hashed keys with roles, TLS, rate limits, audit log | `mmrisk.api`, `mmrisk.security`, [security.md](security.md), [threat-model.md](threat-model.md) | unit and integration tests; `deploy verify` 12 of 12 against the container | CI, author |
| Stream simulation with seeded faults and an accounting identity | `mmrisk.streaming`, [ADR-018](adr/ADR-018-streaming-simulation.md) | the demo's `simulate` step | CI, sandbox |
| Drift detection (PSI against a frozen reference) | `mmrisk.drift`, [ADR-020](adr/ADR-020-drift-detection.md) | the demo's `drift_shift` step; a measured limit: a 50% amount rise goes unnoticed | sandbox |
| Gated retraining, file-based registry, explicit promotion | `mmrisk.retrain`, `mmrisk.registry`, [ADR-021](adr/ADR-021-gated-retraining.md) | tests | CI |
| Open-loop load tester, latency from the intended start | `mmrisk.loadtest`, [ADR-024](adr/ADR-024-load-testing.md), [load-testing.md](load-testing.md) | one run each on 2 shared CPUs and on the author's laptop | sandbox, author |
| Hardened compose, preflight, outside verifier, backup and restore | `mmrisk.deploy`, [deployment.md](deployment.md), [ADR-025](adr/ADR-025-local-deployment.md) | API container ran on Docker Desktop; `docker compose config` and the preflight rules pass in CI | author, CI |
| CI with four jobs | `.github/workflows/ci.yml` | green run 42930127418 | CI |
| AWS design, plan-only Terraform, sourced cost estimator | [aws-design.md](aws-design.md), [terraform.md](terraform.md), [cloud-costs.md](cloud-costs.md) | `terraform validate` passes in CI; **never planned or applied**; costs are lower bounds | design, CI |
| A demo that checks its own output | `mmrisk.demo`, [demo.md](demo.md), [ADR-029](adr/ADR-029-guided-demo.md) | 12 of 12 steps in the sandbox; not yet run on Windows | sandbox, CI |

Skills this supports, honestly qualified: Python (Polars, DuckDB, scikit-learn, LightGBM, FastAPI, Streamlit, pytest), data modelling and data quality, ML evaluation and calibration, API security basics, Docker and Compose, GitHub Actions, observability (Prometheus metrics endpoint; the Prometheus and Grafana containers have **not** been run), Terraform and AWS **at design level only**, technical writing (29 decision records).

## Setting up the GitHub page
These are suggestions for you to apply; the assistant cannot see or change your GitHub settings.
- **About text (under 350 characters):** "Local-first, M-Pesa-style fraud-risk scoring platform on synthetic data: leak-safe features, calibrated LightGBM with an expected-cost decision rule, secured FastAPI service, drift detection, gated retraining, hardened Docker deployment, plan-only AWS Terraform. Not affiliated with Safaricom."
- **Topics:** `fraud-detection`, `mobile-money`, `machine-learning`, `lightgbm`, `fastapi`, `mlops`, `polars`, `duckdb`, `docker`, `terraform`, `synthetic-data`.
- **Pin** the repository. The README's first screen already carries status, the caveat on the results and the architecture diagram.
- **Screenshots:** none exist. The dashboard has not been viewed in a browser by the assistant; take your own screenshots of what you have confirmed, and say which data they show (synthetic).
- **Release notes** come in Phase 28.

## How it was built (say it plainly)
The project was built phase by phase with an AI coding assistant (Claude), which wrote most of the code and documents; the author set the scope and the working rules (synthetic data only, nothing claimed that was not verified, a list of what was not checked after every phase), ran each phase on a Windows machine, reported the results and failures, and decided what came next. Neither the repository nor its commit messages currently say this. Employers increasingly ask, and an unstated answer reads worse than a stated one, so consider adding a short note to the README, for example:

```markdown
*Built with an AI coding assistant under my direction. I set the scope and rules, ran and verified each phase on my own machine, and can explain every decision in the [decision records](docs/decisions.md).*
```
(The link is written for the README at the repository root.)

That is your decision to make; the assistant has not added it. Whatever you choose, do not describe the code as written by you alone, and prepare the answer in [interview-prep.md](interview-prep.md).

## What to say if asked what is missing
Use the list in [limitations.md](limitations.md); the short form: real data, a cloud deployment, a Windows run of the demo, a Prometheus and Grafana run, a Terraform plan, and a repeat of the evaluation across seeds.
