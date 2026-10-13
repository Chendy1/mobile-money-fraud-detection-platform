"""Metrics, preprocessing and rule baseline checked on tiny cases with known answers."""

import numpy as np
import polars as pl
import pytest

pytest.importorskip("sklearn")

from mmrisk.features.registry import FEATURES, feature_names  # noqa: E402
from mmrisk.models.baselines import RULES, PriorBaseline, RuleBaseline  # noqa: E402
from mmrisk.models.metrics import (  # noqa: E402
    BUDGETS_PER_10K,
    bootstrap,
    budget_point,
    customer_weights,
    interval,
    pr_auc,
    summarize,
)
from mmrisk.models.preprocess import CATEGORIES, column_names, to_matrix  # noqa: E402


def feature_frame(n=6, **overrides) -> pl.DataFrame:
    """A small valid feature table (all registered columns)."""
    base = {
        "amount_kes": [100, 5000, 250, 90000, 40, 700][:n],
        "amount_log": np.log1p([100, 5000, 250, 90000, 40, 700][:n]).tolist(),
        "transaction_type": [
            "SEND_MONEY",
            "TILL",
            "AIRTIME",
            "AGENT_WITHDRAWAL",
            "PAYBILL",
            "TILL",
        ][:n],
        "channel": ["USSD", "APP", "USSD", "APP", "USSD", "APP"][:n],
        "hour": [0, 6, 12, 18, 23, 3][:n],
        "day_of_week": [1, 2, 3, 4, 5, 6][:n],
        "is_night": [True, False, False, False, False, True][:n],
        "is_salary_window": [False] * n,
        "segment": [
            "SALARIED",
            "STUDENT",
            "SMALL_BUSINESS",
            "INFORMAL_WORKER",
            "SALARIED",
            "STUDENT",
        ][:n],
        "account_age_days": [100, 200, 300, 400, 500, 600][:n],
        "is_away_from_home": [False] * n,
        "n_prior_txns": [0, 1, 2, 3, 4, 5][:n],
        "secs_since_prev_txn": [None, 60.0, 600.0, 90.0, 10.0, 1000.0][:n],
        "txn_count_10m": [0, 1, 0, 3, 0, 0][:n],
        "txn_count_1h": [0, 1, 1, 3, 1, 0][:n],
        "txn_count_24h": [0, 1, 2, 3, 4, 2][:n],
        "txn_count_7d": [0, 1, 2, 3, 4, 5][:n],
        "amount_sum_1h": [0.0, 10.0, 20.0, 30.0, 40.0, 0.0][:n],
        "amount_sum_24h": [0.0, 10.0, 20.0, 30.0, 40.0, 50.0][:n],
        "txn_count_30d": [0, 1, 2, 3, 4, 5][:n],
        "avg_amount_30d": [None, 10.0, 20.0, 30.0, 40.0, 50.0][:n],
        "amount_to_avg_30d": [None, None, None, 9.0, 1.0, 2.0][:n],
        "is_new_device": [False, False, True, False, False, False][:n],
        "is_new_recipient": [False, True, False, False, False, False][:n],
        "county_changed": [False] * n,
        "location_jump_1h": [False, False, False, False, True, False][:n],
        "secs_since_last_inbound": [None, None, 100.0, None, None, 5000.0][:n],
        "cashout_after_inbound": [False, False, False, False, False, False][:n],
    }
    base.update(overrides)
    return pl.DataFrame(base)


# ---- metrics ----------------------------------------------------------------------------------


def test_budget_point_known_values():
    y = np.array([1, 0, 1, 0])
    s = np.array([0.9, 0.8, 0.7, 0.6])
    assert budget_point(y, s, 5000) == (0.5, 0.5)  # top half = {0.9, 0.8}: 1 fraud of 2
    assert budget_point(y, s, 2500) == (1.0, 0.5)  # top quarter = {0.9}
    assert budget_point(y, s, 10_000) == (0.5, 1.0)  # everything


def test_ties_are_taken_in_proportion_so_row_order_cannot_matter():
    y = np.array([1, 0, 0, 0])
    s = np.zeros(4)
    for perm in ([0, 1, 2, 3], [3, 2, 1, 0], [1, 3, 0, 2]):
        assert budget_point(y[perm], s[perm], 5000) == pytest.approx((0.25, 0.5))


def test_weights_equal_repeating_rows():
    rng = np.random.default_rng(0)
    y = (rng.random(60) < 0.2).astype(float)
    s = rng.random(60)
    w = rng.integers(0, 4, 60).astype(float)
    rep = np.repeat(np.arange(60), w.astype(int))
    for b in (500, 2500, 5000):
        assert budget_point(y, s, b, w) == pytest.approx(budget_point(y[rep], s[rep], b))
    assert pr_auc(y, s, w) == pytest.approx(pr_auc(y[rep], s[rep]))


def test_pr_auc_reference_values():
    y = np.array([0, 0, 1, 1] * 25)
    assert pr_auc(y, y.astype(float)) == pytest.approx(1.0)
    assert pr_auc(y, np.zeros(100)) == pytest.approx(y.mean())  # constant score = prevalence


def test_summarize_has_every_budget():
    y = np.array([0, 1] * 50)
    out = summarize(y, np.arange(100.0))
    for b in BUDGETS_PER_10K:
        assert f"precision@{b}" in out and f"recall@{b}" in out


def test_customer_bootstrap_resamples_whole_customers():
    cust = np.repeat(np.array(["A", "B", "C", "D"]), [5, 1, 1, 3])
    w = customer_weights(cust, np.random.default_rng(1))
    for c in "ABCD":
        assert len(set(w[cust == c])) == 1  # every row of a customer gets the same weight
    totals = [customer_weights(cust, np.random.default_rng(i)).sum() for i in range(5)]
    assert all(t > 0 for t in totals)


def test_bootstrap_is_reproducible_and_shared_across_models():
    rng = np.random.default_rng(3)
    y = (rng.random(300) < 0.1).astype(float)
    cust = rng.integers(0, 40, 300).astype(str)
    scores = {"a": rng.random(300), "b": y + rng.random(300)}
    stats = {"pr_auc": lambda y_, s_, w_: pr_auc(y_, s_, w_)}
    one = bootstrap(y, scores, cust, stats, n=20, seed=5)
    two = bootstrap(y, scores, cust, stats, n=20, seed=5)
    assert np.array_equal(one["b"]["pr_auc"], two["b"]["pr_auc"])
    assert one["b"]["pr_auc"].mean() > one["a"]["pr_auc"].mean()
    lo, hi = interval(one["a"]["pr_auc"])
    assert lo <= hi


# ---- preprocessing ----------------------------------------------------------------------------


@pytest.mark.parametrize("kind", ["linear", "tree"])
def test_matrix_shape_matches_column_names(kind):
    x = to_matrix(feature_frame(), kind)
    assert x.shape == (6, len(column_names(kind)))
    assert x.dtype == np.float64


def test_matrix_rows_do_not_depend_on_other_rows():
    """to_matrix is stateless: a row is transformed identically alone or among others."""
    df = feature_frame()
    full = to_matrix(df, "linear")
    for i in range(df.height):
        assert np.allclose(to_matrix(df[i], "linear"), full[i : i + 1], equal_nan=True)


def test_missing_stays_nan_and_unknown_category_is_all_zero():
    df = feature_frame(transaction_type=["NEW_TYPE"] + ["TILL"] * 5)
    x = to_matrix(df, "tree")
    names = column_names("tree")
    secs = names.index("secs_since_prev_txn")
    assert np.isnan(x[0, secs])
    cols = [i for i, n in enumerate(names) if n.startswith("transaction_type=")]
    assert x[0, cols].sum() == 0 and x[1, cols].sum() == 1


def test_linear_matrix_uses_logs_and_cyclic_hour_tree_keeps_raw():
    df = feature_frame()
    lin, tree = to_matrix(df, "linear"), to_matrix(df, "tree")
    ln, tn = column_names("linear"), column_names("tree")
    assert lin[1, ln.index("amount_kes")] == pytest.approx(np.log1p(5000))
    assert tree[1, tn.index("amount_kes")] == 5000
    hour_sin = lin[:, ln.index("hour_sin")]
    assert hour_sin[0] == pytest.approx(0.0) and hour_sin[4] == pytest.approx(
        np.sin(2 * np.pi * 23 / 24)
    )
    assert "hour" not in ln and "amount_log" not in ln and "amount_log" in tn


def test_booleans_are_zero_one():
    x = to_matrix(feature_frame(), "tree")
    assert set(np.unique(x[:, column_names("tree").index("is_night")])) == {0.0, 1.0}


def test_bad_input_is_rejected():
    with pytest.raises(ValueError, match="missing feature columns"):
        to_matrix(feature_frame().drop("channel"))
    with pytest.raises(ValueError, match="kind"):
        to_matrix(feature_frame(), "deep")


def test_no_label_columns_or_identifiers_reach_the_models():
    for kind in ("linear", "tree"):
        names = column_names(kind)
        assert not any("label" in n for n in names)
        assert not {"customer_id", "device_id", "counterparty_id", "transaction_id"} & set(names)
    assert set(CATEGORIES) == {f.name for f in FEATURES if f.dtype == "category"}
    # every registered feature is represented in the matrix
    joined = " ".join(column_names("tree"))
    assert all(name in joined for name in feature_names() if name != "amount_log")


# ---- baselines --------------------------------------------------------------------------------


def test_rule_score_is_the_number_of_rules_that_fire():
    df = feature_frame()
    score = RuleBaseline().score(df)
    hits = RuleBaseline().hits(df)
    assert score.tolist() == hits.sum_horizontal().to_list()
    # row 3: burst (3 in 10 min), large_vs_baseline (9x) and rapid_repeat (90 s) -> 3 rules
    assert score[3] == 3 and score[0] == 1  # row 0 is night only


def test_rules_use_no_labels_and_no_fitting():
    df = feature_frame()
    a = RuleBaseline().score(df)
    b = RuleBaseline().score(df.with_columns(label_is_fraud=pl.Series([1, 0, 1, 0, 1, 0])))
    assert np.array_equal(a, b)
    assert len(RULES) == 8


def test_rule_table_precision_and_recall():
    df = feature_frame().with_columns(label_is_fraud=pl.Series([0, 0, 0, 1, 0, 0]))
    table = RuleBaseline().rule_table(df)
    burst = table.filter(pl.col("rule") == "burst").row(0, named=True)
    assert (
        burst["precision"] == 1.0
        and burst["recall"] == 1.0
        and burst["fires_on"] == pytest.approx(1 / 6)
    )
    any_rule = table.filter(pl.col("rule") == "any_rule").row(0, named=True)
    assert any_rule["recall"] == 1.0


def test_prior_baseline_is_constant():
    assert PriorBaseline().score(feature_frame()).tolist() == [0.0] * 6
