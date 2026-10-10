"""Feature definitions checked by hand on tiny frames with known answers."""

from datetime import date, datetime

import polars as pl
import pytest

from mmrisk.features import FEATURES, compute_features, feature_names
from mmrisk.features.audit import audit, univariate_auc
from mmrisk.features.compute import FeatureInputError

CUSTOMERS = pl.DataFrame(
    {
        "customer_id": ["C1", "C2"],
        "segment": ["SALARIED", "STUDENT"],
        "home_county": ["Nairobi", "Nairobi"],
        "registration_date": [date(2024, 1, 1), date(2024, 6, 1)],
    }
)


def txn(tid, cid, ts, ttype="AIRTIME", amount=100, cp_type=None, cp_id=None, device="D1",
        county="Nairobi", channel="USSD"):  # fmt: skip
    return {
        "transaction_id": tid, "customer_id": cid, "timestamp": ts, "transaction_type": ttype,
        "amount_kes": amount, "counterparty_type": cp_type, "counterparty_id": cp_id,
        "device_id": device, "location_county": county, "channel": channel,
    }  # fmt: skip


def frame(rows):
    return pl.DataFrame(
        rows,
        schema={
            "transaction_id": pl.Utf8, "customer_id": pl.Utf8, "timestamp": pl.Datetime("us"),
            "transaction_type": pl.Utf8, "amount_kes": pl.Int64, "counterparty_type": pl.Utf8,
            "counterparty_id": pl.Utf8, "device_id": pl.Utf8, "location_county": pl.Utf8,
            "channel": pl.Utf8,
        },
    )  # fmt: skip


def at(h, m=0, s=0, day=10):
    return datetime(2025, 3, day, h, m, s)


def feats(rows):
    out = compute_features(frame(rows), CUSTOMERS)
    return {r["transaction_id"]: r for r in out.to_dicts()}


SCENARIO = [
    txn("T1", "C1", at(10, 0), "SEND_MONEY", 1000, "CUSTOMER", "C2", "D1"),
    txn("T2", "C1", at(10, 1), "AIRTIME", 100, None, None, "D1"),
    txn("T3", "C1", at(10, 30), "SEND_MONEY", 5000, "CUSTOMER", "C2", "D2", "Mombasa"),
    txn("T4", "C2", at(10, 45), "AGENT_WITHDRAWAL", 2000, "AGENT", "A1", "D9"),
]


def test_first_transaction_has_no_history():
    f = feats(SCENARIO)["T1"]
    assert f["n_prior_txns"] == 0
    assert f["secs_since_prev_txn"] is None
    assert f["txn_count_1h"] == 0 and f["amount_sum_1h"] == 0.0
    assert f["avg_amount_30d"] is None and f["amount_to_avg_30d"] is None
    assert f["is_new_device"] is False  # nothing to be new relative to
    assert f["is_new_recipient"] is False
    assert f["secs_since_last_inbound"] is None


def test_velocity_and_baseline_use_only_earlier_rows():
    f = feats(SCENARIO)
    assert f["T2"]["txn_count_10m"] == 1 and f["T2"]["secs_since_prev_txn"] == 60.0
    t3 = f["T3"]
    assert t3["n_prior_txns"] == 2
    assert t3["txn_count_10m"] == 0  # the earlier two are 29 and 30 minutes old
    assert t3["txn_count_1h"] == 2 and t3["amount_sum_1h"] == 1100.0
    assert t3["txn_count_30d"] == 2 and t3["avg_amount_30d"] == 550.0
    assert t3["amount_to_avg_30d"] is None  # fewer than 3 earlier transactions


def test_novelty_flags():
    t3 = feats(SCENARIO)["T3"]
    assert t3["is_new_device"] is True  # D2 first seen, customer has history
    assert t3["is_new_recipient"] is False  # C2 was paid in T1
    assert t3["county_changed"] is True and t3["location_jump_1h"] is True
    assert t3["is_away_from_home"] is True
    assert feats(SCENARIO)["T2"]["is_new_device"] is False


def test_inbound_comes_from_other_customers_earlier_transfers():
    t4 = feats(SCENARIO)["T4"]
    assert t4["secs_since_last_inbound"] == 900.0  # the latest transfer, T3 at 10:30
    assert t4["cashout_after_inbound"] is True
    assert t4["n_prior_txns"] == 0  # own history is empty even though money came in


def test_cashout_window_is_two_hours():
    rows = [
        txn("T1", "C1", at(10), "SEND_MONEY", 1000, "CUSTOMER", "C2"),
        txn("T2", "C2", at(12, 0, 1), "AGENT_WITHDRAWAL", 500, "AGENT", "A1"),
        txn("T3", "C2", at(12, 0, 0), "AGENT_WITHDRAWAL", 500, "AGENT", "A1"),
    ]
    f = feats(rows)
    assert f["T3"]["cashout_after_inbound"] is True  # exactly 7,200 s
    assert f["T2"]["cashout_after_inbound"] is False  # 7,201 s


def test_same_timestamp_is_not_inbound_and_not_in_windows_but_counts_as_prior():
    rows = [
        txn("T1", "C1", at(10), "SEND_MONEY", 1000, "CUSTOMER", "C2"),
        txn("T2", "C2", at(10), "AGENT_WITHDRAWAL", 500, "AGENT", "A1"),
        txn("T3", "C1", at(11), "AIRTIME", 10),
        txn("T4", "C1", at(11), "AIRTIME", 10),
    ]
    f = feats(rows)
    assert f["T2"]["secs_since_last_inbound"] is None  # same instant is not "earlier"
    assert f["T4"]["txn_count_10m"] == 0  # same-timestamp rows stay out of time windows
    assert f["T4"]["n_prior_txns"] == 2  # ...but T3 precedes T4 in order, so it is history
    assert f["T3"]["n_prior_txns"] == 1


def test_relative_amount_needs_three_earlier_transactions_within_30_days():
    rows = [txn(f"T{i}", "C1", at(9, i, day=d), amount=100) for i, d in enumerate([1, 2, 3], 1)]
    rows.append(txn("T4", "C1", at(9, 0, day=20), amount=1000))
    # 21 days after T4 but more than 30 days after T1 to T3, so only T4 is in the window
    rows.append(txn("T5", "C1", datetime(2025, 4, 10, 9), amount=100))
    f = feats(rows)
    assert f["T4"]["amount_to_avg_30d"] == pytest.approx(10.0)
    assert f["T5"]["txn_count_30d"] == 1
    assert f["T5"]["amount_to_avg_30d"] is None


def test_window_boundary_is_inclusive_of_lower_edge():
    rows = [txn("T1", "C1", at(10)), txn("T2", "C1", at(10, 10)), txn("T3", "C1", at(10, 10, 1))]
    f = feats(rows)
    assert f["T2"]["txn_count_10m"] == 1  # exactly 10 minutes old still counts
    assert f["T3"]["txn_count_10m"] == 1  # T1 is 10:01 old (out), T2 is 1 s old (in)


def test_output_is_independent_of_input_order():
    shuffled = [SCENARIO[i] for i in (3, 0, 2, 1)]
    a = compute_features(frame(SCENARIO), CUSTOMERS)
    b = compute_features(frame(shuffled), CUSTOMERS)
    assert a.equals(b)


def test_columns_and_dtypes_match_the_registry():
    out = compute_features(frame(SCENARIO), CUSTOMERS)
    assert out.columns[:3] == ["transaction_id", "customer_id", "timestamp"]
    assert out.columns[3:] == feature_names()
    expected = {"float": pl.Float64, "int": pl.Int64, "bool": pl.Boolean, "category": pl.Utf8}
    for f in FEATURES:
        assert out.schema[f.name] == expected[f.dtype], f.name


def test_registry_is_consistent():
    names = feature_names()
    assert len(names) == len(set(names))
    assert all(f.description and f.history in {"none", "customer", "network"} for f in FEATURES)


def test_bad_input_is_rejected():
    with pytest.raises(FeatureInputError, match="missing columns"):
        compute_features(frame(SCENARIO).drop("channel"), CUSTOMERS)
    with pytest.raises(FeatureInputError, match="unique"):
        compute_features(frame([SCENARIO[0], SCENARIO[0]]), CUSTOMERS)
    with pytest.raises(FeatureInputError, match="customers"):
        compute_features(frame(SCENARIO), CUSTOMERS.drop("segment"))


def test_unknown_customer_gets_null_customer_attributes_not_a_crash():
    rows = [txn("T1", "C9", at(10))]
    out = compute_features(frame(rows), CUSTOMERS)
    assert out["segment"][0] is None


def test_univariate_auc_known_values():
    y = pl.Series([0, 0, 1, 1])
    assert univariate_auc(pl.Series([1, 2, 3, 4]), y) == 1.0
    assert univariate_auc(pl.Series([4, 3, 2, 1]), y) == 0.0
    assert univariate_auc(pl.Series([5, 5, 5, 5]), y) == 0.5
    assert univariate_auc(pl.Series([1, 2, 3, 4]), pl.Series([0, 0, 0, 0])) is None
    assert univariate_auc(pl.Series([1, None, 3, 4]), pl.Series([0, 0, 1, 1])) == 1.0


def test_audit_flags_a_planted_leak():
    n = 400
    label = [i % 2 for i in range(n)]
    table = compute_features(
        frame(
            [
                txn(f"T{i:04d}", "C1", datetime(2025, 3, 1 + i // 100, i % 24, i % 60))
                for i in range(n)
            ]
        ),
        CUSTOMERS,
    ).with_columns(label_is_fraud=pl.Series(label))
    table = table.with_columns(amount_kes=pl.Series(label) * 1000)  # a feature equal to the label
    flagged = audit(table).filter(pl.col("suspicious"))["feature"].to_list()
    assert "amount_kes" in flagged
