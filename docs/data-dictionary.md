# Data dictionary (synthetic data)

All data is **synthetic**. Nothing here is real customer, merchant or Safaricom/M-Pesa data.
Generated with `python -m mmrisk.generation` (see README). Files are Parquet, written to `data/raw/`.
Timestamps are naive **local East Africa Time** (assumed UTC+3, no timezone attached).

## Observable tables (inputs to the pipeline)

### transactions.parquet — one row per outgoing transaction initiated by `customer_id`
| Column | Type | Description |
|---|---|---|
| transaction_id | string | `TXN000000001`...; assigned in time order |
| customer_id | string | Initiating customer (`C000001`...) |
| timestamp | datetime(us) | Local time of the transaction |
| transaction_type | string | SEND_MONEY, PAYBILL, TILL, AGENT_WITHDRAWAL, AGENT_DEPOSIT, AIRTIME |
| amount_kes | int64 | Whole Kenyan shillings, 1 to 250,000 (cap is an assumption, TO VERIFY) |
| counterparty_type | string | CUSTOMER, MERCHANT, AGENT; null for AIRTIME |
| counterparty_id | string | Customer, merchant or agent id; null for AIRTIME |
| device_id | string | Device used (`D000001`...); numbering is random and carries no information |
| location_county | string | County where the transaction was initiated |
| channel | string | USSD or APP |

Recipients of SEND_MONEY are other customers (`counterparty_id`); there is no separate recipients table.
Inbound money is not a row for the recipient; it is visible as `counterparty_id` on the sender's row.

### customers.parquet
`customer_id`, `segment` (SALARIED, SMALL_BUSINESS, INFORMAL_WORKER, STUDENT), `home_county`, `registration_date` (always before the data period).

### merchants.parquet
`merchant_id`, `merchant_type` (PAYBILL or TILL), `category`, `county`.

### agents.parquet
`agent_id`, `county`.

### devices.parquet
`device_id`, `device_type` (SMARTPHONE or FEATURE_PHONE). There is deliberately no owner column; device ownership must be learned from behaviour.

### counties.parquet
`county`, `latitude`, `longitude` (approximate centroids, for simulation only).

## Labels (observed, imperfect)

### labels.parquet
| Column | Type | Description |
|---|---|---|
| transaction_id | string | Join key |
| label_is_fraud | int8 | 1 = reported as fraud, 0 = not reported |
| label_available_at | datetime(us) | When this label becomes known |

Label rules (assumptions): only 88% of true fraud is ever reported; 0.05% of legitimate transactions are wrongly reported; reported-fraud labels arrive after a delay (median about 3 days, 0.25 to 45 days); non-fraud labels are only confirmed after a 45-day dispute window.
**At any evaluation time T, only labels with `label_available_at <= T` may be used.** Recent transactions are unlabelled by design.

## Ground truth (analysis and testing only)

`ground_truth/` is **never** a model input.
- `transaction_truth.parquet`: `transaction_id`, `true_is_fraud`, `fraud_pattern`, `episode_id`.
- `customer_latent.parquet`: `customer_id`, `is_mule`, `vulnerability`.

## manifest.json
Run parameters, seed, generator version, library versions, true-fraud counts by pattern, and row counts plus SHA-256 of every file (lineage and integrity).

## Behaviour model (summary)
**Legitimate:** four customer segments with different activity, amounts, transaction mix and salary-day effects; favourite contacts, merchants and agents; round-figure amounts; day and hour profiles with a small night tail. Deliberately unusual but legitimate behaviour: emergencies (large, sometimes night-time, sometimes to new recipients), small-business bulk payment bursts, replacement phones and second devices, travel, and recipients cashing out after receiving money.

**Fraud** is injected as multi-transaction episodes. Each episode has a hidden sophistication value that makes some fraud mimic normal behaviour.
| Pattern | Idea |
|---|---|
| SIM_SWAP | New (sometimes shared) device, burst of transfers, often odd hours or a location jump |
| SOCIAL_ENGINEERING | Victim's own device and location; larger transfer to a new recipient |
| VELOCITY_BURST | 6 to 20 rapid transactions within minutes |
| BEHAVIORAL_SHIFT | Atypical transaction types and amounts over 1 to 3 days |
| STRUCTURING | Repeated amounts just under 10,000 KES (assumption) to the same recipient |
| AMOUNT_ANOMALY | Single transaction 6 to 25 times the customer's typical amount |
| LOCATION_JUMP | Transactions from a far county shortly after a normal one |
| MULE_CASHOUT | Mule account withdraws at an agent minutes after receiving a fraudulent transfer |

`fraud_rate` is the share of **true** fraud transactions (default 0.5%); the observed label rate differs because of label noise.

## Known limitations
- No account balances, no customers joining during the period, no inbound-only rows.
- Fraud episodes are injected after the legitimate stream, so fraud timing is independent of customers' activity levels.
- Attacker devices are reused across victims (fraud rings), which makes device-sharing features unrealistically useful.
- All distributions are invented. Metrics on this data demonstrate engineering capability, not real-world fraud-detection performance.
- Assumptions: amount cap, dispute window, detection rate, false-report rate, 10,000 KES structuring threshold, EAT timezone.

## Data lake layers (Phase 3)

`python -m mmrisk.ingestion run` reads the source files in `data/raw/` and builds the lake under `data/processed/` (git-ignored, derived data; safe to delete and rebuild).
Note the two meanings of "raw": `data/raw/` is the **source extract** written by the generator; `data/processed/raw/` is the **raw layer** of the lake.

```
data/processed/
  raw/<table>/ingest_date=YYYY-MM-DD/<batch_id>.parquet          verbatim copy of the source file
  validated/<table>/<partition>=YYYY-MM-DD/<batch_id>.parquet    rows that passed the gates (dimensions: <batch_id>.parquet)
  quarantine/<table>/<batch_id>.parquet                          rejected or conflicting rows
  cleaned/<table>/<partition>=YYYY-MM-DD/<batch_id>.parquet      deduplicated, standardised rows
  _meta/ledger.json                                              one record per batch
```
Partition column: `event_date` (transactions, from `timestamp`), `label_available_date` (labels). The partition value lives in the folder name; readers (`mmrisk.ingestion.read_layer`) turn it back into a column.
Tables ingested: counties, customers, merchants, agents, devices, transactions, labels. `ground_truth/` and `manifest.json` are never ingested.

**Lineage columns** on validated and cleaned rows: `_batch_id` (first 16 hex characters of the source file's SHA-256), `_source_file` (file name only), `_row_number` (0-based position in the source file), `_ingested_at` (UTC).

**Quarantine columns:** `_table`, `_batch_id`, `_source_file`, `_row_number`, `_stage` (VALIDATION or DEDUP), `_reason`, `_row_json` (the original row), `_quarantined_at`.
Reasons: `CAST_FAILED:<col>`, `NULL_REQUIRED:<col>`, `EMPTY_REQUIRED:<col>`, `CONFLICTING_DUPLICATE_IN_BATCH`, `CONFLICTING_DUPLICATE_OF_EXISTING`.

**Whole-batch rejection** (nothing reaches validated/cleaned; the raw copy is kept): `UNREADABLE_FILE:<error>`, `MISSING_COLUMNS:<cols>`, `UNEXPECTED_COLUMNS:<cols>`.

**Standardisation in the cleaned layer:** text is trimmed; identifiers and category-like columns are upper-cased; blank optional text becomes null. Original values remain in the raw layer.

**Source file naming:** `<table>.parquet`, or `<table>__<anything>.parquet` for additional batches (late data, replays).
