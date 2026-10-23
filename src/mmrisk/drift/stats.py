"""Population stability index (PSI) for numeric, discrete and categorical features.

A reference sample is summarised once into bins and their shares; a later (current) sample is
counted into the SAME bins, so only the reference needs to be kept (a few numbers per feature,
no rows). Missing values get a bin of their own: in this project a missing value means "no
history yet", which is information, and a change in its share is drift.
"""

from __future__ import annotations

import math
from collections import Counter

import numpy as np
import polars as pl

N_BINS = 10  # quantile bins for numeric features
MAX_DISCRETE = 12  # at most this many distinct values: each value is its own bin
EPS = 1e-4  # shares are floored here so an empty bin gives a finite, large PSI
OTHER = "__other__"  # values the reference never saw
MISSING = "__missing__"


class DriftStatsError(ValueError):
    """The data cannot be summarised (empty, or not a supported type)."""


def _label(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v)


def summarise(values: pl.Series) -> dict:
    """Bins and reference shares for one feature."""
    if values.len() == 0:
        raise DriftStatsError("cannot summarise an empty column")
    n = values.len()
    present = values.drop_nulls()
    if present.len() == 0:
        return {"kind": "discrete", "labels": [], "shares": [], "missing_share": 1.0, "n": n}
    missing_share = (n - present.len()) / n
    if present.dtype in (pl.Utf8, pl.Boolean, pl.Categorical) or present.n_unique() <= MAX_DISCRETE:
        counts = Counter(_label(v) for v in present.to_list())
        ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))  # most common first
        labels = [lab for lab, _ in ranked]
        totals = [c for _, c in ranked]
        return {
            "kind": "discrete",
            "labels": labels,
            "shares": [c / n for c in totals],
            "missing_share": missing_share,
            "n": n,
        }
    arr = present.cast(pl.Float64).to_numpy()
    # cut points are observed values (not interpolated), so ties cannot create an empty bin
    cuts = np.unique(np.quantile(arr, np.linspace(0, 1, N_BINS + 1)[1:-1], method="lower"))
    idx = np.searchsorted(cuts, arr, side="left")
    shares = np.bincount(idx, minlength=len(cuts) + 1) / n
    return {
        "kind": "numeric",
        "edges": [float(c) for c in cuts],
        "shares": [float(s) for s in shares],
        "missing_share": missing_share,
        "n": n,
    }


def current_shares(values: pl.Series, ref: dict) -> tuple[list[float], float, float]:
    """(shares per reference bin plus the 'other' bin, missing share, unseen share) for a sample."""
    n = values.len()
    if n == 0:
        raise DriftStatsError("cannot compare an empty column")
    present = values.drop_nulls()
    missing = (n - present.len()) / n
    if ref["kind"] == "numeric":
        arr = present.cast(pl.Float64).to_numpy()
        idx = np.searchsorted(np.asarray(ref["edges"]), arr, side="left")
        counts = np.bincount(idx, minlength=len(ref["edges"]) + 1)
        return [float(c) / n for c in counts] + [0.0], missing, 0.0
    labelled = [_label(v) for v in present.to_list()]
    known = {lab: i for i, lab in enumerate(ref["labels"])}
    counts = [0] * (len(known) + 1)
    for lab in labelled:
        counts[known.get(lab, len(known))] += 1
    shares = [c / n for c in counts]
    return shares, missing, shares[-1]


def psi(ref_shares: list[float], cur_shares: list[float]) -> float:
    """sum((cur - ref) * ln(cur / ref)) over bins, with both shares floored at EPS."""
    if len(ref_shares) != len(cur_shares):
        raise DriftStatsError("the two samples must use the same bins")
    total = 0.0
    for r, c in zip(ref_shares, cur_shares, strict=True):
        r, c = max(r, EPS), max(c, EPS)
        total += (c - r) * math.log(c / r)
    return total


def compare(values: pl.Series, ref: dict) -> dict:
    """PSI of a current sample against a reference summary, with its context."""
    cur, missing, unseen = current_shares(values, ref)
    ref_shares = [*ref["shares"], 0.0]  # the bin for values the reference never saw
    ref_shares.append(ref["missing_share"])
    cur.append(missing)
    bins = sum(1 for r in ref_shares if r > EPS)
    return {
        "psi": psi(ref_shares, cur),
        "noise": noise_floor(bins, ref["n"], values.len()),
        "n_current": values.len(),
        "missing_share_reference": ref["missing_share"],
        "missing_share_current": missing,
        "unseen_share": unseen,
    }


def noise_floor(bins: int, n_ref: int, n_cur: int) -> float:
    """The PSI two samples of one distribution are expected to show: (bins - 1)(1/n + 1/m)."""
    if n_ref <= 0 or n_cur <= 0:
        return float("inf")
    return max(bins - 1, 0) * (1.0 / n_ref + 1.0 / n_cur)
