# Changelog
Format follows [Keep a Changelog](https://keepachangelog.com/). Versioning: SemVer.

## [Unreleased]

### Added
- Phase 11: local scoring API (`python -m mmrisk.api serve | check`, extra `api`). `POST /v1/score` returns the calibrated probability, the cost-based action, the risk level, assumed expected costs and reason codes; `/health`, `/ready`, `/v1/model`. Framework-free `ScoringService` (history in memory, repeat ids answered once, state unchanged on failure), strict input validation using the ingestion constants, 8 KB body limit, errors that do not echo input, request ids, loopback only unless `--allow-external`. `check` compares streamed with batch scoring and reports latency. `OnlineFeatureState.feature_frame` added (same features with the batch column types). `docs/threat-model.md` (the README link had pointed at a missing file), ADR-016, `dev.ps1 serve` and `apicheck`; 60 new tests.
- Phase 10: `python -m mmrisk.pipeline run` runs features, baselines, the advanced model, explanations and the model card in one process and records the run in a local MLflow store (data fingerprint from the ingestion ledger, git commit, library versions, about 100 metrics, reports, figures, model bundle, manifest). `compare` lists recent runs. Tracking is behind a `Tracker` interface (MLflow or in-memory), local only (remote addresses refused), records no user, host or path, marks failed and interrupted runs, and sends `--quick` runs to `models/quick` and `docs/quick`. Optional extra `tracking`; `dev.ps1 pipeline` and `compare`; ADR-015; 40 new tests.
- Phase 9: explanations and a model card. `python -m mmrisk.models explain` and `card`: exact TreeSHAP contributions
  from LightGBM (`LGBMModel.contributions`, summed to the 28 catalogue features), three views of importance
  (effect on all rows, effect on fraud-labelled rows, ranking lost when shuffled, singly and by group), stability
  across validation and test windows, refits without a feature on the validation window with several seeds,
  reason codes (`RiskModel.explain`), dependence plots, three worked examples chosen by fixed rules, an optional
  ground-truth audit of fraud patterns, a descriptive table of who gets flagged, `docs/model-explanations.md` with
  3 figures, a model card generated from the saved results (`docs/model-card.md`), ADR-014, an optional `explain`
  extra (`shap`, used only to cross-check the explanations in a test), and 42 new tests (the suite has 331).
  Finding: the three importance measures disagree, `segment` has a large average effect and removing it lowers
  validation PR-AUC, so reason codes that cite it need review before being shown to anyone.
- Phase 8: advanced model, calibration and cost-based decisions. `python -m mmrisk.models advanced`: LightGBM
  tuned on validation (staged rounds, no early stopping), Platt calibration fitted on validation only for
  every scorer, a three-action expected-cost rule (ALLOW/REVIEW/BLOCK mapped to LOW/MEDIUM/HIGH) from a
  frozen `CostModel` whose values are assumptions, realized-cost bootstrap, sensitivity tables, a
  comparison with a fixed 0.5 cut-off, an oracle floor, a `RiskModel` bundle (`assess`, `save`, `load`), an
  optional ground-truth audit, `docs/model-advanced.md` with 4 figures, `docs/cost-model.md`, ADR-013, and
  48 new tests (the whole suite has 289). The `ml` extra now includes `lightgbm`. Finding: LightGBM did not
  beat the random forest on ranking (differences not separable); the cost rule beat a fixed 0.5 cut-off.
- Phase 7: baseline models and evaluation. `python -m mmrisk.models train`: time-based split with a 45-day
  label gap (`make_split`), stateless matrix building with fixed category vocabularies, a constant
  baseline, an 8-rule baseline, logistic regression and random forest tuned on validation, PR-AUC as the
  primary metric, precision/recall at fixed review budgets with exact tie handling, customer-level
  bootstrap intervals and paired differences, a split-shortcut sensitivity check, an optional
  ground-truth audit, saved models and metadata, a computed report with 3 figures, ADR-012 and
  38 new tests (mutation-checked).
- Phase 6: point-in-time-safe features. `mmrisk.features` computes 28 features (velocity windows,
  30-day baseline, novelty flags, location jump, inbound-transfer and cash-out features, time and
  customer attributes) with one Polars implementation that strictly looks backwards in
  (timestamp, transaction_id) order. `OnlineFeatureState` scores one transaction at a time with the same
  code. `training_table(as_of)` joins features to the labels known at a chosen time. CLI:
  `python -m mmrisk.features build | audit | catalog`; generated `docs/feature-catalog.md`; ADR-011;
  32 new tests, including truncation (leakage) tests, offline/online parity, and a cross-check against
  the independent DuckDB definitions from Phase 5.
- Phase 5: exploratory data analysis. `python -m mmrisk.eda` reads the cleaned lake with DuckDB and writes
  seven question-driven figures and `docs/eda-report.md` (all numbers computed): class balance, label delay
  and maturity, time rhythm, hourly pattern, absolute vs relative amounts, nine behavioural signals with
  precision/recall/lift, rank correlations, group fraud rates with Wilson intervals, and an optional
  ground-truth audit. Prior-only window features are checked for leakage and against a brute-force
  recomputation. New `eda` optional dependency group (duckdb, matplotlib), `layer_files` in the ingestion
  reader, ADR-010, 14 new tests.
- Phase 4: data quality. 27 domain row rules enforced during ingestion (quarantined as `RULE:<name>`),
  dataset checks with a PASS/WARN/FAIL report (`python -m mmrisk.validation check`: table loading,
  rejected batches, schema drift, quarantine/duplicate/conflict rates, null rates, key uniqueness,
  referential integrity, label timing, daily volume, freshness, ledger consistency), source schema recorded
  in the ledger, `docs/data-quality.md`, ADR-009, 84 new tests.
- Phase 3: layered ingestion (`python -m mmrisk.ingestion run|status`): immutable raw copies, structural
  validation gates, quarantine with reasons, first-write-wins deduplication, daily Hive-style partitions,
  lineage columns, SHA-256 ledger for idempotent and crash-safe loads, cross-layer reconciliation, ADR-008,
  troubleshooting guide, 36 new tests.
- Phase 2: reproducible synthetic data generator (`python -m mmrisk.generation`): customers, merchants,
  agents, devices, counties, transactions; fraud episodes (8 patterns); noisy, delayed labels;
  hidden ground truth; run manifest; data dictionary; ADR-007; 32 new tests.
- Phase 1: repository skeleton, `src/mmrisk` package, `pyproject.toml`, Apache-2.0 license,
  `.gitignore`, `.env.example`, smoke tests, lint/test GitHub Actions workflow, PowerShell dev script.
- Phase 0: architecture, roadmap, threat model, ADR-001 to ADR-005.
