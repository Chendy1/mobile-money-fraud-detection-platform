# Verification status

What has been checked, **where**, and what has not. Three places produced evidence, and they are not equivalent:

- **Sandbox**: the assistant's Linux container (2 shared CPUs, no Docker, no AWS). Tests, lint, mutation checks and measurements ran there.
- **Developer machine**: the author's Windows laptop (PowerShell, Docker Desktop). Only the results the author pasted back are listed.
- **CI**: GitHub Actions on `ubuntu-latest` (`.github/workflows/ci.yml`). The only independent run of the whole suite.

Nothing here is a claim about real fraud: all data is synthetic. "Passed" means the check ran and passed, not that the thing is correct for any real purpose.

## CI (GitHub Actions, Ubuntu)

Latest green run: **43552599791** (the Phase 28 push, covers everything through Phase 28). All four jobs passed. The run that fixed the Phase 20 failure was 42930127418; the pushes for Phases 24 to 27 were also green (43123415193, 43354557392, 43357296884, 43549492031).

| Job | What it runs | Result |
|---|---|---|
| `lint-and-test` | `ruff check`, `ruff format --check`, the secret scan, the full `pytest` suite | Passed |
| `docker-build` | builds the API and dashboard images; LightGBM import; both run as user 10001; `docker compose config` for the dev and the production-like file; the production-like file's own preflight rules | Passed |
| `terraform` | Terraform 1.9.8: `fmt -check`, `init -backend=false`, `validate` (provider downloaded, no credentials, no AWS call) | Passed |
| `dependency-audit` | `pip-audit` (set not to fail the build) | Ran; its findings were not reviewed here (see [security.md](security.md)) |

History worth knowing: CI was green through the Phase 19 commits, then **red on five consecutive pushes (Phase 20 onward)** because the `deploy` extra lacked the libraries the deploy commands import. The failure was in a CI step, not in the product, but it went unnoticed for days. Fixed in two commits; the first (adding `setup-python`) was a wrong guess and is documented as such in the CHANGELOG. A test now keeps the extra complete.

Annotations that remain in CI and are only warnings: Node.js 20 deprecation for `actions/checkout`, `setup-python` and `setup-terraform`; the `ubuntu-latest` label migrating to Ubuntu 26.

## Developer machine (Windows, Docker Desktop)

| Item | Result as reported |
|---|---|
| Phase 1 | lint and 5 tests passed |
| Phase 14 | 627 passed, 1 failed (a test that needed `sh`; fixed) |
| Phase 20 deploy commands | `init`, `preflight` run; integration test file: 13 passed after one test fix |
| Phase 20 API container | built, healthy; `deploy verify` **12 of 12 passed** against the container |
| Phase 20 dashboard container | first run crashed (`No module named 'joblib'`); image fixed; **rebuilt container not confirmed** |
| Phase 27 `dev.ps1 scorecard-check` | `scorecard check: ok (23 areas)` |
| Phases 2 to 13, 15 to 19, 21 to 23 | full-suite results on Windows **not reported** |

## Sandbox (Linux, no Docker)

| Item | Result |
|---|---|
| Full test suite | 1256 passed, 2 skipped in 5 min 27 s, run on 2026-11-14 with everything through Phase 28 present (the skips need `promtool` and a Terraform binary, absent in the sandbox). This is the only full-suite number from the sandbox; the counts in PROJECT_STATE are historical |
| Lint | `ruff check` and `ruff format --check` clean on the files changed in each phase |
| Mutation checks | each phase deliberately broke the code in specific ways and confirmed a test failed (counts are in the phase's CHANGELOG entry); this is a check on the tests, not a proof |
| Measurements (latency, load, memory) | one run each on 2 shared CPUs; see [load-testing.md](load-testing.md) for what they do and do not show |
| Guided demo (Phase 25) | `python -m mmrisk.demo run --yes` against a lake and a `--quick`-trained model: 12 of 12 steps ok in 37 s; its real commands also run in an integration test (13 tests) |
| Older and newer libraries | the suite also passed on older (Polars 1.12.0, scikit-learn 1.5.2, numpy 2.1.3, Python 3.13) and newer (Polars 2.0.0, scikit-learn 1.9.1) versions up to Phase 21; **not re-run for Phases 22 and 23** |

## Per-area view

[scorecard.md](scorecard.md) turns this page into one level per area (E0 to E5). It reads the CI run and the Windows suite recorded above, so when you record a new CI run or a new Windows run here, update `docs/scorecard.json` and run `python -m mmrisk.scorecard render`; a test fails if the page is stale.

## Not verified (TO VERIFY)

| Item | Why |
|---|---|
| Dashboard in a browser (Review queue, Health, Models tabs; `C000***` masking; login page look) | never viewed by the assistant; the author confirmed the Overview only, if at all |
| Dashboard container after the `joblib` fix | built in CI, not confirmed running on the author's machine |
| Prometheus scraping the TLS API and Grafana (`--profile monitoring`) | not run |
| Secret-file and audit-folder permissions on Linux and WSL hosts | not run (see [deployment.md](deployment.md)) |
| Terraform `plan` and `apply` | never run; `validate` only. No AWS account was used. Cost figures are estimates from sourced prices (lower bounds), not bills |
| `/app/aws-entrypoint.sh` | the ECS task definitions reference it; it is not written (a known gap, see [aws-design.md](aws-design.md)) |
| The demo on Windows and the `dev.ps1 demo*` tasks | never run in PowerShell; `python -m mmrisk.demo ...` is the same code and is the tested path (TO VERIFY) |
| Compose `simulator` service (profile `simulate`) | not run |
| Container image scanning, image digest pinning, Alertmanager | not done |
| Real-world fraud performance | not applicable: synthetic data only |
