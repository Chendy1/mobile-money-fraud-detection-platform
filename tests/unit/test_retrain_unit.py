"""Retraining decisions: when to retrain and what a candidate must pass. No model training here."""

from datetime import datetime, timedelta

import numpy as np
import polars as pl
import pytest

pytest.importorskip("sklearn")

from mmrisk.models.costs import CostModel  # noqa: E402
from mmrisk.retrain import gates as G  # noqa: E402
from mmrisk.retrain import trigger  # noqa: E402
from mmrisk.retrain.config import Gates, Triggers  # noqa: E402
from mmrisk.retrain.run import new_fraud_labels  # noqa: E402

# ---- triggers ---------------------------------------------------------------------------------


def decide(**kw):
    base = dict(drift={"drift_detected": False}, model_age_days=10.0, new_fraud_labels=5)
    return trigger.evaluate(**{**base, **kw})


def fired(d):
    return {t.name: t.fired for t in d.triggers}


def test_nothing_fires_on_a_healthy_model():
    d = decide()
    assert not d.retrain
    assert fired(d) == {"drift": False, "model_age": False, "new_labels": False, "forced": False}


def test_drift_fires():
    d = decide(drift={"drift_detected": True, "window": "a to b", "verdict": "MAJOR"})
    assert d.retrain and fired(d)["drift"] is True
    assert "MAJOR" in next(t for t in d.triggers if t.name == "drift").detail


def test_a_demonstration_drift_report_is_ignored():
    demo = {"drift_detected": True, "verdict": "MAJOR", "injected": "amounts multiplied by 3"}
    d = decide(drift=demo)
    assert not d.retrain and fired(d)["drift"] is None
    assert "demonstration" in next(t for t in d.triggers if t.name == "drift").detail
    assert decide(drift={**demo, "injected": None}).retrain  # a real report still counts


def test_age_limit_is_exclusive():
    th = Triggers(max_model_age_days=90)
    assert fired(decide(model_age_days=90.0, th=th))["model_age"] is False
    assert fired(decide(model_age_days=90.01, th=th))["model_age"] is True


def test_label_limit_is_inclusive():
    th = Triggers(min_new_fraud_labels=200)
    assert fired(decide(new_fraud_labels=199, th=th))["new_labels"] is False
    d = decide(new_fraud_labels=200, th=th)
    assert fired(d)["new_labels"] is True and d.retrain


def test_missing_inputs_are_unknown_not_false_and_do_not_retrain():
    d = decide(drift=None, model_age_days=None, new_fraud_labels=None)
    assert fired(d) == {"drift": None, "model_age": None, "new_labels": None, "forced": False}
    assert not d.retrain


def test_force_retrains_even_when_unknown():
    d = decide(drift=None, model_age_days=None, new_fraud_labels=None, force=True)
    assert d.retrain and fired(d)["forced"] is True


def test_new_fraud_labels_counts_only_new_known_frauds():
    t0 = datetime(2025, 6, 1)
    txns = pl.DataFrame({
        "transaction_id": ["a", "b", "c", "d", "e"],
        "timestamp": [t0, t0 + timedelta(days=1), t0 + timedelta(days=2),
                      t0 + timedelta(days=3), t0 + timedelta(days=4)],
    })  # fmt: skip
    labels = pl.DataFrame({
        "transaction_id": ["a", "b", "c", "d", "e"],
        "label_is_fraud": [1, 1, 1, 0, 1],
        "label_available_at": [t0 + timedelta(days=d) for d in (5, 5, 5, 5, 40)],
    })  # fmt: skip
    # a is not after `since`; d is not fraud; e's label is not known yet at as_of; b and c count
    assert new_fraud_labels(labels, txns, since=t0, as_of=t0 + timedelta(days=10)) == 2
    assert (
        new_fraud_labels(labels, txns, since=t0 + timedelta(days=1), as_of=t0 + timedelta(days=10))
        == 1
    )
    assert new_fraud_labels(labels, txns, since=t0, as_of=t0 + timedelta(days=4)) == 0


# ---- gates ------------------------------------------------------------------------------------


def test_data_gate_boundaries_and_message():
    th = Gates(min_train_fraud=100, min_val_fraud=30, min_test_fraud=30)
    assert G.data_gates(100, 30, 30, th)[0].passed
    short = G.data_gates(100, 29, 30, th)[0]
    assert not short.passed and "validation 29 < 30" in short.detail and short.required


def test_fair_comparison_gate():
    t = datetime(2025, 10, 1)
    assert G.fair_comparison_gate(t - timedelta(days=1), t).passed
    assert not G.fair_comparison_gate(t, t).passed  # trained up to the test start: overlap
    unknown = G.fair_comparison_gate(None, t)
    assert not unknown.passed and unknown.required


@pytest.fixture(scope="module")
def world():
    rng = np.random.default_rng(0)
    n = 30_000
    y = (rng.random(n) < 0.03).astype(float)
    signal = rng.normal(size=n) + 2.5 * y
    p = 1 / (1 + np.exp(-(signal * 1.2 - 4.2)))
    return {
        "y": y, "amount": rng.lognormal(7, 1, n).round(), "customers": rng.integers(0, 3000, n),
        "good": p, "noise": np.clip(rng.random(n) * 0.2, 0, 1),
    }  # fmt: skip


def run(world, champion, candidate, **th):
    return G.compare(
        y=world["y"], amount=world["amount"], customers=world["customers"],
        champion_p=champion, candidate_p=candidate, cost_model=CostModel(),
        th=Gates(n_boot=60, **th), seed=3,
    )  # fmt: skip


def by_name(gates):
    return {g.name: g for g in gates}


def test_identical_models_pass_everything(world):
    gates, nums = run(world, world["good"], world["good"])
    assert G.approved(gates) and all(g.passed for g in gates)
    g = by_name(gates)
    assert g["pr_auc"].value == 0 and g["cost"].value == 0 and g["calibration"].value == 0
    assert g["flagged_share"].value == 1.0 and g["agreement"].value == 1.0
    assert nums["pr_auc_diff_interval"] == [0.0, 0.0]


def test_a_much_worse_candidate_fails_accuracy_and_cost(world):
    gates, nums = run(world, world["good"], world["noise"])
    g = by_name(gates)
    assert not g["pr_auc"].passed and g["pr_auc"].value < -0.1
    assert not g["cost"].passed and g["cost"].value > 0
    assert not G.approved(gates)
    assert {x.name for x in G.blocking(gates)} >= {"pr_auc", "cost"}
    assert nums["pr_auc"]["candidate"] < nums["pr_auc"]["champion"]


def test_a_better_candidate_passes(world):
    gates, _ = run(world, world["noise"], world["good"])
    g = by_name(gates)
    assert g["pr_auc"].passed and g["pr_auc"].value > 0.1
    assert g["cost"].passed and g["cost"].value < 0


def test_pr_auc_margin_is_a_real_threshold(world):
    p = world["good"].copy()
    worse = np.where(np.arange(len(p)) % 2 == 0, p, np.clip(p * 0.3, 0, 1))
    loose, _ = run(world, p, worse, pr_auc_margin=1.0)
    tight, _ = run(world, p, worse, pr_auc_margin=0.0)
    assert by_name(loose)["pr_auc"].passed and not by_name(tight)["pr_auc"].passed


def test_calibration_gate_uses_the_margin(world):
    over = np.clip(world["good"] * 3, 0, 1)  # same ranking, wrong probabilities
    gates, _ = run(world, world["good"], over, pr_auc_margin=1.0, cost_margin=100.0,
                   flagged_ratio_max=1e9)  # fmt: skip
    cal = by_name(gates)["calibration"]
    assert not cal.passed and cal.value > 0.005
    relaxed, _ = run(world, world["good"], over, pr_auc_margin=1.0, cost_margin=100.0,
                     flagged_ratio_max=1e9, ece_margin=1.0)  # fmt: skip
    assert by_name(relaxed)["calibration"].passed


def test_flagged_share_band_and_agreement_is_advisory(world):
    everything = np.full(len(world["y"]), 0.99)  # blocks (nearly) every transaction
    gates, _ = run(world, world["good"], everything)
    g = by_name(gates)
    assert not g["flagged_share"].passed and g["flagged_share"].required
    assert not g["agreement"].passed and not g["agreement"].required
    only_agreement = [x for x in gates if x.name == "agreement"]
    assert G.approved(only_agreement)  # an advisory failure alone never blocks
    assert G.blocking(only_agreement) == []


def test_flagged_share_with_a_champion_that_flags_nothing(world):
    nothing = np.zeros(len(world["y"]))
    gates, _ = run(world, nothing, world["good"])
    g = by_name(gates)["flagged_share"]
    assert not g.passed and g.value is None


def test_roundtrip_gate():
    a = np.array([0.1, 0.5, 0.9])
    assert G.roundtrip_gate(a, a.copy()).passed
    assert not G.roundtrip_gate(a, a + 1e-6).passed
    assert not G.roundtrip_gate(a, np.array([0.1, np.nan, 0.9])).passed
    assert not G.roundtrip_gate(a, np.array([0.1, 0.5, 1.5])).passed
    assert not G.roundtrip_gate(a, a[:2]).passed


def test_label_known_exactly_at_as_of_counts():
    t0 = datetime(2025, 6, 1)
    txns = pl.DataFrame({"transaction_id": ["a"], "timestamp": [t0 + timedelta(days=1)]})
    labels = pl.DataFrame({
        "transaction_id": ["a"], "label_is_fraud": [1],
        "label_available_at": [t0 + timedelta(days=5)],
    })  # fmt: skip
    assert new_fraud_labels(labels, txns, since=t0, as_of=t0 + timedelta(days=5)) == 1


def _moderately_worse(world):
    p = world["good"]
    return np.where(np.arange(len(p)) % 3 == 0, np.clip(p * 0.2, 0, 1), p)


def test_pr_auc_gate_judges_the_lower_bound_of_the_interval(world):
    worse = _moderately_worse(world)
    _, nums = run(world, world["good"], worse)
    lo, hi = nums["pr_auc_diff_interval"]
    mid = (lo + hi) / 2
    assert lo < mid < hi and mid < 0  # the interval straddles a margin we can choose
    margin = -mid
    kw = dict(pr_auc_margin=margin, cost_margin=1e6, ece_margin=1.0, flagged_ratio_max=1e9)
    assert not by_name(run(world, world["good"], worse, **kw)[0])["pr_auc"].passed
    kw["pr_auc_margin"] = -lo + 1e-9
    assert by_name(run(world, world["good"], worse, **kw)[0])["pr_auc"].passed


def test_cost_gate_judges_the_upper_bound_of_the_interval(world):
    worse = _moderately_worse(world)
    _, nums = run(world, world["good"], worse)
    lo, hi = nums["cost_diff_interval"]
    champ = nums["cost_per_10k"]["champion"]
    mid = (lo + hi) / 2
    assert lo < mid < hi and mid > 0
    kw = dict(pr_auc_margin=1.0, ece_margin=1.0, flagged_ratio_max=1e9)
    assert not by_name(run(world, world["good"], worse, cost_margin=mid / champ, **kw)[0])[
        "cost"
    ].passed
    assert by_name(run(world, world["good"], worse, cost_margin=hi / champ + 1e-9, **kw)[0])[
        "cost"
    ].passed


def test_flagging_far_fewer_than_the_champion_also_fails(world):
    quiet = np.clip(world["good"] * 0.1, 0, 1)
    gates, nums = run(world, world["good"], quiet)
    g = by_name(gates)["flagged_share"]
    assert g.value < 0.5 and not g.passed
