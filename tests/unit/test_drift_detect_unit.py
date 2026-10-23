"""The reference file and the comparison rules: thresholds, policies and the decision."""

import json
from datetime import datetime

import numpy as np
import polars as pl
import pytest

from mmrisk.drift import config as cfg
from mmrisk.drift import reference as refmod
from mmrisk.drift.detect import FAIL, PASS, SKIPPED, WARN, detect, level, policy_of, ranked
from mmrisk.drift.report import markdown, text, verdict
from mmrisk.drift.stats import DriftStatsError

W = (datetime(2025, 1, 1), datetime(2025, 2, 15))
N_REF = 20000


def make(n, seed, *, shift=0.0, extra=None):
    """A frame with one feature of each kind and a score."""
    rng = np.random.default_rng(seed)
    df = pl.DataFrame(
        {
            "stable": rng.normal(size=n),
            "moved": rng.normal(loc=shift, size=n),
            "cat": rng.choice(["A", "B", "C"], size=n, p=[0.6, 0.3, 0.1]),
            "day_of_week": rng.integers(1, 8, size=n),
            "is_salary_window": rng.random(n) < 0.3,
            "n_prior_txns": rng.integers(0, 500, size=n) + int(shift * 100),
        }
    )
    if extra:
        df = df.with_columns(**extra)
    return df, pl.Series("score", rng.beta(1, 30, size=n))


NAMES = ["stable", "moved", "cat", "day_of_week", "is_salary_window", "n_prior_txns"]


@pytest.fixture(scope="module")
def ref():
    feats, score = make(N_REF, 1)
    return refmod.build(feats, score, NAMES, W, "1.0")


def by(d):
    return {r.name: r for r in [*d.features, d.score]}


# ---- levels and policies ----


def test_levels_use_the_thresholds_inclusively():
    assert level(cfg.PSI_WARN - 1e-9) == PASS and level(cfg.PSI_WARN) == WARN
    assert level(cfg.PSI_FAIL - 1e-9) == WARN and level(cfg.PSI_FAIL) == FAIL


def test_policies():
    assert policy_of("is_salary_window") == "calendar" and policy_of("day_of_week") == "calendar"
    assert (
        policy_of("n_prior_txns") == "accumulating"
        and policy_of("account_age_days") == "accumulating"
    )
    assert policy_of("amount_kes") == "monitored"


# ---- the reference ----


def test_the_reference_holds_summaries_and_no_rows(ref):
    assert ref["rows"] == N_REF and set(ref["features"]) == set(NAMES) and "score" in ref
    blob = json.dumps(ref)
    assert len(blob) < 20_000  # a few numbers per feature
    assert ref["window"] == {"from": W[0].isoformat(), "to": W[1].isoformat()}


def test_a_reference_round_trips_through_a_file_without_leaving_temporary_files(ref, tmp_path):
    path = tmp_path / "d" / "reference.json"
    refmod.save(ref, path)
    assert refmod.load(path) == ref
    assert [p.name for p in path.parent.iterdir()] == ["reference.json"]


def test_building_refuses_bad_input():
    feats, score = make(100, 1)
    with pytest.raises(DriftStatsError, match="no transactions"):
        refmod.build(feats.head(0), score.head(0), NAMES, W, "1.0")
    with pytest.raises(DriftStatsError, match="one score"):
        refmod.build(feats, score.head(5), NAMES, W, "1.0")
    with pytest.raises(DriftStatsError, match="not found"):
        refmod.build(feats, score, [*NAMES, "nope"], W, "1.0")


@pytest.mark.parametrize(
    "content",
    ["{not json", "[]", '{"format": 2, "features": {"a": {}}, "score": {}}',
     '{"format": 1, "features": {}, "score": {}}',
     '{"format": 1, "features": {"a": {"kind": "numeric"}}, "score": {}}'],
)  # fmt: skip
def test_loading_refuses_files_it_did_not_write(tmp_path, content):
    path = tmp_path / "r.json"
    path.write_text(content)
    with pytest.raises(refmod.ReferenceFileError):
        refmod.load(path)
    with pytest.raises(refmod.ReferenceFileError):
        refmod.load(tmp_path / "missing.json")


def test_loading_refuses_a_summary_without_its_bins(ref, tmp_path):
    broken = json.loads(json.dumps(ref))
    del broken["features"]["stable"]["edges"]
    path = tmp_path / "r.json"
    path.write_text(json.dumps(broken))
    with pytest.raises(refmod.ReferenceFileError, match="bin edges"):
        refmod.load(path)
    broken = json.loads(json.dumps(ref))
    del broken["features"]["cat"]["labels"]
    path.write_text(json.dumps(broken))
    with pytest.raises(refmod.ReferenceFileError, match="labels"):
        refmod.load(path)


# ---- detecting ----


def test_a_new_sample_of_the_same_data_shows_no_drift(ref):
    feats, score = make(3000, 2)
    d = detect(ref, feats, score, days=40)
    assert not d.drift_detected and all(r.status in (PASS, SKIPPED) for r in [*d.features, d.score])
    assert verdict(d) == "no drift detected"


def test_the_reference_compared_with_itself_has_zero_psi(ref):
    feats, score = make(N_REF, 1)
    d = detect(ref, feats, score, days=40)
    assert all(r.psi == pytest.approx(0.0, abs=1e-12) for r in [*d.features, d.score])


def test_a_shifted_monitored_feature_is_a_major_drift_and_is_named(ref):
    feats, score = make(3000, 2, shift=1.5)
    d = detect(ref, feats, score, days=40)
    r = by(d)["moved"]
    assert r.status == FAIL and r.psi >= cfg.PSI_FAIL and r.policy == "monitored"
    assert d.drift_detected and "moved" in verdict(d) and verdict(d).startswith("DRIFT DETECTED")
    assert by(d)["stable"].status == PASS


def test_a_moderate_shift_warns_but_does_not_declare_drift(ref):
    feats, score = make(5000, 2, shift=0.4)
    d = detect(ref, feats, score, days=40)
    assert by(d)["moved"].status == WARN and not d.drift_detected
    assert verdict(d) == "no major drift; moderate shift in moved"


def test_a_change_in_the_score_alone_is_counted(ref):
    feats, _ = make(3000, 2)
    score = pl.Series("score", np.random.default_rng(5).beta(3, 20, size=3000))
    d = detect(ref, feats, score, days=40)
    assert d.score.status == FAIL and d.drift_detected and d.score.policy == "score"
    assert all(r.status != FAIL for r in d.features if r.policy == "monitored")


def test_accumulating_features_are_shown_but_do_not_declare_drift(ref):
    feats, score = make(3000, 2, shift=0.0, extra={"n_prior_txns": pl.col("n_prior_txns") + 400})
    d = detect(ref, feats, score, days=40)
    r = by(d)["n_prior_txns"]
    assert r.status == FAIL and r.policy == "accumulating" and "not counted" in r.detail
    assert not d.drift_detected and r.name not in {c.name for c in d.counted}
    assert verdict(d) == "no drift detected"


def test_calendar_features_wait_for_a_full_cycle(ref):
    feats, score = make(3000, 2, extra={"is_salary_window": pl.lit(True), "day_of_week": pl.lit(3)})
    short = detect(ref, feats, score, days=6.0)
    assert (
        by(short)["day_of_week"].status == SKIPPED
        and by(short)["is_salary_window"].status == SKIPPED
    )
    assert (
        "7+ days" in by(short)["day_of_week"].detail
        and "31+ days" in by(short)["is_salary_window"].detail
    )
    assert not short.drift_detected
    week = detect(ref, feats, score, days=7.0)
    assert by(week)["day_of_week"].status == FAIL and by(week)["is_salary_window"].status == SKIPPED
    month = detect(ref, feats, score, days=31.0)
    assert by(month)["is_salary_window"].status == FAIL


def test_a_calendar_feature_that_fails_in_a_full_cycle_does_declare_drift(ref):
    feats, score = make(3000, 2, extra={"day_of_week": pl.lit(3)})
    assert detect(ref, feats, score, days=7.0).drift_detected


def test_too_few_rows_scores_nothing_and_says_so(ref):
    feats, score = make(cfg.MIN_CURRENT_ROWS - 1, 2, shift=3.0)
    d = detect(ref, feats, score, days=40)
    assert not d.scored and not d.drift_detected and all(r.status == SKIPPED for r in d.features)
    assert verdict(d) == "nothing could be scored (too few rows)"
    exactly = detect(ref, *make(cfg.MIN_CURRENT_ROWS, 2, shift=3.0), days=40)
    assert exactly.drift_detected  # the minimum itself is enough


def test_a_new_category_and_missing_values_are_explained_in_the_detail(ref):
    feats, score = make(
        3000,
        2,
        extra={"cat": pl.when(pl.col("stable") > 1).then(pl.lit("Z")).otherwise(pl.col("cat"))},
    )
    d = detect(ref, feats, score, days=40)
    assert "never seen in the reference" in by(d)["cat"].detail
    feats, score = make(
        3000,
        2,
        extra={"stable": pl.when(pl.col("moved") > 0).then(None).otherwise(pl.col("stable"))},
    )
    d = detect(ref, feats, score, days=40)
    assert "missing 0.0% -> " in by(d)["stable"].detail


def test_missing_columns_and_mismatched_scores_are_errors(ref):
    feats, score = make(1000, 2)
    with pytest.raises(DriftStatsError, match="lacks features"):
        detect(ref, feats.drop("cat"), score, days=40)
    with pytest.raises(DriftStatsError, match="one score"):
        detect(ref, feats, score.head(10), days=40)


def test_ranking_puts_the_largest_first_and_skipped_last(ref):
    feats, score = make(3000, 2, shift=1.5)
    d = detect(ref, feats, score, days=6.0)  # the calendar features are skipped
    order = ranked(d)
    psis = [r.psi for r in order if r.psi is not None]
    assert psis == sorted(psis, reverse=True) and order[-1].status == SKIPPED
    assert order[0].name == "moved" or order[0].policy == "accumulating"


def test_the_text_and_markdown_reports(ref):
    feats, score = make(3000, 2, shift=1.5)
    d = detect(ref, feats, score, days=40)
    out = text(d, top=3)
    assert out.count("\n") == 3 and "MAJOR" in out and "DRIFT DETECTED" in out.splitlines()[-1]
    meta = {"reference_window": "a to b", "reference_rows": 1, "current_window": "c to d",
            "series_window": "1d", "injected": "amounts x3"}  # fmt: skip
    series = [
        {"start": "2025-01-01", "rows": 700, "psi": 0.2, "feature": "moved", "score_psi": 0.1},
        {"start": "2025-01-02", "rows": 10, "psi": None},
    ]
    md = markdown(d, meta, series)
    assert "| moved |" in md and "Injected shift (demonstration only)" in md
    assert "| 2025-01-02 | 10 | - | too few rows | - |" in md and "needs labels" in md
