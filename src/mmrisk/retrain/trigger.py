"""When is a retrain worth doing? Pure decision logic; the CLI gathers the inputs.

Triggers (any one is enough):
  drift          the latest drift report says major drift (PSI above the fail limit); a report
                 made with --scale-amounts is a demonstration and is ignored
  model_age      the champion's training data ended too long ago (simulated time, not the clock)
  new_labels     enough fraud labels exist on transactions newer than the champion's data
  forced         the person asked

Why age and labels in addition to drift: the accumulating features (n_prior_txns,
account_age_days) grow with time by construction, so the drift report deliberately does not count
them. A model that was fitted to young accounts becomes stale without any alarm from PSI; the age
and label triggers cover that.
"""

from __future__ import annotations

from dataclasses import dataclass

from mmrisk.retrain.config import Triggers


@dataclass(frozen=True)
class Trigger:
    name: str
    fired: bool | None  # None = could not be evaluated (missing input)
    detail: str


@dataclass(frozen=True)
class Decision:
    retrain: bool
    triggers: list[Trigger]


def evaluate(
    *,
    drift: dict | None,
    model_age_days: float | None,
    new_fraud_labels: int | None,
    force: bool = False,
    th: Triggers | None = None,
) -> Decision:
    th = th or Triggers()
    out: list[Trigger] = []

    if drift is None:
        out.append(Trigger("drift", None, "no drift report given or found"))
    elif drift.get("injected"):
        out.append(Trigger("drift", None, "the drift report is a demonstration "
                           f"({drift['injected']}); ignored"))  # fmt: skip
    else:
        fired = bool(drift["drift_detected"])
        out.append(Trigger("drift", fired, f"drift report {drift.get('window', '?')}: "
                           f"{drift.get('verdict', 'no verdict')}"))  # fmt: skip

    if model_age_days is None:
        out.append(Trigger("model_age", None, "the champion has no recorded data end"))
    else:
        out.append(Trigger(
            "model_age", model_age_days > th.max_model_age_days,
            f"champion data ended {model_age_days:.0f} days before the data end "
            f"(limit {th.max_model_age_days:g}, ASSUMED)",
        ))  # fmt: skip

    if new_fraud_labels is None:
        out.append(Trigger("new_labels", None, "the champion has no recorded data end"))
    else:
        out.append(Trigger(
            "new_labels", new_fraud_labels >= th.min_new_fraud_labels,
            f"{new_fraud_labels} fraud labels on transactions after the champion's data "
            f"(needed {th.min_new_fraud_labels}, ASSUMED)",
        ))  # fmt: skip

    out.append(Trigger("forced", bool(force), "--force" if force else "not asked"))
    return Decision(any(t.fired is True for t in out), out)
