# Prediction store
`data/predictions/` (git-ignored) holds the output of `python -m mmrisk.streaming run`: one row per transaction scored for the first time. Later phases read it for monitoring, drift checks and the dashboard.

| Column | Type | Meaning |
|---|---|---|
| `seq` | Int64 | Position of the event in arrival order for that run |
| `transaction_id`, `customer_id` | Utf8 | As in the transactions table |
| `event_time` | Datetime (us) | Transaction time, naive local East Africa Time |
| `transaction_type`, `channel`, `location_county` | Utf8 | Copied from the event |
| `amount_kes` | Int64 | Copied from the event |
| `risk_probability` | Float64 | Calibrated fraud probability |
| `action`, `risk_level` | Utf8 | ALLOW, REVIEW or BLOCK, and LOW, MEDIUM or HIGH |
| `reasons` | Utf8 | JSON list of `{rank, feature, effect, text}`; empty for ALLOW; describes the model, not a cause |
| `feature_version` | Utf8 | Feature set version the model was built for |
| `processed_at` | Datetime (us) | Wall-clock time of scoring, naive UTC |

Guarantees: files are named `part-000001.parquet`, `part-000002.parquet` and so on; each is written under a temporary name and renamed, so a reader never sees a partial file (temporary files start with a dot and are ignored); a part is never rewritten; a new run continues the numbering. Not stored: the 28 feature values (recompute them from the lake by `transaction_id` for replayed events), labels (they arrive up to 45 days later), and events that were rejected or dropped (the run report, `stream_run.json`, counts them). The synthetic data is not real; nothing here is about real people.

Read it with `python -m mmrisk.streaming summary --store data/predictions` or in Python with `mmrisk.streaming.store.read(path)`.
