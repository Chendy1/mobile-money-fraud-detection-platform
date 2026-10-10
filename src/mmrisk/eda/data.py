"""Analysis workspace: DuckDB over the lake's Parquet files, with prior-only behavioural signals.

Everything a signal needs about a customer's *past* (previous transaction gap, running
average amount, counts in the last 10 minutes, first-time device or recipient) is computed
with window frames that exclude the current row and anything later, so nothing leaks.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

import duckdb
import polars as pl

from mmrisk.generation.config import DISPUTE_WINDOW_DAYS
from mmrisk.ingestion.reader import layer_files

SIGNALS = {
    "f_night": "Night-time (00:00 to 04:59)",
    "f_new_device": "First time this device is used by the customer",
    "f_new_recipient": "First transfer to this recipient",
    "f_high_rel_amount": "Amount above 5x the customer's running average",
    "f_rapid_repeat": "Within 2 minutes of the customer's previous transaction",
    "f_burst": "3 or more transactions in the previous 10 minutes",
    "f_location_jump": "County changed within 1 hour of the previous transaction",
    "f_top1pct_amount": "Amount in the top 1% of all transactions",
    "f_cashout_after_inbound": "Agent withdrawal within 2 hours of receiving a transfer",
}


class EDAError(RuntimeError):
    """Raised when the lake does not hold what the analysis needs."""


def _parquet_list(files: list[Path]) -> str:
    quoted = ",".join("'" + f.as_posix().replace("'", "''") + "'" for f in files)
    return f"read_parquet([{quoted}], hive_partitioning=false)"


def _lit(ts: datetime) -> str:
    return f"TIMESTAMP '{ts.isoformat(sep=' ')}'"


@dataclass
class Analysis:
    con: duckdb.DuckDBPyConnection
    cutoff: datetime  # analysis time: the end of the data
    matured_cutoff: datetime  # transactions up to here have final labels
    truth_available: bool
    has_merchants: bool
    lineage: dict

    def query(self, sql: str) -> pl.DataFrame:
        cur = self.con.execute(sql)
        cols = [d[0] for d in cur.description]
        return pl.DataFrame(cur.fetchall(), schema=cols, orient="row", infer_schema_length=None)

    def scalar(self, sql: str):
        return self.con.execute(sql).fetchone()[0]

    @classmethod
    def from_lake(cls, lake_root: Path | str, source_dir: Path | str | None = None) -> Analysis:
        root = Path(lake_root)
        tables = ("transactions", "labels", "customers", "merchants")
        files = {t: layer_files(root, "cleaned", t) for t in tables}
        missing = [t for t in ("transactions", "labels", "customers") if not files[t]]
        if missing:
            raise EDAError(
                f"lake has no completed data for: {', '.join(missing)}; run the ingestion first"
            )
        truth = None
        if source_dir is not None:
            candidate = Path(source_dir) / "ground_truth" / "transaction_truth.parquet"
            truth = candidate if candidate.exists() else None
        lineage = {t: sorted({f.stem for f in fs}) for t, fs in files.items() if fs}
        return cls.from_files(
            files["transactions"],
            files["labels"],
            files["customers"],
            files["merchants"],
            truth,
            lineage,
        )

    @classmethod
    def from_files(
        cls,
        txn_files: list[Path],
        label_files: list[Path],
        customer_files: list[Path],
        merchant_files: list[Path] | None = None,
        truth_file: Path | None = None,
        lineage: dict | None = None,
    ) -> Analysis:
        con = duckdb.connect()
        con.execute(f"CREATE VIEW txn AS SELECT * FROM {_parquet_list(txn_files)}")
        con.execute(f"CREATE VIEW lab AS SELECT * FROM {_parquet_list(label_files)}")
        con.execute(f"CREATE VIEW cust AS SELECT * FROM {_parquet_list(customer_files)}")
        if merchant_files:
            con.execute(f"CREATE VIEW merch AS SELECT * FROM {_parquet_list(merchant_files)}")
        if truth_file is not None:
            truth_path = truth_file.as_posix()
            con.execute(f"CREATE VIEW truth AS SELECT * FROM read_parquet('{truth_path}')")
        cutoff = con.execute("SELECT max(timestamp) FROM txn").fetchone()[0]
        if cutoff is None:
            raise EDAError("there are no transactions to analyse")
        matured = cutoff - timedelta(days=DISPUTE_WINDOW_DAYS)
        top1 = con.execute("SELECT quantile_cont(amount_kes, 0.99) FROM txn").fetchone()[0]
        con.execute(
            _ANALYSIS_TABLE.format(cutoff=_lit(cutoff), matured=_lit(matured), top1=float(top1))
        )
        return cls(
            con, cutoff, matured, truth_file is not None, bool(merchant_files), lineage or {}
        )


# Window frames never include the current row or later rows ("... AND 1 PRECEDING").
_ANALYSIS_TABLE = """
CREATE TABLE a AS
WITH base AS (
  SELECT t.transaction_id, t.customer_id, t.timestamp AS ts, t.transaction_type AS ttype,
         t.amount_kes::DOUBLE AS amount, t.counterparty_type AS cp_type,
         t.counterparty_id AS cp_id, t.device_id, t.location_county AS county, t.channel,
         c.segment, l.label_is_fraud AS final_label, l.label_available_at AS label_at
  FROM txn t
  LEFT JOIN cust c ON c.customer_id = t.customer_id
  LEFT JOIN lab l ON l.transaction_id = t.transaction_id
), inb AS (
  SELECT cp_id AS recipient, ts AS in_ts
  FROM base WHERE ttype = 'SEND_MONEY' AND cp_type = 'CUSTOMER'
), w0 AS (
  SELECT b.*, i.in_ts AS last_in_ts
  FROM base b ASOF LEFT JOIN inb i ON b.customer_id = i.recipient AND b.ts > i.in_ts
), w AS (
  SELECT *,
    row_number() OVER (PARTITION BY customer_id ORDER BY ts, transaction_id) AS cust_seq,
    row_number() OVER (PARTITION BY customer_id, device_id
                       ORDER BY ts, transaction_id) AS dev_seq,
    row_number() OVER (PARTITION BY customer_id, cp_id ORDER BY ts, transaction_id) AS cp_seq,
    date_diff('second', lag(ts) OVER cw, ts) AS secs_since_prev,
    lag(county) OVER cw AS prev_county,
    avg(amount) OVER (PARTITION BY customer_id ORDER BY ts, transaction_id
                      ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING) AS prior_avg,
    count(*) OVER (PARTITION BY customer_id ORDER BY ts, transaction_id
                   ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING) AS prior_n,
    count(*) OVER (PARTITION BY customer_id ORDER BY ts
                   RANGE BETWEEN INTERVAL 10 MINUTE PRECEDING
                   AND INTERVAL 1 SECOND PRECEDING) AS n_10min,
    count(*) OVER (PARTITION BY customer_id ORDER BY ts
                   RANGE BETWEEN INTERVAL 1 HOUR PRECEDING
                   AND INTERVAL 1 SECOND PRECEDING) AS n_1h,
    count(*) OVER (PARTITION BY customer_id ORDER BY ts
                   RANGE BETWEEN INTERVAL 24 HOUR PRECEDING
                   AND INTERVAL 1 SECOND PRECEDING) AS n_24h
  FROM w0
  WINDOW cw AS (PARTITION BY customer_id ORDER BY ts, transaction_id)
)
SELECT *,
  hour(ts) AS hr, dayofweek(ts) AS dow, day(ts) AS dom, CAST(ts AS DATE) AS day,
  (ts <= {matured}) AS matured,
  (label_at IS NOT NULL AND label_at <= {cutoff}) AS label_known,
  (hour(ts) < 5) AS f_night,
  (dev_seq = 1 AND cust_seq > 1) AS f_new_device,
  (ttype = 'SEND_MONEY' AND cp_seq = 1 AND cust_seq > 1) AS f_new_recipient,
  (prior_n >= 3 AND amount > 5 * prior_avg) AS f_high_rel_amount,
  (secs_since_prev IS NOT NULL AND secs_since_prev <= 120) AS f_rapid_repeat,
  (n_10min >= 3) AS f_burst,
  (prev_county IS NOT NULL AND county <> prev_county
   AND secs_since_prev < 3600) AS f_location_jump,
  (amount >= {top1}) AS f_top1pct_amount,
  (ttype = 'AGENT_WITHDRAWAL' AND last_in_ts IS NOT NULL
   AND date_diff('second', last_in_ts, ts) <= 7200) AS f_cashout_after_inbound,
  CASE WHEN prior_n >= 3 THEN amount / prior_avg END AS rel_amount
FROM w
"""
