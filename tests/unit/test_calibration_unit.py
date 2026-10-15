"""Platt scaling and calibration measures, on data where the right answer is known."""

import numpy as np
import pytest

pytest.importorskip("sklearn")

from mmrisk.models.calibration import (  # noqa: E402
    PlattCalibrator,
    brier,
    ece,
    reliability_table,
    wilson,
)


def _logit(p):
    return np.log(p / (1 - p))


def test_platt_recovers_a_known_miscalibration():
    rng = np.random.default_rng(1)
    true_p = np.clip(rng.beta(0.3, 8, 200_000), 1e-5, 0.999)
    y = (rng.random(len(true_p)) < true_p).astype(int)
    score = 1 / (1 + np.exp(-(0.5 * _logit(true_p) - 1.0)))  # squeezed and shifted
    cal = PlattCalibrator.fit(score, y)
    assert cal.a == pytest.approx(2.0, abs=0.15) and cal.b == pytest.approx(2.0, abs=0.35)
    assert np.mean(np.abs(cal.transform(score) - true_p)) < 0.01
    assert abs(brier(y, cal.transform(score)) - brier(y, true_p)) < 5e-4


def test_transform_is_monotone_and_a_probability():
    cal = PlattCalibrator(a=1.3, b=0.4)
    x = np.linspace(0.0001, 0.9999, 500)
    p = cal.transform(x)
    assert (np.diff(p) > 0).all() and p.min() > 0 and p.max() < 1


def test_identity_link_works_on_counts_with_ties():
    rng = np.random.default_rng(2)
    count = rng.integers(0, 5, 20_000).astype(float)
    y = (rng.random(20_000) < 1 / (1 + np.exp(-(1.2 * count - 6)))).astype(int)
    cal = PlattCalibrator.fit(count, y, link="identity")
    assert cal.a == pytest.approx(1.2, abs=0.15) and cal.b == pytest.approx(-6, abs=0.8)


def test_extreme_scores_do_not_break_the_logit_link():
    cal = PlattCalibrator.fit(
        np.array([0.0, 1e-9, 0.2, 0.9, 1.0, 0.5]), np.array([0, 0, 0, 1, 1, 1])
    )
    assert np.isfinite(cal.transform(np.array([0.0, 1.0]))).all()


def test_one_class_is_rejected():
    with pytest.raises(ValueError, match="both classes"):
        PlattCalibrator.fit(np.array([0.1, 0.2]), np.array([0, 0]))


def test_bad_link_is_rejected():
    with pytest.raises(ValueError, match="link"):
        PlattCalibrator.fit(np.array([0.1, 0.2, 0.3]), np.array([0, 1, 0]), link="probit")


def test_brier_and_wilson_known_values():
    assert brier(np.array([1, 0]), np.array([1.0, 0.0])) == 0.0
    assert brier(np.array([1, 0]), np.array([0.0, 1.0])) == 1.0
    assert brier(np.array([1, 0, 0, 0]), np.full(4, 0.25)) == pytest.approx(0.1875)
    lo, hi = wilson(0, 10)
    assert lo == 0.0 and hi == pytest.approx(0.2775, abs=1e-3)
    assert wilson(0, 0) == (0.0, 1.0)
    lo, hi = wilson(50, 100)
    assert lo < 0.5 < hi


def test_reliability_table_accounts_for_every_row_and_ece_is_zero_when_perfect():
    rng = np.random.default_rng(3)
    p = rng.beta(0.3, 10, 50_000)
    y = (rng.random(50_000) < p).astype(float)
    table = reliability_table(y, p)
    assert sum(r["rows"] for r in table) == 50_000
    assert sum(r["fraud"] for r in table) == int(y.sum())
    assert all(r["ci_low"] <= r["observed"] <= r["ci_high"] for r in table)
    assert ece(y, p) < 0.002  # right probabilities: only sampling noise
    assert ece(y, np.clip(p * 5, 0, 1)) > 5 * ece(y, p)  # five-fold overconfident: much worse
