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
