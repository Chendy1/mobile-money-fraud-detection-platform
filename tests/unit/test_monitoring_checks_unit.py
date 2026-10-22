"""Checks on stored predictions: each check passes on healthy data and fails on its own fault."""

import json
from datetime import datetime, timedelta

import polars as pl
import pytest

from mmrisk.monitoring import config as cfg
from mmrisk.monitoring.checks import (
    FAIL,
    PASS,
    SKIPPED,
    WARN,
    MonitoringError,
    overall,
    profile,
    run_checks,
    window_hours,
    windows,
)
from mmrisk.monitoring.checks import _z as z_score
from mmrisk.streaming.store import SCHEMA

T0 = datetime(2025, 12, 1, 0, 0, 0)
LEVEL = {"ALLOW": "LOW", "REVIEW": "MEDIUM", "BLOCK": "HIGH"}
REASONS = json.dumps([{"rank": 1, "feature": "amount_kes", "effect": 1.0, "text": "x"}])


def frame(n=4000, step=60, flagged_every=40, block_every=4, start=T0, **over):
    """n predictions, one per `step` seconds; every `flagged_every`-th is flagged."""
    rows = []
    for i in range(n):
        flagged = flagged_every and i % flagged_every == flagged_every - 1
        action = (
            ("BLOCK" if (i // flagged_every) % block_every == 0 else "REVIEW")
            if flagged
            else "ALLOW"
        )
        rows.append(
            {
                "seq": i,
                "transaction_id": f"T{i:07d}",
                "customer_id": f"C{i % 50:06d}",
                "event_time": start + timedelta(seconds=i * step),
                "transaction_type": "SEND_MONEY",
                "amount_kes": 1000,
                "channel": "APP",
                "location_county": "Nairobi",
                "risk_probability": 0.9 if flagged else 0.01,
                "action": action,
                "risk_level": LEVEL[action],
                "reasons": REASONS if flagged else "[]",
                "feature_version": "1.0",
                "processed_at": datetime(2026, 1, 1, 0, 0, 0),
            }
        )
    df = pl.DataFrame(rows, schema=SCHEMA)
    for col, val in over.items():
        df = df.with_columns(pl.lit(val).alias(col))
    return df


def by_name(checks):
    return {c.name: c for c in checks}


def status(checks, name):
    return by_name(checks)[name].status


# ---- windows ----


def test_window_lengths():
    assert window_hours("30m") == 0.5 and window_hours("1h") == 1 and window_hours("2d") == 48


@pytest.mark.parametrize("bad", ["", "h", "1", "1w", "0h", "-1h", "1.5h", "abc"])
def test_a_bad_window_is_refused(bad):
    with pytest.raises(MonitoringError):
        window_hours(bad)


def test_windows_count_rows_and_actions_and_include_empty_ones():
    df = frame(n=6, step=3600 * 10, flagged_every=2, block_every=1)  # events at 0, 10, 20, ... h
    t = windows(df, "1d")
    assert t["rows"].to_list() == [3, 2, 1]
    t = windows(df, "5h")
    assert t["rows"].sum() == 6 and 0 in t["rows"].to_list()  # gaps are rows with zero events
    assert t["start"].is_sorted() and t["start"].n_unique() == t.height
    empty = t.filter(pl.col("rows") == 0)
    assert empty["flagged_share"].null_count() == empty.height  # no share without events


def test_a_window_is_closed_on_the_left():
    df = frame(n=3, step=3600)  # events at 00:00, 01:00, 02:00
    t = windows(df, "1h")
    assert t["rows"].to_list() == [1, 1, 1]
    t = windows(frame(n=2, step=3600), "2h")
    assert t["rows"].to_list() == [2]


def test_flagged_share_of_a_window():
    df = frame(n=100, step=60, flagged_every=10)
    t = windows(df, "1d")
    assert t["flagged_share"][0] == pytest.approx(0.10)
    assert (t["review"][0] + t["block"][0], t["allow"][0]) == (10, 90)


def test_an_empty_store_has_an_empty_table():
    assert windows(frame(n=0), "1h").is_empty()


# ---- profile ----


def test_profile_numbers():
    base = profile(frame(n=2400, step=60, flagged_every=40, block_every=4))
    assert base["rows"] == 2400
    assert base["flagged_share"] == pytest.approx(1 / 40)
    assert sum(base["action_share"].values()) == pytest.approx(1.0)
    assert base["action_share"]["BLOCK"] == pytest.approx(1 / 160)
    assert base["rows_per_hour"] == pytest.approx(60.0, rel=0.01)
    assert base["feature_versions"] == ["1.0"] and base["probability"]["p50"] == pytest.approx(0.01)
    json.dumps(base)  # it must be writable as JSON


def test_profile_refuses_an_empty_store_and_missing_columns():
    with pytest.raises(MonitoringError):
        profile(frame(n=0))
    with pytest.raises(MonitoringError, match="lack columns"):
        profile(frame(n=5).drop("action"))


# ---- a healthy store ----


def test_a_healthy_store_has_no_warnings_or_failures():
    checks, _ = run_checks(frame(), window="1d")
    assert overall(checks) == PASS
    assert {c.status for c in checks} <= {PASS, SKIPPED}
    # without a baseline, an as-of time or a capacity those checks say what they need
    assert status(checks, "store is fresh") == SKIPPED
    assert status(checks, "flagged share like the baseline") == SKIPPED
    assert status(checks, "review queue within capacity") == SKIPPED


# ---- integrity: each fault fails its own check ----


def test_repeated_ids_fail():
    df = frame(n=100).with_columns(pl.lit("T0000000").alias("transaction_id"))
    assert status(run_checks(df)[0], "unique transaction ids") == FAIL


@pytest.mark.parametrize("bad", [1.5, -0.1, None, float("nan")])
def test_probabilities_outside_zero_one_or_missing_fail(bad):
    df = frame(n=100)
    df = df.with_columns(
        pl.when(pl.col("seq") == 3)
        .then(pl.lit(bad, dtype=pl.Float64))
        .otherwise(pl.col("risk_probability"))
        .alias("risk_probability")
    )
    assert status(run_checks(df)[0], "probabilities are between 0 and 1") == FAIL


def test_probabilities_of_exactly_zero_and_one_are_valid():
    df = frame(n=100).with_columns(
        pl.when(pl.col("seq") == 1).then(0.0).when(pl.col("seq") == 2).then(1.0)
        .otherwise(pl.col("risk_probability")).alias("risk_probability")
    )  # fmt: skip
    assert status(run_checks(df)[0], "probabilities are between 0 and 1") == PASS


def test_an_unknown_action_or_a_level_that_does_not_match_fails():
    df = frame(n=100, flagged_every=10)
    unknown = df.with_columns(
        pl.when(pl.col("seq") == 1).then(pl.lit("PASS")).otherwise(pl.col("action")).alias("action")
    )
    assert status(run_checks(unknown)[0], "action and risk level agree") == FAIL
    wrong = df.with_columns(
        pl.when(pl.col("seq") == 9)
        .then(pl.lit("LOW"))
        .otherwise(pl.col("risk_level"))
        .alias("risk_level")
    )
    assert wrong.filter(pl.col("seq") == 9)["action"][0] != "ALLOW"
    assert status(run_checks(wrong)[0], "action and risk level agree") == FAIL


def test_a_missing_time_or_version_fails():
    df = frame(n=100).with_columns(
        pl.when(pl.col("seq") == 5)
        .then(None)
        .otherwise(pl.col("feature_version"))
        .alias("feature_version")
    )
    assert status(run_checks(df)[0], "no missing times or versions") == FAIL


def test_allowed_transactions_must_not_carry_reasons():
    df = frame(n=100).with_columns(
        pl.when(pl.col("seq") == 1)
        .then(pl.lit(REASONS))
        .otherwise(pl.col("reasons"))
        .alias("reasons")
    )
    assert status(run_checks(df)[0], "allowed transactions carry no reasons") == FAIL


def test_unreadable_reasons_fail_and_missing_reasons_only_warn():
    df = frame(n=100, flagged_every=10)
    flagged = df.filter(pl.col("action") != "ALLOW")["seq"][0]
    garbled = df.with_columns(
        pl.when(pl.col("seq") == flagged)
        .then(pl.lit("{not json"))
        .otherwise(pl.col("reasons"))
        .alias("reasons")
    )
    checks = run_checks(garbled)[0]
    assert status(checks, "reasons are readable JSON") == FAIL
    empty = df.with_columns(
        pl.when(pl.col("seq") == flagged)
        .then(pl.lit("[]"))
        .otherwise(pl.col("reasons"))
        .alias("reasons")
    )
    checks = run_checks(empty)[0]
    assert status(checks, "flagged transactions have reasons") == WARN
    assert status(checks, "reasons are readable JSON") == PASS
    assert overall(checks) == WARN


def test_two_feature_versions_warn():
    df = frame(n=100).with_columns(
        pl.when(pl.col("seq") < 50)
        .then(pl.lit("2.0"))
        .otherwise(pl.col("feature_version"))
        .alias("feature_version")
    )
    c = by_name(run_checks(df)[0])["one feature version"]
    assert c.status == WARN and "1.0" in c.detail and "2.0" in c.detail


# ---- completeness, freshness, capacity ----


def test_an_empty_store_fails_and_missing_columns_are_an_error():
    checks, table = run_checks(frame(n=0))
    assert [c.status for c in checks] == [FAIL] and table.is_empty()
    with pytest.raises(MonitoringError):
        run_checks(frame(n=10).drop("processed_at"))


def test_a_gap_in_the_stream_warns():
    df = frame(n=4000, step=60)  # about 2.8 days
    cut = df.filter(
        (pl.col("event_time") < T0 + timedelta(days=1))
        | (pl.col("event_time") >= T0 + timedelta(days=2))
    )
    assert status(run_checks(cut, window="1d")[0], "no empty windows") == WARN
    assert status(run_checks(df, window="1d")[0], "no empty windows") == PASS


def test_freshness_needs_an_as_of_time_and_compares_with_the_limit():
    df = frame(n=100)  # processed_at is 2026-01-01 00:00
    newest = datetime(2026, 1, 1)
    limit = timedelta(hours=cfg.STALE_AFTER_HOURS)
    assert status(run_checks(df, as_of=newest + limit)[0], "store is fresh") == PASS  # equal: fine
    assert (
        status(run_checks(df, as_of=newest + limit + timedelta(seconds=1))[0], "store is fresh")
        == WARN
    )
    assert status(run_checks(df, as_of=newest)[0], "store is fresh") == PASS


def test_the_review_queue_is_compared_with_the_given_capacity_per_hour():
    df = frame(n=1440, step=60, flagged_every=10, block_every=1000)  # one day
    reviews = df.filter(pl.col("action") == "REVIEW").height
    per_hour = reviews / 24
    ok = run_checks(df, window="1d", review_capacity=per_hour)[0]
    assert status(ok, "review queue within capacity") == PASS  # equal is within capacity
    over = run_checks(df, window="1d", review_capacity=per_hour - 0.01)[0]
    assert status(over, "review queue within capacity") == WARN
    assert f"{per_hour:.1f} REVIEW per hour" in by_name(over)["review queue within capacity"].detail
    with pytest.raises(MonitoringError):
        run_checks(df, review_capacity=0)


# ---- against a baseline ----


def base_for(df):
    return profile(df)


def test_the_same_data_matches_its_own_baseline():
    df = frame(n=6000)
    checks, _ = run_checks(df, baseline=base_for(df), window="1d")
    assert status(checks, "flagged share like the baseline") == PASS
    assert status(checks, "blocked share like the baseline") == PASS
    assert status(checks, "volume like the baseline") == PASS


def test_a_much_higher_flagged_share_fails_and_a_slightly_higher_one_warns():
    healthy = frame(n=6000, flagged_every=40)  # 2.5%
    base = base_for(healthy)
    triple = frame(n=6000, flagged_every=13)  # 7.7%
    assert status(run_checks(triple, base)[0], "flagged share like the baseline") == FAIL
    n = 6000
    p0 = base["flagged_share"]
    se = (p0 * (1 - p0) / n) ** 0.5
    warn_share = p0 + 3.5 * se  # between the warning and the failure limits
    flagged_every = round(1 / warn_share)
    mild = frame(n=n, flagged_every=flagged_every)
    z = z_score(1 / flagged_every, n, p0)
    assert cfg.Z_WARN <= z < cfg.Z_FAIL
    assert status(run_checks(mild, base)[0], "flagged share like the baseline") == WARN


def test_a_collapse_in_flags_is_found_as_well():
    base = base_for(frame(n=6000, flagged_every=40))
    none = frame(n=6000, flagged_every=0)
    assert status(run_checks(none, base)[0], "flagged share like the baseline") == FAIL


def test_the_share_check_is_skipped_when_too_few_flags_are_expected():
    tiny_base = base_for(frame(n=6000, flagged_every=2000))  # 0.05%
    small = frame(n=500, flagged_every=2000)
    c = by_name(run_checks(small, tiny_base)[0])["flagged share like the baseline"]
    assert c.status == SKIPPED and "too few" in c.detail


def test_volume_outside_the_factor_warns_and_exactly_the_factor_does_not():
    base = base_for(frame(n=6000, step=60))
    fast = frame(n=6000, step=int(60 / 2.5))  # 2.5 times faster
    slow = frame(n=6000, step=60 * 3)
    assert status(run_checks(fast, base)[0], "volume like the baseline") == WARN
    assert status(run_checks(slow, base)[0], "volume like the baseline") == WARN
    base2 = dict(base, rows_per_hour=base["rows_per_hour"] * cfg.VOLUME_FACTOR)
    c = by_name(run_checks(frame(n=6000, step=60), base2)[0])["volume like the baseline"]
    assert c.status == PASS and "x0.50" in c.detail


def test_one_odd_window_warns_but_never_fails():
    base = base_for(frame(n=6000, flagged_every=40))
    df = frame(n=6000, flagged_every=40)
    # in the second day, flag about one in three: far outside the baseline
    day2 = (pl.col("event_time") >= T0 + timedelta(days=1)) & (
        pl.col("event_time") < T0 + timedelta(days=2)
    )
    odd = df.with_columns(
        pl.when(day2 & (pl.col("seq") % 3 == 0))
        .then(pl.lit("REVIEW"))
        .otherwise(pl.col("action"))
        .alias("action")
    ).with_columns(
        pl.when(pl.col("action") == "ALLOW")
        .then(pl.lit("LOW"))
        .when(pl.col("action") == "REVIEW")
        .then(pl.lit("MEDIUM"))
        .otherwise(pl.lit("HIGH"))
        .alias("risk_level"),
        pl.when(pl.col("action") == "ALLOW")
        .then(pl.lit("[]"))
        .otherwise(pl.lit(REASONS))
        .alias("reasons"),
    )
    c = by_name(run_checks(odd, base, window="1d")[0])["no unusual window"]
    assert c.status == WARN and "2025-12-02" in c.detail
    assert c.status != FAIL


def test_the_window_check_is_skipped_for_small_windows():
    df = frame(n=6000)
    c = by_name(run_checks(df, base_for(df), window="1h")[0])["no unusual window"]
    assert c.status == SKIPPED  # 60 events per hour is below the minimum for a share test


def test_the_z_score():
    assert z_score(0.5, 100, 0.5) == 0.0
    assert z_score(0.6, 100, 0.5) == pytest.approx(2.0)
    assert (
        z_score(0.9, 100, 0.0) == 0.0
        and z_score(0.9, 100, 1.0) == 0.0
        and z_score(0.9, 0, 0.5) == 0.0
    )


def test_overall_takes_the_worst_status():
    from mmrisk.monitoring.checks import Check

    assert overall([Check("a", PASS, ""), Check("b", SKIPPED, "")]) == PASS
    assert overall([Check("a", PASS, ""), Check("b", WARN, "")]) == WARN
    assert overall([Check("a", WARN, ""), Check("b", FAIL, ""), Check("c", PASS, "")]) == FAIL


def test_the_direction_of_a_share_difference_is_shown():
    base = base_for(frame(n=6000, flagged_every=40))
    collapse = by_name(run_checks(frame(n=6000, flagged_every=0), base)[0])
    surge = by_name(run_checks(frame(n=6000, flagged_every=13), base)[0])
    assert "z = -" in collapse["flagged share like the baseline"].detail
    assert "z = +" in surge["flagged share like the baseline"].detail


def test_a_window_needs_enough_events_even_when_many_flags_are_expected():
    # 20% flagged: a 60-event window expects 12 flags, but a share from 60 events is still too
    # noisy to test against the baseline
    df = frame(n=6000, step=60, flagged_every=5)
    c = by_name(run_checks(df, base_for(df), window="1h")[0])["no unusual window"]
    assert c.status == SKIPPED
    c = by_name(run_checks(df, base_for(df), window="3h")[0])["no unusual window"]
    assert c.status == PASS and "windows tested" in c.detail  # 180 events per window: tested
