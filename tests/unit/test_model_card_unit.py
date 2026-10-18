"""The model card is built only from saved files and says so when one is missing."""

import json

import pytest

from mmrisk.models import model_card as C


def _part(name, fraud):
    return {
        "part": name,
        "first": "2025-01-01 00:00:00",
        "last": "2025-02-01 00:00:00",
        "rows": 1000,
        "fraud": fraud,
        "fraud_rate": fraud / 1000,
        "customers": 100,
    }


def _unit(name, value):
    return {"unit": name, "mean_abs": value}


def _write(d, drop=None):
    scorers = list(C.NAMES)
    costs = ["allow_all", "oracle", "rules", "logistic_regression", "random_forest", "lightgbm"]
    adv = {
        "test_pr_auc": {k: 0.1 + i / 10 for i, k in enumerate(scorers)},
        "validation_pr_auc": {k: 0.2 + i / 10 for i, k in enumerate(scorers)},
        "cost_per_10k": {k: {"point": 1000.0 * (i + 1)} for i, k in enumerate(costs + ["naive"])},
        # the same key shapes run_advanced writes: ece by plain name, brier with " calibrated"
        "calibration": {
            "ece": {**{k: 0.001 for k in scorers}, "lightgbm raw": 0.002},
            "brier": {
                **{f"{k} calibrated": 0.002 for k in scorers},
                "constant (training fraud rate)": 0.004,
            },
        },
        "cost_model_ASSUMED": {
            "loss_rate": 1.0,
            "review_cost": 25.0,
            "review_prevents": 0.7,
            "block_cost": 150.0,
        },
    }
    meta = {
        "feature_version": "1.0",
        "created_at": "2026-10-18T10:00:00+00:00",
        "rounds": 300,
        "scorer_params": {"num_leaves": 8, "min_child_samples": 60, "pos_weight": 1.0},
        "calibrator": {"a": 1.14, "b": 1.85, "link": "logit"},
    }
    slices = [
        {"slice": "channel", "value": "APP", "n": 600, "legit_flagged_per_10k": 300.0},
        {"slice": "channel", "value": "USSD", "n": 800, "legit_flagged_per_10k": 100.0},
    ]
    exp = {
        "split_summary": [_part("train", 20), _part("validation", 6), _part("test", 4)],
        "split_bounds": {"train_end": "2025-04-01", "val_start": "2025-05-15"},
        "importance_all": [_unit("a_feature", 0.5)] * 3,
        "importance_fraud_rows": [_unit("b_feature", 1.0)] * 3,
        "permutation": [{"unit": "c_feature", "drop": 0.1}] * 3,
        "agreement": {"top10_overlap_val_test": 9},
        "additivity_error_max": 1e-14,
        "n_flagged": 77,
        "slices": slices,
    }
    files = {"risk_model.json": meta, "advanced_metrics.json": adv, "explain_metrics.json": exp}
    for name, content in files.items():
        if name != drop:
            (d / name).write_text(json.dumps(content))
    return adv


def test_card_reads_its_numbers_from_the_saved_files(tmp_path):
    adv = _write(tmp_path)
    text = C.build(tmp_path)
    test_ap, val_ap = adv["test_pr_auc"]["lightgbm"], adv["validation_pr_auc"]["lightgbm"]
    row = f"| LightGBM (shipped) | {test_ap:.3f} | {val_ap:.3f} |"
    assert (
        row in text
    )  # the test column is the test number, the validation column the validation one
    assert "1.14 x raw log-odds +1.85" in text
    assert "`a_feature`" in text and "`b_feature`" in text and "`c_feature`" in text
    assert "77 flagged" in text
    assert "channel = APP (300 per 10,000)" in text and "channel = USSD (100 per 10,000)" in text
    assert "Synthetic data only" in text and "ASSUMED" in text
    lgbm_row = next(line for line in text.splitlines() if line.startswith("| LightGBM (shipped)"))
    assert "0.0010" in lgbm_row and "0.00200" in lgbm_row and "n/a" not in lgbm_row


@pytest.mark.parametrize("missing", list(C.CARD_FILES))
def test_card_refuses_to_be_written_without_every_input(tmp_path, missing):
    _write(tmp_path, drop=missing)
    with pytest.raises(C.CardError, match=missing):
        C.build(tmp_path)
    with pytest.raises(C.CardError):
        C.write(tmp_path, tmp_path / "card.md")
    assert not (tmp_path / "card.md").exists()
