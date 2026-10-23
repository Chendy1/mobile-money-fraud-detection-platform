"""PSI and the binning behind it, checked against hand calculations and an independent version."""

import math

import numpy as np
import polars as pl
import pytest

from mmrisk.drift import stats
from mmrisk.drift.stats import (
    EPS,
    DriftStatsError,
    compare,
    current_shares,
    noise_floor,
    psi,
    summarise,
)


def series(values, dtype=None):
    return pl.Series("x", values, dtype=dtype)


# ---- summarise ----


def test_numeric_bins_are_quantiles_and_shares_add_up():
    values = series(np.arange(1000, dtype=float))
    s = summarise(values)
    assert s["kind"] == "numeric" and len(s["edges"]) == stats.N_BINS - 1
    assert sum(s["shares"]) + s["missing_share"] == pytest.approx(1.0)
    assert all(share == pytest.approx(0.1, abs=0.002) for share in s["shares"])
    assert s["edges"] == sorted(s["edges"]) and s["n"] == 1000


def test_ties_collapse_the_bins_instead_of_making_empty_ones():
    values = series([0.0] * 800 + list(np.arange(1, 201, dtype=float)))
    s = summarise(values)
    assert s["kind"] == "numeric" and len(s["edges"]) < stats.N_BINS - 1
    assert len(set(s["edges"])) == len(s["edges"])
    assert all(share > 0 for share in s["shares"])  # no empty bin to inflate the PSI


def test_missing_values_have_their_own_share():
    values = series([1.0, 2.0, 3.0, None] * 100 + list(np.arange(200.0)))
    s = summarise(values)
    assert s["missing_share"] == pytest.approx(100 / 600)
    assert sum(s["shares"]) == pytest.approx(500 / 600)


def test_few_distinct_values_are_treated_as_categories():
    s = summarise(series([1, 2, 3, 1, 1, 2] * 50))
    assert s["kind"] == "discrete" and s["labels"] == ["1", "2", "3"]
    assert s["shares"] == pytest.approx([0.5, 1 / 3, 1 / 6])


def test_a_constant_column_can_still_show_a_new_value():
    ref = summarise(series([5.0] * 1000))
    assert ref["kind"] == "discrete" and ref["labels"] == ["5"]
    assert compare(series([5.0] * 500 + [9.0] * 500), ref)["psi"] > 1.0


def test_booleans_and_strings_and_integer_floats_get_readable_labels():
    assert summarise(series([True, False, True]))["labels"] == ["true", "false"]
    assert summarise(series(["B", "A", "A"]))["labels"] == ["A", "B"]
    assert summarise(series([3.0, 3.0, 4.0]))["labels"] == ["3", "4"]


def test_equal_counts_are_ordered_by_label_so_the_summary_is_reproducible():
    assert summarise(series(["b", "a", "c"]))["labels"] == ["a", "b", "c"]


def test_an_empty_or_all_missing_column():
    with pytest.raises(DriftStatsError):
        summarise(series([], dtype=pl.Float64))
    s = summarise(series([None, None], dtype=pl.Float64))
    assert s["missing_share"] == 1.0 and s["shares"] == []


# ---- counting a current sample into the reference bins ----


def test_a_value_on_an_edge_falls_in_the_lower_bin():
    ref = {"kind": "numeric", "edges": [10.0, 20.0], "shares": [0.3, 0.4, 0.3],
           "missing_share": 0.0, "n": 100}  # fmt: skip
    shares, missing, unseen = current_shares(series([10.0, 10.5, 20.0, 20.5, 5.0]), ref)
    assert shares[:3] == pytest.approx([0.4, 0.4, 0.2]) and unseen == 0.0 and missing == 0.0


def test_values_beyond_the_reference_range_go_to_the_outer_bins():
    ref = summarise(series(np.arange(1000, dtype=float)))
    shares, _, _ = current_shares(series([-1e9, 1e9]), ref)
    assert shares[0] == 0.5 and shares[len(ref["edges"])] == 0.5


def test_unseen_categories_get_their_own_bin_and_everything_adds_up():
    ref = summarise(series(["A"] * 80 + ["B"] * 20))
    shares, missing, unseen = current_shares(series(["A", "B", "C", "D", None]), ref)
    assert unseen == pytest.approx(0.4) and missing == pytest.approx(0.2)
    assert sum(shares) + missing == pytest.approx(1.0) and shares[:2] == pytest.approx([0.2, 0.2])


def test_an_empty_current_sample_is_refused():
    ref = summarise(series(np.arange(100.0)))
    with pytest.raises(DriftStatsError):
        current_shares(series([], dtype=pl.Float64), ref)


# ---- psi ----


def test_identical_shares_give_zero():
    assert psi([0.2, 0.3, 0.5], [0.2, 0.3, 0.5]) == 0.0


def test_a_hand_calculated_value():
    expected = (0.5 - 0.4) * math.log(0.5 / 0.4) + (0.5 - 0.6) * math.log(0.5 / 0.6)
    assert psi([0.4, 0.6], [0.5, 0.5]) == pytest.approx(expected)
    assert expected == pytest.approx(0.04055, abs=1e-4)


def test_psi_is_symmetric_and_never_negative():
    rng = np.random.default_rng(1)
    for _ in range(50):
        a, b = rng.dirichlet(np.ones(6)), rng.dirichlet(np.ones(6))
        assert psi(list(a), list(b)) == pytest.approx(psi(list(b), list(a)))
        assert psi(list(a), list(b)) >= 0


def test_an_empty_bin_is_floored_at_one_in_ten_thousand():
    ref, cur = [0.5, 0.5, 0.0], [0.4, 0.4, 0.2]
    floor = 1e-4  # the documented floor, written out so a changed constant is noticed
    want = (0.4 - 0.5) * math.log(0.4 / 0.5) * 2 + (0.2 - floor) * math.log(0.2 / floor)
    assert psi(ref, cur) == pytest.approx(want)
    assert psi([0.5, 0.5, 0.0], [0.5, 0.5, 0.0]) == 0.0
    assert EPS == floor


def test_different_bin_counts_are_refused():
    with pytest.raises(DriftStatsError):
        psi([0.5, 0.5], [1.0])


# ---- compare: sensitivity, noise and an independent implementation ----


def test_two_samples_of_one_distribution_sit_near_the_noise_level():
    rng = np.random.default_rng(7)
    ref = summarise(series(rng.normal(size=20000)))
    values = []
    for _ in range(20):
        c = compare(series(rng.normal(size=1000)), ref)
        values.append(c["psi"])
        assert c["noise"] == pytest.approx(noise_floor(10, 20000, 1000), rel=0.15)
    assert 0.3 * c["noise"] < np.mean(values) < 3 * c["noise"]
    assert max(values) < 0.1  # never mistaken for a moderate shift


@pytest.mark.parametrize(
    ("shift", "level"), [(0.0, "none"), (0.15, "none"), (0.4, "moderate"), (1.5, "major")]
)
def test_a_mean_shift_is_found_in_proportion_to_its_size(shift, level):
    rng = np.random.default_rng(3)
    ref = summarise(series(rng.normal(size=20000)))
    value = compare(series(rng.normal(loc=shift, size=5000)), ref)["psi"]
    if level == "none":
        assert value < 0.1
    elif level == "moderate":
        assert 0.1 <= value < 0.25
    else:
        assert value >= 0.25


def test_the_numeric_psi_equals_an_independent_calculation():
    rng = np.random.default_rng(11)
    a = rng.gamma(2.0, 100.0, size=15000)
    b = rng.gamma(2.4, 110.0, size=4000)
    ref = summarise(series(a))
    got = compare(series(b), ref)["psi"]
    edges = np.array(ref["edges"])
    ca = np.bincount(np.searchsorted(edges, a, side="left"), minlength=len(edges) + 1) / len(a)
    cb = np.bincount(np.searchsorted(edges, b, side="left"), minlength=len(edges) + 1) / len(b)
    want = float(np.sum((cb - ca) * np.log(cb / ca)))
    assert got == pytest.approx(want, rel=1e-9)


def test_new_categories_and_a_missing_share_move_the_psi():
    ref = summarise(series(["A"] * 900 + ["B"] * 100))
    same = compare(series(["A"] * 900 + ["B"] * 100), ref)
    new = compare(series(["A"] * 800 + ["B"] * 100 + ["Z"] * 100), ref)
    assert same["psi"] == pytest.approx(0.0, abs=1e-12) and new["unseen_share"] == pytest.approx(
        0.1
    )
    assert new["psi"] > 0.25
    gone = compare(series(["A"] * 450 + ["B"] * 50 + [None] * 500), ref)
    assert gone["missing_share_current"] == 0.5 and gone["psi"] > 0.25
    # the missing bin is part of the sum: ref shares (0.9, 0.1, 0 unseen, 0 missing) against
    # (0.45, 0.05, 0, 0.5)
    e = 1e-4
    want = (
        (0.45 - 0.9) * math.log(0.45 / 0.9)
        + (0.05 - 0.1) * math.log(0.05 / 0.1)
        + (0.5 - e) * math.log(0.5 / e)
    )
    assert gone["psi"] == pytest.approx(want)


def test_noise_floor_formula_and_guard():
    assert noise_floor(10, 20000, 1000) == pytest.approx(9 * (1 / 20000 + 1 / 1000))
    assert noise_floor(1, 100, 100) == 0.0
    assert noise_floor(5, 0, 100) == math.inf and noise_floor(5, 100, 0) == math.inf
