"""Health checks on a prediction store: is it consistent, complete, and like the baseline?

This is monitoring of what the system DID (its decisions), which needs no labels. Whether the
inputs changed (drift) is Phase 15; whether the model is still right needs labels (later phases).
Checks give PASS, WARN, FAIL or SKIPPED, the same words as the dataset checks.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import UTC, datetime

import polars as pl

from mmrisk.monitoring import config as cfg

PASS, WARN, FAIL, SKIPPED = "PASS", "WARN", "FAIL", "SKIPPED"
ACTIONS = ("ALLOW", "REVIEW", "BLOCK")
_REQUIRED = (
    "transaction_id",
    "event_time",
    "risk_probability",
    "action",
    "risk_level",
    "reasons",
    "feature_version",
    "processed_at",
)


class MonitoringError(ValueError):
    """The input cannot be checked (a user mistake, not a finding)."""


@dataclass(frozen=True)
class Check:
    name: str
    status: str
    detail: str


def window_hours(window: str) -> float:
    """Length of a polars duration such as 1h, 6h or 1d, in hours."""
    units = {"m": 1 / 60, "h": 1.0, "d": 24.0}
    if len(window) < 2 or window[-1] not in units or not window[:-1].isdigit():
        raise MonitoringError(f"window must look like 30m, 1h or 1d, not {window!r}")
    hours = int(window[:-1]) * units[window[-1]]
    if hours <= 0:
        raise MonitoringError("window must be longer than zero")
    return hours


def _require_columns(df: pl.DataFrame) -> None:
    missing = [c for c in _REQUIRED if c not in df.columns]
    if missing:
        raise MonitoringError(f"the predictions lack columns: {', '.join(missing)}")


def windows(df: pl.DataFrame, window: str) -> pl.DataFrame:
    """One row per window between the first and last event, empty windows included.

    Columns: start, rows, allow, review, block, flagged_share, mean_probability.
    """
    window_hours(window)  # validates
    if df.is_empty():
        return pl.DataFrame(
            schema={
                "start": pl.Datetime("us"),
                "rows": pl.Int64,
                "allow": pl.Int64,
                "review": pl.Int64,
                "block": pl.Int64,
                "flagged_share": pl.Float64,
                "mean_probability": pl.Float64,
            }
        )
    grouped = (
        df.sort("event_time")
        .group_by_dynamic("event_time", every=window, closed="left", label="left")
        .agg(
            pl.len().alias("rows"),
            (pl.col("action") == "ALLOW").sum().alias("allow"),
            (pl.col("action") == "REVIEW").sum().alias("review"),
            (pl.col("action") == "BLOCK").sum().alias("block"),
            pl.col("risk_probability").mean().alias("mean_probability"),
        )
        .rename({"event_time": "start"})
    )
    full = pl.DataFrame(
        {
            "start": pl.datetime_range(
                grouped["start"].min(), grouped["start"].max(), window, eager=True
            )
        }
    ).cast({"start": grouped.schema["start"]})
    out = full.join(grouped, on="start", how="left").with_columns(
        pl.col("rows", "allow", "review", "block").fill_null(0)
    )
    return out.with_columns(
        pl.when(pl.col("rows") > 0)
        .then((pl.col("review") + pl.col("block")) / pl.col("rows"))
        .otherwise(None)
        .alias("flagged_share")
    ).select("start", "rows", "allow", "review", "block", "flagged_share", "mean_probability")


def profile(df: pl.DataFrame, window: str = cfg.DEFAULT_WINDOW) -> dict:
    """The numbers a later report is compared with (written by `baseline`)."""
    _require_columns(df)
    if df.is_empty():
        raise MonitoringError("there are no predictions to profile")
    n = df.height
    share = {a: float((df["action"] == a).sum()) / n for a in ACTIONS}
    span_hours = max((df["event_time"].max() - df["event_time"].min()).total_seconds() / 3600, 1e-9)
    p = df["risk_probability"]
    return {
        "rows": n,
        "window": window,
        "action_share": share,
        "flagged_share": share["REVIEW"] + share["BLOCK"],
        "rows_per_hour": n / max(span_hours, 1.0),
        "probability": {
            "mean": float(p.mean()),
            "p50": float(p.quantile(0.5)),
            "p90": float(p.quantile(0.9)),
            "p99": float(p.quantile(0.99)),
        },
        "feature_versions": sorted(df["feature_version"].unique().to_list()),
        "first_event": df["event_time"].min().isoformat(),
        "last_event": df["event_time"].max().isoformat(),
        "created_at": datetime.now(UTC).replace(tzinfo=None).isoformat(timespec="seconds"),
        "note": "Statistics of a store believed to be healthy. Synthetic data only.",
    }


def _z(observed: float, n: int, expected: float) -> float:
    """z score of an observed share against an expected one (0 if the expected share is 0 or 1)."""
    if not 0.0 < expected < 1.0 or n <= 0:
        return 0.0
    return (observed - expected) / math.sqrt(expected * (1 - expected) / n)


def _level(z: float) -> str:
    z = abs(z)
    return FAIL if z >= cfg.Z_FAIL else WARN if z >= cfg.Z_WARN else PASS


def _integrity(df: pl.DataFrame) -> list[Check]:
    out: list[Check] = []
    dup = df.height - df["transaction_id"].n_unique()
    out.append(
        Check(
            "unique transaction ids",
            FAIL if dup else PASS,
            f"{dup} repeated ids" if dup else f"{df.height} rows, all different",
        )
    )
    bad_p = df.filter(
        pl.col("risk_probability").is_null()
        | pl.col("risk_probability").is_nan()
        | (pl.col("risk_probability") < 0)
        | (pl.col("risk_probability") > 1)
    ).height
    out.append(
        Check(
            "probabilities are between 0 and 1",
            FAIL if bad_p else PASS,
            f"{bad_p} rows outside [0, 1] or missing" if bad_p else "all valid",
        )
    )
    odd_action = df.filter(~pl.col("action").is_in(ACTIONS)).height
    mismatched = df.filter(
        pl.col("action").is_in(ACTIONS)
        & (
            pl.col("risk_level").replace_strict(cfg.ACTION_FOR_LEVEL, default=None)
            != pl.col("action")
        )
    ).height
    bad = odd_action + mismatched
    out.append(
        Check(
            "action and risk level agree",
            FAIL if bad else PASS,
            f"{odd_action} unknown actions, {mismatched} level mismatches"
            if bad
            else "ALLOW=LOW, REVIEW=MEDIUM, BLOCK=HIGH everywhere",
        )
    )
    nulls = df.select(pl.col("event_time", "processed_at", "feature_version").is_null().sum())
    n_null = int(sum(nulls.row(0)))
    out.append(
        Check(
            "no missing times or versions",
            FAIL if n_null else PASS,
            f"{n_null} missing values" if n_null else "none missing",
        )
    )
    allow_reasons = df.filter((pl.col("action") == "ALLOW") & (pl.col("reasons") != "[]")).height
    out.append(
        Check(
            "allowed transactions carry no reasons",
            FAIL if allow_reasons else PASS,
            f"{allow_reasons} allowed rows have reasons" if allow_reasons else "ok",
        )
    )
    flagged = df.filter(pl.col("action") != "ALLOW")
    unreadable = 0
    empty = 0
    for text in flagged["reasons"].to_list():
        try:
            value = json.loads(text)
        except (TypeError, ValueError):
            unreadable += 1
            continue
        empty += 0 if value else 1
    out.append(
        Check(
            "reasons are readable JSON",
            FAIL if unreadable else PASS,
            f"{unreadable} unreadable" if unreadable else "ok",
        )
    )
    out.append(
        Check(
            "flagged transactions have reasons",
            WARN if empty else PASS,
            f"{empty} of {flagged.height} flagged rows have none" if empty else "ok",
        )
    )
    versions = df["feature_version"].drop_nulls().unique().to_list()
    out.append(
        Check(
            "one feature version",
            WARN if len(versions) > 1 else PASS,
            f"versions: {', '.join(sorted(versions))}",
        )
    )
    return out


def _operational(
    df: pl.DataFrame,
    table: pl.DataFrame,
    window: str,
    as_of: datetime | None,
    review_capacity: float | None,
) -> list[Check]:
    out: list[Check] = []
    empty = table.filter(pl.col("rows") == 0).height
    out.append(
        Check(
            "no empty windows",
            WARN if empty else PASS,
            f"{empty} of {table.height} windows of {window} have no events"
            if empty
            else f"all {table.height} windows of {window} have events",
        )
    )
    if as_of is None:
        out.append(Check("store is fresh", SKIPPED, "needs --as-of (the stream replays old data)"))
    else:
        newest = df["processed_at"].max()
        age = (as_of - newest).total_seconds() / 3600
        stale = age > cfg.STALE_AFTER_HOURS
        out.append(
            Check(
                "store is fresh",
                WARN if stale else PASS,
                f"newest prediction is {age:.1f} h old (limit {cfg.STALE_AFTER_HOURS} h, assumed)",
            )
        )
    if review_capacity is None:
        out.append(
            Check("review queue within capacity", SKIPPED, "needs --review-capacity-per-hour")
        )
    else:
        hours = window_hours(window)
        peak = float(table["review"].max()) / hours + 0.0 if table.height else 0.0
        peak_block = float(table["block"].max()) / hours if table.height else 0.0
        over = peak > review_capacity
        out.append(
            Check(
                "review queue within capacity",
                WARN if over else PASS,
                f"peak {peak:.1f} REVIEW per hour (capacity {review_capacity:g}, given); "
                f"peak {peak_block:.1f} BLOCK per hour",
            )
        )
    return out


def _window_check(table: pl.DataFrame, expected: float, window: str) -> Check:
    """The window whose flagged share differs most from the baseline's, among testable windows."""
    name = "no unusual window"
    worst, worst_start, tested = 0.0, None, 0
    for start, rows, share in table.select("start", "rows", "flagged_share").iter_rows():
        if rows < cfg.MIN_ROWS_FOR_SHARE_TEST or rows * expected < cfg.MIN_EXPECTED_FLAGGED:
            continue
        tested += 1
        z = _z(share, rows, expected)
        if abs(z) > abs(worst):
            worst, worst_start = z, start
    if tested == 0:
        return Check(
            name,
            SKIPPED,
            f"no window of {window} has {cfg.MIN_ROWS_FOR_SHARE_TEST}+ events and "
            f"{cfg.MIN_EXPECTED_FLAGGED:g}+ expected flags",
        )
    return Check(
        name,
        WARN if abs(worst) >= cfg.WINDOW_Z_WARN else PASS,
        f"{tested} windows tested; largest |z| = {abs(worst):.1f}"
        + (f" at {worst_start}" if worst else ""),
    )


def _against_baseline(
    df: pl.DataFrame, table: pl.DataFrame, base: dict | None, window: str
) -> list[Check]:
    names = [
        "flagged share like the baseline",
        "blocked share like the baseline",
        "volume like the baseline",
        "no unusual window",
    ]
    if base is None:
        return [Check(n, SKIPPED, "needs --baseline") for n in names]
    out: list[Check] = []
    n = df.height
    for label, observed, expected in (
        (
            "flagged share like the baseline",
            float((df["action"] != "ALLOW").sum()) / n,
            base["flagged_share"],
        ),
        (
            "blocked share like the baseline",
            float((df["action"] == "BLOCK").sum()) / n,
            base["action_share"]["BLOCK"],
        ),
    ):
        if n * expected < cfg.MIN_EXPECTED_FLAGGED:
            out.append(Check(label, SKIPPED, f"only {n * expected:.1f} expected, too few to test"))
            continue
        z = _z(observed, n, expected)
        out.append(
            Check(
                label, _level(z), f"{observed:.2%} against {expected:.2%} (z = {z:+.1f}, n = {n})"
            )
        )
    span_hours = max((df["event_time"].max() - df["event_time"].min()).total_seconds() / 3600, 1.0)
    rate = n / span_hours
    ratio = rate / base["rows_per_hour"] if base["rows_per_hour"] > 0 else float("inf")
    off = ratio > cfg.VOLUME_FACTOR or ratio < 1 / cfg.VOLUME_FACTOR
    out.append(
        Check(
            "volume like the baseline",
            WARN if off else PASS,
            f"{rate:.1f} per hour against {base['rows_per_hour']:.1f} (x{ratio:.2f}; "
            f"limit x{cfg.VOLUME_FACTOR:g}, assumed)",
        )
    )
    out.append(_window_check(table, base["flagged_share"], window))
    return out


def run_checks(
    df: pl.DataFrame,
    baseline: dict | None = None,
    window: str = cfg.DEFAULT_WINDOW,
    as_of: datetime | None = None,
    review_capacity: float | None = None,
) -> tuple[list[Check], pl.DataFrame]:
    """(checks, windows table). An empty store is a FAIL, not an error."""
    window_hours(window)
    if review_capacity is not None and review_capacity <= 0:
        raise MonitoringError("review capacity must be above zero")
    _require_columns(df)
    if df.is_empty():
        return [Check("predictions exist", FAIL, "the store has no predictions")], windows(
            df, window
        )
    table = windows(df, window)
    checks = [Check("predictions exist", PASS, f"{df.height} predictions")]
    checks += _integrity(df)
    checks += _operational(df, table, window, as_of, review_capacity)
    checks += _against_baseline(df, table, baseline, window)
    return checks, table


def overall(checks: list[Check]) -> str:
    statuses = {c.status for c in checks}
    return FAIL if FAIL in statuses else WARN if WARN in statuses else PASS
