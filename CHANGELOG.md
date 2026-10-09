# Changelog
Format follows [Keep a Changelog](https://keepachangelog.com/). Versioning: SemVer.

## [Unreleased]

### Added
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
