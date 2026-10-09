# Data quality

All data in this project is synthetic. This document describes how the pipeline protects itself from bad data, and answers: **what happens when bad data enters the system?**

Two tiers work together:
1. **Row rules** run inside ingestion. A violating row is quarantined with a reason and never reaches the cleaned layer.
2. **Dataset checks** run afterwards (`python -m mmrisk.validation check`) and produce a PASS / WARN / FAIL report for the whole lake.

Structural gates (Phase 3) come first: unreadable file, wrong columns, values that cannot be cast, missing required fields.

## What happens when bad data enters the system?

| What arrives | What the pipeline does | Where you see it |
|---|---|---|
| A file that cannot be read, or has missing or unexpected columns | Whole batch **rejected**. Nothing reaches validated or cleaned. The raw copy is kept for audit. | `ingest` prints `REJECTED` and the reason; check `batches_rejected` = FAIL (WARN once the table was loaded successfully afterwards) |
| A row with a value that cannot be cast (for example `abc` as an amount) or a missing required field | Row **quarantined** (`CAST_FAILED`, `NULL_REQUIRED`, `EMPTY_REQUIRED`) | quarantine layer; `quarantine_rate` |
| A row that breaks a domain rule (amount of 0 or above the cap, future or ancient timestamp, unknown category, inconsistent counterparty, self-transfer, malformed ID) | Row **quarantined** as `RULE:<name>` | quarantine layer; `quarantine_rate` |
| The same row again (a replay) | Dropped and **counted**; the first copy stays | `duplicate_rate` |
| The same key with different content | Later copy **quarantined** as a conflict; the first copy stays | `conflicting_duplicates` |
| A batch whose column types changed but still cast cleanly (amount arrives as text) | Loaded normally, drift **warned** | `schema_drift` = WARN |
| A reference to something that does not exist (unknown customer, device, county, counterparty, label for an unknown transaction) | Loaded; **reported** | `referential_*` = WARN for a tiny share, FAIL above 0.1% |
| A label timestamp at or before its transaction | Loaded; **reported** (leakage risk) | `label_timing` = FAIL |
| Missing days or an abnormal daily volume | Reported | `daily_volume` (FAIL for missing days, WARN for unusually low or high days) |
| Stale data (when `--as-of` is given) | Reported | `freshness` = FAIL when the lag exceeds the limit |
| Files changed or deleted by hand | Reported | `row_accounting` = FAIL |

**Knock-on effects are real.** Quarantining a transaction leaves its label without a transaction, so a quarantined transaction usually also produces a `referential_labels` warning. Read the quarantine first, then the checks.

**Clean data passes.** A test asserts that freshly generated data produces zero WARN and zero FAIL, so the rules cannot silently start rejecting good data.

## Row rules

| Table | Rules |
|---|---|
| transactions | `transaction_id_format`, `customer_id_format`, `device_id_format`, `amount_positive`, `amount_within_cap`, `timestamp_plausible`, `timestamp_not_future`, `allowed_transaction_type`, `allowed_channel`, `counterparty_consistent`, `counterparty_id_format`, `no_self_transfer` |
| labels | `transaction_id_format`, `label_value_valid`, `timestamp_plausible`, `timestamp_not_future` |
| customers | `customer_id_format`, `allowed_segment`, `registration_plausible`, `registration_not_future` |
| merchants | `merchant_id_format`, `allowed_merchant_type` |
| agents | `agent_id_format` |
| devices | `device_id_format`, `allowed_device_type` |
| counties | `latitude_in_range`, `longitude_in_range` |

The first matching rule names the reason. "Not future" compares against the ingestion time plus one day (clock skew and East Africa Time). Identifiers are trimmed and upper-cased before the rules run, so `txn000000001` is standardised, not rejected.
Assumptions: the amount cap (250,000 KES, TO VERIFY against real rules) and the approximate Kenya bounding box. Both live in `src/mmrisk/validation/config.py`, and a test keeps the cap equal to the generator's.

## Dataset checks and thresholds

| Check | PASS / WARN / FAIL |
|---|---|
| `table_loaded` | FAIL if a table has no completed batch |
| `batches_rejected` | FAIL if a batch was rejected and the table was not loaded afterwards; WARN if it was |
| `schema_drift` | WARN if a column arrived with a different type than expected |
| `quarantine_rate` | WARN above 1% of incoming rows, FAIL above 5% |
| `duplicate_rate` | WARN above 5% |
| `conflicting_duplicates` | WARN above 0, FAIL above 1% |
| `null_rates` | FAIL if a required column has any null or a counterparty column exceeds 25% |
| `key_uniqueness` | FAIL on duplicate keys in the cleaned layer |
| `referential_*` | WARN if orphans are 0.1% or fewer, FAIL above that, SKIPPED if a table is not loaded |
| `label_timing` | FAIL if any label is available at or before its transaction |
| `daily_volume` | FAIL for missing days; WARN below 0.25x or above 4x the median day; SKIPPED if the median is under 20 a day |
| `freshness` | needs `--as-of`; FAIL if the newest event is older than `--max-lag-hours` (default 24); otherwise SKIPPED |
| `row_accounting` | FAIL if files on disk disagree with the ledger |

Thresholds are starting points chosen for this synthetic dataset and are not calibrated on real traffic.

## Running it

```powershell
.\scripts\dev.ps1 quality                                            # summary of WARN/FAIL only
.\.venv\Scripts\python -m mmrisk.validation check --verbose          # every check
.\.venv\Scripts\python -m mmrisk.validation check --as-of 2025-12-31T23:59:59 --max-lag-hours 24
.\.venv\Scripts\python -m mmrisk.validation check --strict           # WARN also exits with 1
```
Exit codes: 0 = PASS or WARN, 1 = FAIL (or WARN with `--strict`), 2 = no lake found. The report is also saved to `data/processed/_meta/dq_report.json`.
Freshness is skipped by default because the generated data is historical; the real-time simulation (Phase 13) and monitoring (Phase 14) will enforce it.

## Responding to a failure
1. Run `check --verbose` and read the non-PASS rows.
2. Read the quarantine: `read_layer("data/processed", "quarantine", "<table>")`. `_reason` says why; `_row_json` holds the original row.
3. Fix the source, or accept the loss if the rows are genuinely invalid.
4. Re-ingest the corrected file. It has a new hash, so it loads normally; earlier good batches are not touched.
5. If the lake itself looks damaged (`row_accounting`), delete `data/processed` and re-run the ingestion; it is derived data.

## Limitations
- Rules are hand-written, not learned. They catch known defect types, not new ones; drift monitoring (Phase 15) covers distribution changes.
- Checks read the whole cleaned layer; this is fine at hundreds of thousands of rows, not at tens of millions.
- A rejected or quarantined row is not repaired automatically.
- Quarantine thresholds judge the share of *incoming* rows across all batches, so a bad batch can be diluted by many good ones; per-batch alerting arrives with monitoring.
