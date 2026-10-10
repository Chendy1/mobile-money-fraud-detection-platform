# Changelog
Format follows [Keep a Changelog](https://keepachangelog.com/). Versioning: SemVer.

## [Unreleased]

### Added
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
