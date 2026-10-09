"""Data-quality tests: corrupt the data in a specific way, then check the right thing happens.

Each scenario builds a fresh lake from deliberately damaged source frames, so these tests
fail if a rule or check stops catching its defect (or starts flagging clean data).
"""

import json
from datetime import date, datetime, timedelta

import polars as pl
import pytest

from mmrisk.generation import GeneratorConfig, generate
from mmrisk.ingestion import ingest_source_dir, read_layer
from mmrisk.ingestion.lake import LakePaths
from mmrisk.validation.checks import FAIL, PASS, SKIPPED, WARN, run_checks
from mmrisk.validation.cli import main as dq_main

CLOCK = lambda: datetime(2025, 7, 1, 12, 0, 0)  # noqa: E731
TABLES = ["counties", "customers", "merchants", "agents", "devices", "transactions", "labels"]
N = 6_000


@pytest.fixture(scope="module")
def base():
    cfg = GeneratorConfig(
        num_customers=200,
        num_transactions=N,
        fraud_rate=0.02,
        seed=11,
        end_date=date(2025, 3, 1),
    )
    frames = generate(cfg, write=False)
    return {t: frames[t] for t in TABLES}


def build_lake(tmp_path, frames, extra=None):
    src = tmp_path / "src"
    src.mkdir(parents=True, exist_ok=True)
    for name, df in frames.items():
        df.write_parquet(src / f"{name}.parquet")
    for filename, df in (extra or {}).items():
        df.write_parquet(src / filename)
    lake = tmp_path / "lake"
    ingest_source_dir(src, lake, CLOCK)
    return lake


def check(report, name, table=None):
    hits = [
        c for c in report.checks if c.name.startswith(name) and (table is None or c.table == table)
    ]
    assert hits, f"no check named {name!r}"
    return (
        hits[0]
        if len(hits) == 1
        else max(hits, key=lambda c: [PASS, SKIPPED, WARN, FAIL].index(c.status))
    )


def set_rows(df, idx, **cols):
    out = df.with_row_index("_i")
    for col, val in cols.items():
        value = val if isinstance(val, pl.Expr) else pl.lit(val)
        out = out.with_columns(
            pl.when(pl.col("_i").is_in(idx)).then(value).otherwise(pl.col(col)).alias(col)
        )
    return out.drop("_i")


def pick(df, cond, n):
    return df.with_row_index("_i").filter(cond)["_i"][:n].to_list()


def with_transactions(base, txn):
    return {**base, "transactions": txn}


# ---- clean data must pass -----------------------------------------------------------------


def test_clean_generated_data_passes_every_check(base, tmp_path):
    report = run_checks(build_lake(tmp_path, base), write=False)
    bad = [
        (c.name, c.table, c.status, c.observed) for c in report.checks if c.status in (WARN, FAIL)
    ]
    assert bad == []
    assert report.status == PASS
    assert [c.name for c in report.checks if c.status == SKIPPED] == ["freshness"]


# ---- row rules: each defect is quarantined with the right reason ------------------------------

TXN = pl.col


def _rows(df, cond):
    return pick(df, cond, 5)


ROW_DEFECTS = {
    "zero_amount": (
        lambda d: set_rows(d, _rows(d, TXN("amount_kes") > 0), amount_kes=0),
        "amount_positive",
    ),
    "negative_amount": (
        lambda d: set_rows(d, _rows(d, TXN("amount_kes") > 0), amount_kes=-100),
        "amount_positive",
    ),
    "huge_amount": (
        lambda d: set_rows(d, _rows(d, TXN("amount_kes") > 0), amount_kes=10_000_000),
        "amount_within_cap",
    ),
    "future_timestamp": (
        lambda d: set_rows(
            d, _rows(d, TXN("amount_kes") > 0), timestamp=TXN("timestamp") + pl.duration(days=400)
        ),
        "timestamp_not_future",
    ),
    "ancient_timestamp": (
        lambda d: set_rows(d, _rows(d, TXN("amount_kes") > 0), timestamp=datetime(1970, 1, 1)),
        "timestamp_plausible",
    ),
    "unknown_type": (
        lambda d: set_rows(d, _rows(d, TXN("amount_kes") > 0), transaction_type="WIRE_TRANSFER"),
        "allowed_transaction_type",
    ),
    "unknown_channel": (
        lambda d: set_rows(d, _rows(d, TXN("amount_kes") > 0), channel="SMS"),
        "allowed_channel",
    ),
    "airtime_with_counterparty": (
        lambda d: set_rows(
            d,
            _rows(d, TXN("transaction_type") == "AIRTIME"),
            counterparty_type="CUSTOMER",
            counterparty_id="C000001",
        ),
        "counterparty_consistent",
    ),
    "send_money_without_counterparty": (
        lambda d: set_rows(
            d,
            _rows(d, TXN("transaction_type") == "SEND_MONEY"),
            counterparty_type=pl.lit(None, dtype=pl.Utf8),
            counterparty_id=pl.lit(None, dtype=pl.Utf8),
        ),
        "counterparty_consistent",
    ),
    "self_transfer": (
        lambda d: set_rows(
            d, _rows(d, TXN("transaction_type") == "SEND_MONEY"), counterparty_id=TXN("customer_id")
        ),
        "no_self_transfer",
    ),
    "malformed_customer_id": (
        lambda d: set_rows(d, _rows(d, TXN("amount_kes") > 0), customer_id="cust-1"),
        "customer_id_format",
    ),
}


@pytest.mark.parametrize("defect", sorted(ROW_DEFECTS))
def test_row_defects_are_quarantined_with_the_right_reason(base, tmp_path, defect):
    mutate, rule = ROW_DEFECTS[defect]
    lake = build_lake(tmp_path, with_transactions(base, mutate(base["transactions"])))
    q = read_layer(lake, "quarantine", "transactions")
    assert q.height == 5
    assert set(q["_reason"]) == {f"RULE:{rule}"}
    assert set(q["_stage"]) == {"RULES"}
    assert read_layer(lake, "cleaned", "transactions").height == N - 5
    # Knock-on effect: the labels of the quarantined transactions are now orphans (a WARN),
    # but nothing else is flagged, and the quarantine rate stays below the 1% warning level.
    report = run_checks(lake, write=False)
    flagged = {c.name: c.status for c in report.checks if c.status in (WARN, FAIL)}
    assert flagged == {"referential_labels.transaction_id->transactions": WARN}


def test_lowercase_identifiers_are_standardised_not_rejected(base, tmp_path):
    t = base["transactions"]
    low = set_rows(
        t,
        pick(t, pl.col("amount_kes") > 0, 50),
        transaction_id=pl.col("transaction_id").str.to_lowercase(),
    )
    lake = build_lake(tmp_path, with_transactions(base, low))
    cleaned = read_layer(lake, "cleaned", "transactions")
    assert cleaned.height == N
    assert cleaned["transaction_id"].str.starts_with("TXN").all()
    assert read_layer(lake, "quarantine", "transactions").height == 0


@pytest.mark.parametrize(("share", "expected"), [(0.02, WARN), (0.08, FAIL)])
def test_quarantine_rate_thresholds(base, tmp_path, share, expected):
    t = base["transactions"]
    broken = set_rows(t, pick(t, pl.col("amount_kes") > 0, int(share * N)), amount_kes=0)
    report = run_checks(build_lake(tmp_path, with_transactions(base, broken)), write=False)
    assert check(report, "quarantine_rate", "transactions").status == expected


# ---- duplicates and conflicts --------------------------------------------------------------


def second_batch(base, dup=0, conflict=0):
    t = base["transactions"].sample(fraction=1.0, shuffle=True, seed=3)
    parts = [t[:dup], t[dup : dup + conflict].with_columns(pl.col("amount_kes") + 1)]
    return pl.concat([p for p in parts if p.height])


@pytest.mark.parametrize(("dups", "expected"), [(200, PASS), (400, WARN)])
def test_duplicate_rate_thresholds(base, tmp_path, dups, expected):
    lake = build_lake(
        tmp_path, base, {"transactions__replay.parquet": second_batch(base, dup=dups)}
    )
    report = run_checks(lake, write=False)
    assert check(report, "duplicate_rate", "transactions").status == expected
    assert check(report, "key_uniqueness", "transactions").status == PASS


@pytest.mark.parametrize(("conflicts", "expected"), [(30, WARN), (100, FAIL)])
def test_conflicting_duplicates_thresholds(base, tmp_path, conflicts, expected):
    lake = build_lake(
        tmp_path, base, {"transactions__fix.parquet": second_batch(base, conflict=conflicts)}
    )
    report = run_checks(lake, write=False)
    assert check(report, "conflicting_duplicates", "transactions").status == expected
    assert read_layer(lake, "quarantine", "transactions").height == conflicts


# ---- referential integrity -----------------------------------------------------------------

ORPHANS = {
    "customer": ("customer_id", "C999999", "referential_transactions.customer_id"),
    "device": ("device_id", "D999999", "referential_transactions.device_id"),
    "county": ("location_county", "Atlantis", "referential_transactions.location_county"),
}


@pytest.mark.parametrize("kind", sorted(ORPHANS))
@pytest.mark.parametrize(("count", "expected"), [(1, WARN), (50, FAIL)])
def test_orphan_references(base, tmp_path, kind, count, expected):
    col, value, name = ORPHANS[kind]
    t = base["transactions"]
    bad = set_rows(t, pick(t, pl.col("amount_kes") > 0, count), **{col: value})
    report = run_checks(build_lake(tmp_path, with_transactions(base, bad)), write=False)
    assert check(report, name).status == expected


def test_orphan_counterparty(base, tmp_path):
    t = base["transactions"]
    bad = set_rows(
        t, pick(t, pl.col("counterparty_type") == "CUSTOMER", 50), counterparty_id="C999999"
    )
    report = run_checks(build_lake(tmp_path, with_transactions(base, bad)), write=False)
    assert check(report, "referential_transactions.counterparty(CUSTOMER)").status == FAIL


def test_labels_for_unknown_transactions(base, tmp_path):
    ghost = (
        base["labels"]
        .head(50)
        .with_columns(
            pl.format("TXN8{}", pl.int_range(pl.len()).cast(pl.Utf8).str.zfill(8)).alias(
                "transaction_id"
            )
        )
    )
    lake = build_lake(tmp_path, {**base, "labels": pl.concat([base["labels"], ghost])})
    assert check(run_checks(lake, write=False), "referential_labels.transaction_id").status == FAIL


def test_missing_dimension_table_fails_and_skips_dependent_checks(base, tmp_path):
    lake = build_lake(tmp_path, {k: v for k, v in base.items() if k != "customers"})
    report = run_checks(lake, write=False)
    assert check(report, "table_loaded", "customers").status == FAIL
    assert check(report, "referential_transactions.customer_id").status == SKIPPED
    assert report.status == FAIL


# ---- schema changes --------------------------------------------------------------------------


def test_dtype_drift_is_warned_but_data_still_loads(base, tmp_path):
    drift = base["transactions"].with_columns(pl.col("amount_kes").cast(pl.Utf8))
    lake = build_lake(tmp_path, with_transactions(base, drift))
    report = run_checks(lake, write=False)
    drift_check = check(report, "schema_drift", "transactions")
    assert drift_check.status == WARN and "amount_kes" in drift_check.message
    assert read_layer(lake, "cleaned", "transactions").height == N


def test_missing_column_rejects_batch_and_a_later_fix_downgrades_to_warn(base, tmp_path):
    broken = with_transactions(base, base["transactions"].drop("channel"))
    lake = build_lake(tmp_path, broken)
    report = run_checks(lake, write=False)
    assert check(report, "batches_rejected", "transactions").status == FAIL
    assert check(report, "table_loaded", "transactions").status == FAIL

    later = datetime(2025, 7, 2, 12)
    base["transactions"].write_parquet(tmp_path / "src" / "transactions__fixed.parquet")
    ingest_source_dir(tmp_path / "src", lake, lambda: later)
    after = run_checks(lake, write=False)
    assert check(after, "batches_rejected", "transactions").status == WARN
    assert check(after, "table_loaded", "transactions").status == PASS


# ---- damage to the stored data (defence in depth) ----------------------------------------------


def first_cleaned_file(lake, table="transactions"):
    return next((lake / "cleaned" / table).rglob("*.parquet"))


def test_nulls_injected_into_stored_data_fail_null_rate(base, tmp_path):
    lake = build_lake(tmp_path, base)
    f = first_cleaned_file(lake)
    pl.read_parquet(f).with_columns(pl.lit(None, dtype=pl.Int64).alias("amount_kes")).write_parquet(
        f
    )
    report = run_checks(lake, write=False)
    nulls = check(report, "null_rates", "transactions")
    assert nulls.status == FAIL and "amount_kes" in nulls.observed


def test_duplicated_rows_in_stored_data_fail_key_and_accounting_checks(base, tmp_path):
    lake = build_lake(tmp_path, base)
    f = first_cleaned_file(lake)
    df = pl.read_parquet(f)
    pl.concat([df, df.head(3)]).write_parquet(f)
    report = run_checks(lake, write=False)
    assert check(report, "key_uniqueness", "transactions").status == FAIL
    assert check(report, "row_accounting").status == FAIL


def test_deleted_partition_fails_row_accounting(base, tmp_path):
    lake = build_lake(tmp_path, base)
    first_cleaned_file(lake).unlink()
    assert check(run_checks(lake, write=False), "row_accounting").status == FAIL


# ---- label timing, freshness, volume ---------------------------------------------------------


def test_label_known_before_transaction_fails(base, tmp_path):
    j = base["labels"].join(
        base["transactions"].select("transaction_id", "timestamp"), on="transaction_id"
    )
    early = j.with_row_index("_i").with_columns(
        pl.when(pl.col("_i") < 10)
        .then(pl.col("timestamp") - pl.duration(hours=1))
        .otherwise(pl.col("label_available_at"))
        .alias("label_available_at")
    )
    labels = early.select(base["labels"].columns)
    report = run_checks(build_lake(tmp_path, {**base, "labels": labels}), write=False)
    assert check(report, "label_timing").status == FAIL


def test_freshness(base, tmp_path):
    lake = build_lake(tmp_path, base)
    newest = read_layer(lake, "cleaned", "transactions")["timestamp"].max()
    assert (
        check(run_checks(lake, newest + timedelta(hours=2), write=False), "freshness").status
        == PASS
    )
    stale = run_checks(lake, newest + timedelta(days=3), write=False)
    assert check(stale, "freshness").status == FAIL
    lenient = run_checks(lake, newest + timedelta(days=3), max_lag_hours=100, write=False)
    assert check(lenient, "freshness").status == PASS
    assert check(run_checks(lake, write=False), "freshness").status == SKIPPED


def test_missing_days_fail_volume_check(base, tmp_path):
    days = [date(2025, 2, 10), date(2025, 2, 11), date(2025, 2, 12)]
    t = base["transactions"].filter(~pl.col("timestamp").dt.date().is_in(days))
    labels = base["labels"].join(t.select("transaction_id"), on="transaction_id")
    lake = build_lake(tmp_path, {**base, "transactions": t, "labels": labels})
    vol = check(run_checks(lake, write=False), "daily_volume")
    assert vol.status == FAIL and "2025-02-10" in vol.message


def test_a_volume_spike_warns(base, tmp_path):
    t = base["transactions"]
    day = t.filter(pl.col("timestamp").dt.date() == date(2025, 2, 14))
    spike = pl.concat([day] * 12).with_columns(
        pl.format("TXN7{}", pl.int_range(pl.len()).cast(pl.Utf8).str.zfill(8)).alias(
            "transaction_id"
        )
    )
    lake = build_lake(tmp_path, with_transactions(base, pl.concat([t, spike])))
    vol = check(run_checks(lake, write=False), "daily_volume")
    assert vol.status == WARN and "high" in vol.message


def test_tiny_datasets_skip_the_volume_check(tmp_path):
    cfg = GeneratorConfig(num_customers=50, num_transactions=500, end_date=date(2025, 12, 31))
    frames = generate(cfg, write=False)
    lake = build_lake(tmp_path, {t: frames[t] for t in TABLES})
    assert check(run_checks(lake, write=False), "daily_volume").status == SKIPPED


# ---- report and CLI -----------------------------------------------------------------------


def test_report_is_written_as_json(base, tmp_path):
    lake = build_lake(tmp_path, base)
    run_checks(lake, now=datetime(2025, 7, 1, 12))
    data = json.loads((LakePaths(lake).meta / "dq_report.json").read_text())
    assert data["status"] == "PASS" and data["generated_at"] == "2025-07-01T12:00:00"
    assert data["counts"]["FAIL"] == 0 and len(data["checks"]) > 40


def test_cli_exit_codes(base, tmp_path, capsys):
    clean = build_lake(tmp_path / "clean", base)
    assert dq_main(["check", "--lake", str(clean)]) == 0
    out = capsys.readouterr().out
    assert "OVERALL: PASS" in out and "SKIPPED" in out

    t = base["transactions"]
    warn_frames = with_transactions(
        base, set_rows(t, pick(t, pl.col("amount_kes") > 0, 1), customer_id="C999999")
    )
    warn = build_lake(tmp_path / "warn", warn_frames)
    assert dq_main(["check", "--lake", str(warn)]) == 0
    assert "OVERALL: WARN" in capsys.readouterr().out
    assert dq_main(["check", "--lake", str(warn), "--strict"]) == 1

    fail_frames = with_transactions(
        base, set_rows(t, pick(t, pl.col("amount_kes") > 0, 50), customer_id="C999999")
    )
    fail = build_lake(tmp_path / "fail", fail_frames)
    assert dq_main(["check", "--lake", str(fail)]) == 1
    assert "FAIL" in capsys.readouterr().out

    assert dq_main(["check", "--lake", str(tmp_path / "nothing")]) == 2
    assert "run the ingestion first" in capsys.readouterr().err
    assert dq_main(["check", "--lake", str(clean), "--verbose", "--no-write"]) == 0
    assert "PASS    table_loaded" in capsys.readouterr().out
