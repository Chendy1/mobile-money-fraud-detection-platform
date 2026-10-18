"""Explanation helpers: hand-checked contributions, reason codes, bins and permutation."""

import numpy as np
import polars as pl
import pytest

pytest.importorskip("sklearn")

from mmrisk.features.registry import FEATURES, feature_names  # noqa: E402
from mmrisk.models import explain as X  # noqa: E402
from mmrisk.models.preprocess import CATEGORIES, column_names  # noqa: E402

NAMES = feature_names()
COLS = column_names("tree")


def _phi(n=4, seed=0):
    return np.random.default_rng(seed).normal(size=(n, len(COLS)))


def _feat(n_rows, **hot):
    """A (n x 28) contribution matrix of zeros with the given feature columns set."""
    m = np.zeros((n_rows, len(NAMES)))
    for name, vals in hot.items():
        m[:, NAMES.index(name)] = vals
    return m


def _frame(**cols):
    n = len(next(iter(cols.values())))
    base = {f.name: [None] * n for f in FEATURES}
    base["transaction_id"] = [f"T{i}" for i in range(n)]
    base.update(cols)
    return pl.DataFrame(base, strict=False)


# ---- aggregation -----------------------------------------------------------------------------


def test_aggregate_sums_one_hot_and_cyclic_columns_into_their_feature():
    phi = np.zeros((1, len(COLS)))
    for c in COLS:
        if c.startswith("channel="):
            phi[0, COLS.index(c)] = 1.0
        if c in ("hour_sin", "hour_cos"):
            phi[0, COLS.index(c)] = 2.0
    agg = X.aggregate(phi)
    assert agg[0, NAMES.index("channel")] == pytest.approx(len(CATEGORIES["channel"]))
    assert agg[0, NAMES.index("hour")] == pytest.approx(4.0)
    assert agg.sum() == pytest.approx(phi.sum())  # nothing lost or double counted


def test_aggregate_keeps_every_row_total_and_checks_width():
    phi = _phi()
    assert np.allclose(X.aggregate(phi).sum(axis=1), phi.sum(axis=1))
    assert X.aggregate(phi).shape == (4, len(NAMES))
    with pytest.raises(X.ExplainError):
        X.aggregate(phi[:, :-1])


def test_every_feature_is_in_exactly_one_unit_and_has_a_label():
    flat = [f for fs in X.UNITS.values() for f in fs]
    assert sorted(flat) == sorted(NAMES)
    assert X.UNITS["amount"] == ["amount_kes", "amount_log"]
    assert set(X.LABELS) == set(X.UNITS)


def test_units_add_up_the_amount_pair():
    phi = _feat(2, amount_kes=[1.0, 2.0], amount_log=[0.5, -1.0])
    u = X.by_unit(phi)
    assert u["amount"].tolist() == [1.5, 1.0]
    assert "amount_log" not in u


def test_expected_signals_name_real_units():
    for sig in X.EXPECTED_SIGNALS.values():
        assert set(sig) <= set(X.UNITS)


# ---- global importance -----------------------------------------------------------------------


def test_global_importance_by_hand():
    units = {u: np.zeros(4) for u in X.UNITS}
    units["is_night"] = np.array([1.0, -1.0, 1.0, -1.0])  # big but cancels out
    units["hour"] = np.array([0.5, 0.5, 0.5, 0.5])
    g = X.global_importance(units)
    assert g["unit"][0] == "is_night" and g["rank"][0] == 1
    row = g.filter(pl.col("unit") == "is_night").row(0, named=True)
    assert row["mean_abs"] == pytest.approx(1.0) and row["mean_signed"] == pytest.approx(0.0)
    assert g["share"].sum() == pytest.approx(1.0)
    assert g.filter(pl.col("unit") == "hour")["share"][0] == pytest.approx(0.5 / 1.5)


def test_group_importance_sums_absolute_values_inside_a_row():
    units = {u: np.zeros(2) for u in X.UNITS}
    units["txn_count_10m"] = np.array([1.0, 0.0])
    units["txn_count_1h"] = np.array([-1.0, 0.0])  # same group, opposite sign
    g = X.group_importance(units)
    assert g["group"][0] == "velocity"
    assert g["mean_abs"][0] == pytest.approx(1.0)  # (|1| + |-1| + 0) / 2 rows
    assert g["share"].sum() == pytest.approx(1.0)


def test_rank_correlation():
    a = np.array([5.0, 4.0, 3.0, 2.0, 1.0])
    assert X.rank_correlation(a, a * 2) == pytest.approx(1.0)
    assert X.rank_correlation(a, a[::-1]) == pytest.approx(-1.0)
    assert np.isnan(X.rank_correlation(a, np.ones(5)))


# ---- reason codes ----------------------------------------------------------------------------


def test_reason_codes_pick_the_largest_risk_raising_features_in_order():
    df = _frame(is_new_device=[True, False], txn_count_10m=[7, 0], is_night=[False, True])
    phi = _feat(2, is_new_device=[2.0, -3.0], txn_count_10m=[1.0, 0.0], is_night=[-5.0, 0.4])
    out = X.reason_codes(df, phi, a=1.0, top_k=3)
    first = out.filter(pl.col("transaction_id") == "T0").sort("rank")
    assert first["feature"].to_list() == ["is_new_device", "txn_count_10m"]  # -5 is not a reason
    assert first["text"].to_list()[0] == "First use of this device: yes"
    assert first["text"].to_list()[1] == "Transactions in the last 10 minutes: 7"
    second = out.filter(pl.col("transaction_id") == "T1")
    assert second["feature"].to_list() == ["is_night"]  # only one risk-raising feature


def test_reason_codes_are_scaled_by_the_calibration_slope_and_limited_to_top_k():
    df = _frame(is_new_device=[True], txn_count_10m=[7], is_night=[True])
    phi = _feat(1, is_new_device=[2.0], txn_count_10m=[1.0], is_night=[0.5])
    out = X.reason_codes(df, phi, a=1.5, top_k=2)
    assert out["effect"].to_list() == pytest.approx([3.0, 1.5])
    assert out.height == 2


def test_reason_codes_missing_values_and_empty_result_keep_the_schema():
    df = _frame(secs_since_last_inbound=[None])
    phi = _feat(1, secs_since_last_inbound=[1.0])
    out = X.reason_codes(df, phi)
    assert out["text"][0] == "Seconds since money last arrived: no history"
    none = X.reason_codes(df, _feat(1, secs_since_last_inbound=[-1.0]))
    assert none.height == 0 and none.columns == out.columns
    with pytest.raises(X.ExplainError):
        X.reason_codes(df, phi, top_k=0)


def test_amount_reason_is_shown_once_with_the_amount_in_kes():
    df = _frame(amount_kes=[12_345], amount_log=[9.4])
    phi = _feat(1, amount_kes=[0.7], amount_log=[0.6])
    out = X.reason_codes(df, phi)
    assert out.height == 1 and out["effect"][0] == pytest.approx(1.3)
    assert out["text"][0] == "Transaction amount: 12,345"


# ---- dependence and direction ----------------------------------------------------------------


def test_dependence_bins_hold_every_row_and_average_by_hand():
    x = list(range(1, 101))
    df = _frame(avg_amount_30d=[float(v) for v in x])
    eff = np.array(x, dtype=float)
    d = X.dependence(df, eff, "avg_amount_30d", bins=5)
    assert d["n"].sum() == 100 and d.height == 5
    assert d["mean_effect"].to_list() == sorted(d["mean_effect"].to_list())
    first = d.row(0, named=True)
    assert first["mean_effect"] == pytest.approx(np.mean(range(1, 21)), abs=0.6)


def test_a_common_zero_gets_its_own_bin_and_nulls_are_separate():
    vals = [0.0] * 60 + [float(v) for v in range(1, 31)] + [None] * 10
    df = _frame(amount_sum_1h=vals)
    eff = np.concatenate([np.full(60, -1.0), np.arange(1, 31, dtype=float), np.full(10, 9.0)])
    d = X.dependence(df, eff, "amount_sum_1h", bins=5)
    assert d["bin"].to_list()[:2] == ["no history", "0"]
    zero = d.filter(pl.col("bin") == "0").row(0, named=True)
    assert zero["n"] == 60 and zero["mean_effect"] == pytest.approx(-1.0)
    assert d["n"].sum() == 100


def test_dependence_for_flags_and_categories_uses_the_values_themselves():
    df = _frame(is_new_device=[True, True, False, False], channel=["APP", "USSD", "APP", "APP"])
    d = X.dependence(df, np.array([1.0, 3.0, 5.0, 7.0]), "is_new_device")
    assert dict(zip(d["bin"], d["mean_effect"], strict=True)) == {"false": 6.0, "true": 2.0}
    c = X.dependence(df, np.array([1.0, 3.0, 5.0, 7.0]), "channel")
    assert dict(zip(c["bin"], c["n"], strict=True)) == {"APP": 3, "USSD": 1}


def test_direction_is_signed_tie_aware_and_absent_where_meaningless():
    x = np.arange(1.0, 51.0)
    df = _frame(avg_amount_30d=list(x), is_new_device=[True, False] * 25, channel=["APP"] * 50)
    assert X.direction(df, "avg_amount_30d", x * 2) == pytest.approx(1.0)
    assert X.direction(df, "avg_amount_30d", -x) == pytest.approx(-1.0)
    assert X.direction(df, "is_new_device", x) is None  # a flag has no direction to speak of
    assert X.direction(df, "channel", x) is None
    zeros = _frame(amount_sum_1h=[0.0] * 50)
    assert X.direction(zeros, "amount_sum_1h", x) is None


# ---- permutation importance ------------------------------------------------------------------


class _Stub:
    """Scores by column `signal`; ignores everything else."""

    def __init__(self, col="signal"):
        self.col = col

    def score(self, df):
        return df[self.col].to_numpy().astype(float)


def _perm_frame(n=2000, seed=1):
    rng = np.random.default_rng(seed)
    y = (rng.random(n) < 0.1).astype(int)
    signal = y * 2.0 + rng.normal(size=n)
    return pl.DataFrame({"signal": signal, "noise": rng.normal(size=n), "twin": signal}), y


def test_shuffling_the_used_column_costs_ranking_and_an_unused_one_costs_nothing():
    df, y = _perm_frame()
    out = X.permutation_importance(_Stub(), df, y, {"signal": ["signal"], "noise": ["noise"]}, 5)
    drop = dict(zip(out["unit"], out["drop"], strict=True))
    assert drop["signal"] > 0.15
    assert drop["noise"] == pytest.approx(0.0, abs=1e-12)  # not used at all


class _Recorder:
    """Remembers whether the two columns still matched row by row each time it was called."""

    def __init__(self):
        self.matched: list[bool] = []

    def score(self, df):
        self.matched.append(bool((df["signal"] == df["twin"]).all()))
        return df["signal"].to_numpy().astype(float)


def test_shuffling_a_group_moves_its_columns_together():
    df, y = _perm_frame()
    together, alone = _Recorder(), _Recorder()
    X.permutation_importance(together, df, y, {"pair": ["signal", "twin"]}, 3)
    X.permutation_importance(alone, df, y, {"one": ["signal"]}, 3)
    assert together.matched == [True] * 4  # the baseline call and three shuffles
    assert alone.matched[0] is True and alone.matched[1:] == [False] * 3


def test_permutation_is_reproducible_and_reports_the_baseline():
    df, y = _perm_frame()
    a = X.permutation_importance(_Stub(), df, y, {"signal": ["signal"]}, 4, seed=3)
    b = X.permutation_importance(_Stub(), df, y, {"signal": ["signal"]}, 4, seed=3)
    assert a.equals(b)
    assert 0.0 < a["base_pr_auc"][0] <= 1.0


def test_permutation_sets_cover_every_feature_once():
    flat = [c for cols in X.permutation_sets().values() for c in cols]
    assert sorted(flat) == sorted(NAMES)
    grouped = [c for cols in X.permutation_group_sets().values() for c in cols]
    assert sorted(grouped) == sorted(NAMES)
