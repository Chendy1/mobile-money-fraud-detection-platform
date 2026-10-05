"""Dataset-level tests on generated synthetic data (schema, integrity, realism, leakage)."""

from datetime import date

import polars as pl
import pytest

from mmrisk.generation import GeneratorConfig, generate
from mmrisk.generation.config import DISPUTE_WINDOW_DAYS, MAX_TXN_KES
from mmrisk.generation.reference import PATTERNS, TXN_TYPES

CFG = GeneratorConfig(
    num_customers=1_500,
    num_transactions=60_000,
    fraud_rate=0.01,
    seed=7,
    start_date=date(2025, 1, 1),
    end_date=date(2025, 3, 31),
)


@pytest.fixture(scope="module")
def frames():
    return generate(CFG, write=False)


@pytest.fixture(scope="module")
def joined(frames):
    return (
        frames["transactions"]
        .join(frames["ground_truth/transaction_truth"], on="transaction_id")
        .join(frames["labels"], on="transaction_id")
    )


def test_exact_row_count_and_fraud_rate(frames):
    assert frames["transactions"].height == CFG.num_transactions
    truth = frames["ground_truth/transaction_truth"]
    assert truth["true_is_fraud"].sum() == CFG.num_fraud == 600


def test_transaction_ids_unique_and_time_ordered(frames):
    t = frames["transactions"]
    assert t["transaction_id"].n_unique() == t.height
    assert t["timestamp"].is_sorted()


def test_schema(frames):
    schema = frames["transactions"].schema
    assert schema["amount_kes"] == pl.Int64
    assert schema["timestamp"] == pl.Datetime("us")
    assert set(frames["transactions"]["transaction_type"].unique()) == set(TXN_TYPES)
    assert frames["labels"].schema["label_is_fraud"] == pl.Int8


def test_no_nulls_except_airtime_counterparty(frames):
    t = frames["transactions"]
    required = [c for c in t.columns if c not in ("counterparty_id", "counterparty_type")]
    assert all(t[c].null_count() == 0 for c in required)
    nulls = t.filter(pl.col("counterparty_id").is_null())
    assert set(nulls["transaction_type"].unique()) == {"AIRTIME"}


def test_timestamps_and_amounts_in_valid_range(frames):
    t = frames["transactions"]
    assert t["timestamp"].min().date() == date(2025, 1, 1)
    assert t["timestamp"].max().date() <= date(2025, 3, 31)
    assert t["amount_kes"].min() >= 1
    assert t["amount_kes"].max() <= MAX_TXN_KES


def test_referential_integrity(frames):
    t = frames["transactions"]
    customers = set(frames["customers"]["customer_id"])
    devices = set(frames["devices"]["device_id"])
    merchants = set(frames["merchants"]["merchant_id"])
    agents = set(frames["agents"]["agent_id"])
    counties = set(frames["counties"]["county"])
    assert set(t["customer_id"]) <= customers
    assert set(t["device_id"]) <= devices
    assert set(t["location_county"]) <= counties
    by_type = {"CUSTOMER": customers, "MERCHANT": merchants, "AGENT": agents}
    for kind, valid in by_type.items():
        ids = t.filter(pl.col("counterparty_type") == kind)["counterparty_id"]
        assert set(ids) <= valid, kind
    assert not (t["customer_id"] == t["counterparty_id"]).any()


def test_observable_tables_contain_no_fraud_information(frames):
    forbidden = ("fraud", "mule", "pattern", "episode", "vulnerab", "label")
    for name in (
        "transactions",
        "customers",
        "merchants",
        "agents",
        "devices",
        "counties",
    ):
        for col in frames[name].columns:
            assert not any(word in col.lower() for word in forbidden), f"{name}.{col}"


def test_all_fraud_patterns_present(joined):
    found = set(joined.filter(pl.col("true_is_fraud"))["fraud_pattern"].unique())
    assert found == set(PATTERNS)
    assert joined.filter(~pl.col("true_is_fraud"))["fraud_pattern"].null_count() > 0


def test_labels_are_noisy_and_delayed(joined):
    missed = joined.filter(pl.col("true_is_fraud") & (pl.col("label_is_fraud") == 0)).height
    wrong = joined.filter(~pl.col("true_is_fraud") & (pl.col("label_is_fraud") == 1)).height
    assert missed > 0, "some true fraud must be unreported"
    assert wrong > 0, "some legitimate transactions must be mislabelled"
    delay = joined.select(
        (pl.col("label_available_at") - pl.col("timestamp")).dt.total_seconds().alias("s"),
        "label_is_fraud",
    )
    assert delay["s"].min() > 0
    legit_delay = delay.filter(pl.col("label_is_fraud") == 0)["s"].min()
    assert legit_delay >= DISPUTE_WINDOW_DAYS * 86400
    assert delay.filter(pl.col("label_is_fraud") == 1)["s"].median() > 86400


def _with_signals(df: pl.DataFrame) -> pl.DataFrame:
    d = df.sort(["customer_id", "timestamp"]).with_columns(
        hour=pl.col("timestamp").dt.hour(),
        prior=pl.int_range(pl.len()).over("customer_id") > 0,
        first_dev=pl.int_range(pl.len()).over(["customer_id", "device_id"]) == 0,
        first_cp=pl.int_range(pl.len()).over(["customer_id", "counterparty_id"]) == 0,
        gap=pl.col("timestamp").diff().over("customer_id").dt.total_seconds(),
        prev_county=pl.col("location_county").shift(1).over("customer_id"),
        cust_median=pl.col("amount_kes").median().over("customer_id"),
    )
    return d.with_columns(
        night=pl.col("hour") < 5,
        new_device=pl.col("first_dev") & pl.col("prior"),
        new_recipient=(pl.col("transaction_type") == "SEND_MONEY")
        & pl.col("first_cp")
        & pl.col("prior"),
        high_amount=pl.col("amount_kes") > 5 * pl.col("cust_median"),
        fast_repeat=pl.col("gap") < 120,
        location_jump=(pl.col("location_county") != pl.col("prev_county")) & (pl.col("gap") < 3600),
    )


SIGNALS = [
    "night",
    "new_device",
    "new_recipient",
    "high_amount",
    "fast_repeat",
    "location_jump",
]


@pytest.mark.parametrize("signal", SIGNALS)
def test_signals_overlap_between_fraud_and_legit(joined, signal):
    """No single signal may perfectly identify (or perfectly miss) fraud."""
    d = _with_signals(joined)
    fraud, legit = d.filter(pl.col("true_is_fraud")), d.filter(~pl.col("true_is_fraud"))
    fraud_share, legit_share = fraud[signal].mean(), legit[signal].mean()
    assert 0.02 < fraud_share < 0.98, f"{signal}: fraud share {fraud_share:.3f}"
    assert legit_share > 0.0, f"{signal}: never occurs in legitimate data"
    assert fraud_share > legit_share, f"{signal}: should be more common in fraud"


def test_no_single_signal_is_a_perfect_rule(joined):
    d = _with_signals(joined)
    for signal in SIGNALS:
        flagged = d.filter(pl.col(signal))
        precision = flagged["true_is_fraud"].mean()
        recall = flagged["true_is_fraud"].sum() / d["true_is_fraud"].sum()
        assert not (precision > 0.9 and recall > 0.9), signal


def test_device_ids_do_not_leak_allocation_order(joined):
    """Device numbers must not encode customer identity or whether a device was used by fraud."""
    d = joined.with_columns(
        dev=pl.col("device_id").str.slice(1).cast(pl.Int64),
        cust=pl.col("customer_id").str.slice(1).cast(pl.Int64),
    )
    assert (d["dev"] == d["cust"]).mean() < 0.02
    fraud, legit = (
        d.filter(pl.col("true_is_fraud"))["dev"],
        d.filter(~pl.col("true_is_fraud"))["dev"],
    )
    spread = legit.max() - legit.min()
    assert abs(fraud.median() - legit.median()) < 0.15 * spread


def test_mule_accounts_are_hidden_but_receive_fraud(frames):
    latent = frames["ground_truth/customer_latent"]
    truth_t = frames["transactions"].join(
        frames["ground_truth/transaction_truth"], on="transaction_id"
    )
    mules = set(latent.filter(pl.col("is_mule"))["customer_id"])
    assert 3 <= len(mules) <= 0.05 * CFG.num_customers
    fraud_in = truth_t.filter(
        pl.col("true_is_fraud") & (pl.col("transaction_type") == "SEND_MONEY")
    )
    share_to_mules = fraud_in["counterparty_id"].is_in(list(mules)).mean()
    assert share_to_mules > 0.5


def test_reproducible_for_same_seed_and_different_for_other_seed():
    small = dict(
        num_customers=300,
        num_transactions=8_000,
        fraud_rate=0.01,
        end_date=date(2025, 2, 28),
    )
    a = generate(GeneratorConfig(seed=5, **small), write=False)
    b = generate(GeneratorConfig(seed=5, **small), write=False)
    c = generate(GeneratorConfig(seed=6, **small), write=False)
    assert all(a[k].equals(b[k]) for k in a)
    assert not a["transactions"].equals(c["transactions"])


def test_zero_fraud_rate_produces_no_fraud():
    cfg = GeneratorConfig(
        num_customers=100,
        num_transactions=2_000,
        fraud_rate=0.0,
        end_date=date(2025, 2, 15),
    )
    truth = generate(cfg, write=False)["ground_truth/transaction_truth"]
    assert truth["true_is_fraud"].sum() == 0
    assert truth.height == 2_000
