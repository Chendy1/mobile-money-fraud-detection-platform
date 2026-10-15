"""The cost model: hand-computed decisions, monotonicity, and optimality when p is right."""

import numpy as np
import pytest

from mmrisk.models.costs import ACTIONS, RISK_LEVELS, CostModel, CostModelError

CM = CostModel()  # loss 1.0, review 25 KES stopping 70%, block 150 KES


def test_expected_costs_by_hand():
    c = CM.expected_costs(np.array([0.1]), np.array([1000.0]))[0]
    assert c[0] == pytest.approx(100.0)  # allow: 0.1 * 1000
    assert c[1] == pytest.approx(25 + 0.1 * 0.3 * 1000)  # review: 25 + 30 = 55
    assert c[2] == pytest.approx(0.9 * 150)  # block: 135


@pytest.mark.parametrize(
    ("p", "amount", "action"),
    [
        (0.001, 100, 0),
        (0.1, 1000, 1),
        (0.5, 1000, 2),
        (0.001, 100_000, 1),
        (0.01, 100_000, 2),
        (0.9, 50, 2),
    ],
)
def test_decisions_by_hand(p, amount, action):
    assert CM.decide(np.array([p]), np.array([float(amount)]))[0] == action


def test_thresholds_match_the_closed_form():
    review, block = CM.probability_thresholds(1000)
    assert review == pytest.approx(25 / (0.7 * 1000), abs=1e-3)  # review beats allow
    assert block == pytest.approx(125 / 450, abs=1e-3)  # (1-p)*150 = 25 + 300 p
    assert CM.probability_thresholds(10_000)[0] < review  # larger amount, stricter


def test_a_larger_amount_never_gets_a_milder_action():
    p = np.repeat(np.linspace(0, 1, 101), 50)
    amounts = np.tile(np.logspace(1, 6, 50), 101)
    a = CM.decide(p, amounts).reshape(101, 50)
    assert (np.diff(a, axis=1) >= 0).all()


def test_a_higher_probability_never_gets_a_milder_action():
    p = np.tile(np.linspace(0, 1, 201), 40)
    amounts = np.repeat(np.logspace(1, 6, 40), 201)
    a = CM.decide(p, amounts).reshape(40, 201)
    assert (np.diff(a, axis=1) >= 0).all()


def test_ties_go_to_the_least_severe_action():
    free_review = CostModel(review_cost=0.0, review_prevents=0.0)  # review == allow
    # at p = 1% on 500 KES: allow = review = 5, block = 148.5
    assert free_review.decide(np.array([0.01]), np.array([500.0]))[0] == 0


def test_realized_cost_by_hand():
    amount = np.array([1000.0])
    one, zero = np.array([1.0]), np.array([0.0])
    assert CM.realized_cost(np.array([0]), one, amount)[0] == 1000  # fraud allowed: lose it
    assert CM.realized_cost(np.array([0]), zero, amount)[0] == 0
    assert CM.realized_cost(np.array([1]), one, amount)[0] == pytest.approx(25 + 300)
    assert CM.realized_cost(np.array([1]), zero, amount)[0] == 25  # review always costs
    assert CM.realized_cost(np.array([2]), one, amount)[0] == 0  # blocked fraud stops
    assert CM.realized_cost(np.array([2]), zero, amount)[0] == 150  # blocked legit: friction


def test_realized_equals_expected_when_the_label_is_the_probability():
    rng = np.random.default_rng(0)
    y = rng.integers(0, 2, 500).astype(float)
    amount = rng.uniform(50, 20_000, 500)
    actions = rng.integers(0, 3, 500)
    expected = CM.expected_costs(y, amount)[np.arange(500), actions]
    assert np.allclose(CM.realized_cost(actions, y, amount), expected)


def test_allow_all_and_oracle():
    y = np.array([1.0, 0.0, 1.0, 0.0])
    amount = np.array([100.0, 500.0, 2000.0, 80.0])
    assert CM.allow_all_cost(y, amount).sum() == 2100  # the fraud amounts
    assert CM.oracle_cost(y, amount).sum() == 0
    scaled = CostModel(loss_rate=0.5)
    assert scaled.allow_all_cost(y, amount).sum() == 1050


def test_scaled_changes_only_the_friction_costs():
    s = CM.scaled(2.0)
    assert (s.review_cost, s.block_cost) == (50.0, 300.0)
    assert (s.loss_rate, s.review_prevents) == (CM.loss_rate, CM.review_prevents)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"loss_rate": 0},
        {"loss_rate": 1.5},
        {"review_prevents": 1.2},
        {"review_cost": -1},
        {"block_cost": -5},
    ],
)
def test_bad_parameters_are_rejected(kwargs):
    with pytest.raises(CostModelError):
        CostModel(**kwargs)


def test_names_line_up_with_risk_levels():
    assert ACTIONS == ("ALLOW", "REVIEW", "BLOCK") and RISK_LEVELS == ("LOW", "MEDIUM", "HIGH")


def test_the_cost_rule_beats_fixed_cutoffs_when_probabilities_are_right():
    """With true probabilities, the cost rule should cost no more than any fixed cut-off."""
    rng = np.random.default_rng(7)
    n = 400_000
    p = rng.beta(0.15, 12, n)  # a rare-event probability distribution
    y = (rng.random(n) < p).astype(float)
    amount = np.exp(rng.normal(7, 1.2, n))
    best = CM.realized_cost(CM.decide(p, amount), y, amount).sum()
    for cut in (0.01, 0.03, 0.1, 0.3, 0.5):
        for level in (1, 2):
            fixed = CM.realized_cost(np.where(p >= cut, level, 0), y, amount).sum()
            assert best <= fixed * 1.005, (cut, level)
    assert best < CM.allow_all_cost(y, amount).sum()
