"""The gates a candidate must pass before it may replace the champion.

Both models are scored on the same newest matured test window, with the champion's own
calibrator and the same (assumed) cost model. Required gates block promotion; advisory gates are
shown but never block. Thresholds are in `config.Gates` and are ASSUMED.

  data_sufficiency   enough fraud in train, validation and test to say anything
  fair_comparison    the champion did not train on the test window (else it would be graded on
                     its own homework, which favours it)
  pr_auc             candidate PR-AUC is not worse than the champion's by more than the margin
                     (customer-level bootstrap of the paired difference)
  cost               realised cost per 10,000 is not worse by more than the margin (same bootstrap)
  calibration        candidate ECE is not worse than the champion's by more than the margin
  flagged_share      the share of transactions that are not ALLOW is within a ratio band of the
                     champion's, so a model that suddenly blocks everything is stopped
  roundtrip          the saved candidate loads (which also checks its feature version) and
                     reproduces the in-memory probabilities
  agreement          (advisory) how often both pick the same action
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

import numpy as np

from mmrisk.models.advanced import cost_bootstrap
from mmrisk.models.calibration import ece
from mmrisk.models.costs import CostModel
from mmrisk.models.metrics import bootstrap, interval, pr_auc
from mmrisk.retrain.config import Gates


@dataclass(frozen=True)
class Gate:
    name: str
    required: bool
    passed: bool
    value: float | None
    threshold: str
    detail: str

    def as_dict(self) -> dict:
        return asdict(self)


def blocking(gates: list[Gate]) -> list[Gate]:
    return [g for g in gates if g.required and not g.passed]


def approved(gates: list[Gate]) -> bool:
    return not blocking(gates)


def data_gates(train_fraud: int, val_fraud: int, test_fraud: int, th: Gates) -> list[Gate]:
    need = {"train": th.min_train_fraud, "validation": th.min_val_fraud, "test": th.min_test_fraud}
    have = {"train": train_fraud, "validation": val_fraud, "test": test_fraud}
    short = [f"{k} {have[k]} < {need[k]}" for k in need if have[k] < need[k]]
    return [Gate(
        "data_sufficiency", True, not short, float(min(have[k] / need[k] for k in need)),
        "fraud rows: " + ", ".join(f"{k} >= {v}" for k, v in need.items()) + " (ASSUMED)",
        "short: " + "; ".join(short) if short else "enough fraud in every part",
    )]  # fmt: skip


def fair_comparison_gate(champion_train_end, test_start) -> Gate:
    rule = "champion train_end < test start"
    if champion_train_end is None:
        detail = "the champion has no recorded split, so overlap cannot be ruled out"
        return Gate("fair_comparison", True, False, None, rule, detail)
    detail = f"champion trained to {champion_train_end}, test window starts {test_start}"
    return Gate("fair_comparison", True, champion_train_end < test_start, None, rule, detail)


def compare(
    *,
    y: np.ndarray,
    amount: np.ndarray,
    customers: np.ndarray,
    champion_p: np.ndarray,
    candidate_p: np.ndarray,
    cost_model: CostModel,
    th: Gates,
    seed: int = 7,
) -> tuple[list[Gate], dict]:
    """Metric gates on one shared test window. Returns the gates and the numbers behind them."""
    stat = {"pr_auc": lambda yy, s, w: pr_auc(yy, s, w)}
    boot = bootstrap(y, {"champion": champion_p, "candidate": candidate_p}, customers,
                     stat, n=th.n_boot, seed=seed)  # fmt: skip
    d_ap = boot["candidate"]["pr_auc"] - boot["champion"]["pr_auc"]
    ap = {"champion": pr_auc(y, champion_p), "candidate": pr_auc(y, candidate_p)}
    ap_lo, ap_hi = interval(d_ap)

    a_champ, a_cand = cost_model.decide(champion_p, amount), cost_model.decide(candidate_p, amount)
    per_row = {"champion": cost_model.realized_cost(a_champ, y, amount),
               "candidate": cost_model.realized_cost(a_cand, y, amount)}  # fmt: skip
    cb = cost_bootstrap(per_row, customers, th.n_boot, seed + 1)
    d_cost = cb["candidate"] - cb["champion"]
    cost = {k: float(v.mean() * 10_000) for k, v in per_row.items()}
    cost_lo, cost_hi = interval(d_cost)
    cost_limit = th.cost_margin * cost["champion"]

    e = {"champion": ece(y, champion_p), "candidate": ece(y, candidate_p)}
    flagged = {"champion": float((a_champ != 0).mean()), "candidate": float((a_cand != 0).mean())}
    ratio = flagged["candidate"] / flagged["champion"] if flagged["champion"] > 0 else None
    agreement = float((a_champ == a_cand).mean())

    gates = [
        Gate("pr_auc", True, ap_lo >= -th.pr_auc_margin, ap["candidate"] - ap["champion"],
             f"95% lower bound of difference >= -{th.pr_auc_margin:g} (ASSUMED)",
             f"champion {ap['champion']:.4f}, candidate {ap['candidate']:.4f}, "
             f"difference interval [{ap_lo:+.4f}, {ap_hi:+.4f}]"),
        Gate("cost", True, cost_hi <= cost_limit, cost["candidate"] - cost["champion"],
             f"95% upper bound of difference <= {cost_limit:.1f} KES per 10k "
             f"({th.cost_margin:.0%} of champion, ASSUMED)",
             f"per 10k: champion {cost['champion']:.1f}, candidate {cost['candidate']:.1f}, "
             f"difference interval [{cost_lo:+.1f}, {cost_hi:+.1f}]"),
        Gate("calibration", True, e["candidate"] <= e["champion"] + th.ece_margin,
             e["candidate"] - e["champion"],
             f"candidate ECE <= champion ECE + {th.ece_margin:g} (ASSUMED)",
             f"ECE champion {e['champion']:.4f}, candidate {e['candidate']:.4f}"),
        Gate("flagged_share", True,
             ratio is not None and th.flagged_ratio_min <= ratio <= th.flagged_ratio_max, ratio,
             f"candidate/champion share of REVIEW+BLOCK in [{th.flagged_ratio_min:g}, "
             f"{th.flagged_ratio_max:g}] (ASSUMED)",
             f"champion {flagged['champion']:.2%}, candidate {flagged['candidate']:.2%}"),
        Gate("agreement", False, agreement >= th.min_agreement, agreement,
             f">= {th.min_agreement:.0%} (ASSUMED, advisory)",
             f"same action on {agreement:.1%} of transactions"),
    ]  # fmt: skip
    numbers = {"pr_auc": ap, "pr_auc_diff_interval": [ap_lo, ap_hi], "cost_per_10k": cost,
               "cost_diff_interval": [cost_lo, cost_hi], "ece": e, "flagged_share": flagged,
               "agreement": agreement, "rows": int(len(y)), "fraud": int(np.sum(y))}  # fmt: skip
    return gates, numbers


def roundtrip_gate(in_memory: np.ndarray, reloaded: np.ndarray) -> Gate:
    ok = bool(
        len(in_memory) == len(reloaded)
        and np.all(np.isfinite(reloaded))
        and np.all((reloaded >= 0) & (reloaded <= 1))
        and np.allclose(in_memory, reloaded, atol=1e-9, rtol=0)
    )
    worst = float(np.max(np.abs(in_memory - reloaded))) if len(in_memory) == len(reloaded) else None
    return Gate(
        "roundtrip",
        True,
        ok,
        worst,
        "reloaded probabilities equal in-memory, in [0, 1]",
        f"largest difference {worst}" if worst is not None else "different lengths",
    )
