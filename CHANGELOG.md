# Changelog
Format follows [Keep a Changelog](https://keepachangelog.com/). Versioning: SemVer.

## [Unreleased]

### Added
- Phase 2: reproducible synthetic data generator (`python -m mmrisk.generation`): customers, merchants,
  agents, devices, counties, transactions; fraud episodes (8 patterns); noisy, delayed labels;
  hidden ground truth; run manifest; data dictionary; ADR-007; 32 new tests.
- Phase 1: repository skeleton, `src/mmrisk` package, `pyproject.toml`, Apache-2.0 license,
  `.gitignore`, `.env.example`, smoke tests, lint/test GitHub Actions workflow, PowerShell dev script.
- Phase 0: architecture, roadmap, threat model, ADR-001 to ADR-005.
