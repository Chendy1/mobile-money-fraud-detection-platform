"""Compare a current window with a reference: PSI per feature and for the model's score."""

from __future__ import annotations

from dataclasses import dataclass

import polars as pl

from mmrisk.drift import config as cfg
from mmrisk.drift import stats

PASS, WARN, FAIL, SKIPPED = "PASS", "WARN", "FAIL", "SKIPPED"


@dataclass(frozen=True)
class Result:
    name: str
    status: str
    psi: float | None
    noise: float | None
    n: int
    policy: str  # monitored | calendar | accumulating | score
    detail: str


@dataclass(frozen=True)
class Drift:
    features: list[Result]
    score: Result
    drift_detected: bool
    rows: int
    days: float

    @property
    def scored(self) -> list[Result]:
        return [r for r in [*self.features, self.score] if r.psi is not None]

    @property
    def counted(self) -> list[Result]:
        """What decides `drift_detected`: the score and every feature except accumulating ones.

        A calendar feature counts when it was scored, that is, when the window spans its cycle.
        """
        return [self.score, *(r for r in self.features if r.policy != "accumulating")]


def policy_of(name: str) -> str:
    if name in cfg.CALENDAR_MIN_DAYS:
        return "calendar"
    if name in cfg.ACCUMULATING:
        return "accumulating"
    return "monitored"


def level(psi: float) -> str:
    return FAIL if psi >= cfg.PSI_FAIL else WARN if psi >= cfg.PSI_WARN else PASS


def _one(name: str, values: pl.Series, ref: dict, policy: str, days: float) -> Result:
    n = values.len()
    if n < cfg.MIN_CURRENT_ROWS:
        note = f"only {n} rows, at least {cfg.MIN_CURRENT_ROWS} are needed"
        return Result(name, SKIPPED, None, None, n, policy, note)
    cycle = cfg.CALENDAR_MIN_DAYS.get(name, 0.0)
    if policy == "calendar" and days < cycle:
        note = f"a calendar effect: needs a window of {cycle:g}+ days, not {days:.1f}"
        return Result(name, SKIPPED, None, None, n, policy, note)
    c = stats.compare(values, ref)
    parts = []
    if c["unseen_share"] > 0:
        parts.append(f"{c['unseen_share']:.2%} of values were never seen in the reference")
    if abs(c["missing_share_current"] - c["missing_share_reference"]) >= 0.02:
        parts.append(
            f"missing {c['missing_share_reference']:.1%} -> {c['missing_share_current']:.1%}"
        )
    if policy == "accumulating":
        parts.append("grows with time by construction; not counted")
    return Result(name, level(c["psi"]), c["psi"], c["noise"], n, policy, "; ".join(parts))


def detect(ref: dict, features: pl.DataFrame, scores: pl.Series, days: float) -> Drift:
    if scores.len() != features.height:
        raise stats.DriftStatsError("one score per row is needed")
    missing = [n for n in ref["features"] if n not in features.columns]
    if missing:
        raise stats.DriftStatsError(f"the current data lacks features: {', '.join(missing)}")
    results = [
        _one(name, features[name], summary, policy_of(name), days)
        for name, summary in ref["features"].items()
    ]
    score = _one("risk_probability", scores, ref["score"], "score", days)
    out = Drift(results, score, False, features.height, days)
    return Drift(results, score, any(r.status == FAIL for r in out.counted), features.height, days)


def ranked(d: Drift) -> list[Result]:
    """Scored results, largest PSI first; skipped ones last."""
    return sorted([*d.features, d.score], key=lambda r: (r.psi is None, -(r.psi or 0.0)))
