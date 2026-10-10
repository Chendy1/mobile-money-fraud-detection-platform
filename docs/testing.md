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

## Phase 4 tests
- `tests/unit/test_dq_rules.py`: every domain rule fires on a violating row and stays silent on a valid one (a guard fails if a rule has no test); configuration is checked against the generator's reference data so the two cannot drift apart.
- `tests/data/test_data_quality.py`: the data is deliberately corrupted in specific ways and the test asserts the right quarantine reason or check result. Covers 11 row defects, threshold boundaries (quarantine, duplicates, conflicts, orphans), dtype drift, a rejected batch followed by a fix, nulls and duplicates injected into stored files, a deleted partition, labels known before their transaction, freshness, missing days, a volume spike, tiny datasets, the JSON report and CLI exit codes.
- A dedicated test asserts that freshly generated data produces no WARN and no FAIL (no false positives).
- Mutation-checked: neutering a rule, making the referential or label-timing check never fail, over-strict category rule, and a raised threshold each make specific tests fail.
- The full suite takes roughly 1.5 minutes on a single core (it builds many small lakes); expect a few minutes on a slower laptop.

## Phase 5 tests
- `tests/integration/test_eda.py` builds a small dataset, ingests it and checks the analysis layer.
- **Brute-force cross-check:** `prior_n`, `prior_avg`, `secs_since_prev` and `n_10min` are recomputed with plain Python loops for a sample of customers and must match the DuckDB window results.
- **Leakage:** dropping each customer's later rows must not change any earlier row's features; a global time cut must not change the "last inbound transfer" feature. Mutation-checked: including the current row in a window, a window that looks forward, and a reversed ASOF join each make a test fail.
- Signal table bounds, class balance on matured rows only, hidden recent labels, Spearman symmetry, Wilson interval, the report (7 figures, computed numbers, audit present or skipped), an empty lake raising a clear error, and the CLI exit codes.
- The EDA tests are skipped if `duckdb` or `matplotlib` is missing; install with `pip install -e ".[dev,eda]"` (`dev.ps1 setup` does).

## Phase 6 tests
- `tests/unit/test_features_unit.py`: every feature checked by hand on tiny frames with known answers, including boundaries (a cash-out at exactly 7,200 s and at 7,201 s; a transaction exactly 10 minutes earlier), same-timestamp ties, history that is too short, inputs in any order, bad input, and the audit's AUC and planted-leak detection.
- `tests/integration/test_features.py` builds a small dataset and checks: a global time cut and a per-customer cut leave earlier rows' features unchanged (leakage); shuffled input gives identical output; 500 sampled transactions streamed through the online path match the batch features; the Polars features equal the independent DuckDB definitions from Phase 5 exactly; the training table contains only labels known at the chosen time and never treats recent unlabelled rows as legitimate; features do not change with the label time; no single feature separates the classes suspiciously well; the CLI is deterministic; the committed feature catalog matches the registry.
- Mutation-checked: a window that includes the current row, an exact-match inbound join, a forward-looking lookup, a future-based count, a new-device flag without history, a 30-day sum that includes the current row, an online path that ignores inbound transfers, and a wrong device sequence each make specific tests fail. A leak that only uses the current row (not the future) is invisible to truncation tests and is caught by the unit tests and the DuckDB cross-check, which is why all three exist.
