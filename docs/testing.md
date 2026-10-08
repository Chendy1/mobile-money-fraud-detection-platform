# Testing
- Framework: pytest. Linting and formatting: ruff.
- Layout: `tests/unit`, `tests/integration`, `tests/data`, `tests/api`.
- Run on Windows: `.\scripts\dev.ps1 test` and `.\scripts\dev.ps1 lint`. Linux/macOS: `make test`, `make lint`.
- CI (`.github/workflows/ci.yml`): ruff check, ruff format check, pytest with coverage. No cloud credentials.
- Rule: each phase adds tests, including failure-path tests. Coverage is monitored, not chased.

## Phase 2 tests
- `tests/unit/test_generation_config.py`: config validation, probability tables, CLI behaviour.
- `tests/data/test_generated_data.py`: row count and exact fraud rate, schema, referential integrity, no fraud columns in observable tables, noisy and delayed labels, overlap of every fraud signal with legitimate behaviour, no perfect single-signal rule, device-id leakage, determinism for a given seed.
- Tests were checked by mutation: breaking the generator on purpose (perfect labels, a leaked fraud column, no legitimate new-device events, unpermuted device ids) makes the relevant test fail.
- Coverage is monitored, not chased.

## Phase 3 tests
- `tests/unit/test_ingestion_units.py`: table specs, validation gates (cast failures, nulls, blanks, fractional amounts), standardisation, deduplication, atomic writes, ledger.
- `tests/integration/test_ingestion_pipeline.py`: end to end on generated data. Covers idempotent re-runs, replays under a new file name, identical lakes from identical input, rebuild after a lost ledger, a simulated crash mid-batch (unfinished data stays invisible and the re-run matches a clean run), overlapping batches with duplicates, conflicts, null keys and unparseable amounts, whole-batch rejection (missing column, extra column, unreadable file), dimension snapshots, ground truth never entering the lake, reconciliation detecting a deleted partition or tampered raw file, and CLI exit codes.
- Mutation-checked: disabling deduplication, the ledger skip, the null-key gate, or batch visibility each makes specific tests fail.
