# Testing
- Framework: pytest. Linting and formatting: ruff.
- Layout: `tests/unit`, `tests/integration`, `tests/data`, `tests/api`.
- Run on Windows: `.\scripts\dev.ps1 test` and `.\scripts\dev.ps1 lint`. Linux/macOS: `make test`, `make lint`.
- CI (`.github/workflows/ci.yml`): ruff check, ruff format check, pytest with coverage. No cloud credentials.
- Rule: each phase adds tests, including failure-path tests. Coverage is monitored, not chased.
