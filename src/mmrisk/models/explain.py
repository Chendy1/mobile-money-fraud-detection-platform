"""Explanations for the LightGBM model: global importance, reason codes and cross-checks.

Everything here describes what the *model* does with its inputs. None of it says why a
transaction is fraudulent, and a feature that carries a lot of weight is not necessarily a cause.

Method. LightGBM computes exact TreeSHAP contributions (`pred_contrib`): for every row, a base
value plus one contribution per matrix column adds up to the model's log-odds. Matrix columns are
summed back to the 28 catalogue features (one-hot columns and the hour's sine and cosine belong
to one feature each), which keeps the additivity exact. Platt calibration with the logit link is
an affine map (a x log-odds + b), so contributions multiplied by `a` add up to the calibrated
log-odds as well; that is the scale used for reason codes.
"""

from __future__ import annotations

import numpy as np
import polars as pl

from mmrisk.features.registry import FEATURES, feature_names
from mmrisk.models.metrics import pr_auc
from mmrisk.models.preprocess import column_names

GROUP_OF = {f.name: f.group for f in FEATURES}
DTYPE_OF = {f.name: f.dtype for f in FEATURES}
# amount_kes and amount_log carry the same information, so they are judged as one unit
UNITS: dict[str, list[str]] = {
    ("amount" if f.name == "amount_kes" else f.name): (
        ["amount_kes", "amount_log"] if f.name == "amount_kes" else [f.name]
    )
    for f in FEATURES
    if f.name != "amount_log"
}
LABELS = {
    "amount": "Transaction amount",
    "transaction_type": "Transaction type",
    "channel": "Channel",
    "hour": "Hour of day",
    "day_of_week": "Day of week",
    "is_night": "Night-time transaction",
    "is_salary_window": "Around salary days",
    "segment": "Customer segment",
    "account_age_days": "Account age (days)",
    "is_away_from_home": "Away from home county",
    "n_prior_txns": "Earlier transactions by the customer",
    "secs_since_prev_txn": "Seconds since previous transaction",
    "txn_count_10m": "Transactions in the last 10 minutes",
    "txn_count_1h": "Transactions in the last hour",
    "txn_count_24h": "Transactions in the last 24 hours",
    "txn_count_7d": "Transactions in the last 7 days",
    "amount_sum_1h": "Amount sent in the last hour (KES)",
    "amount_sum_24h": "Amount sent in the last 24 hours (KES)",
    "txn_count_30d": "Transactions in the last 30 days",
    "avg_amount_30d": "Average amount, last 30 days (KES)",
    "amount_to_avg_30d": "Amount compared with the 30-day average",
    "is_new_device": "First use of this device",
    "is_new_recipient": "New recipient",
    "county_changed": "County changed since previous transaction",
    "location_jump_1h": "County changed within an hour",
    "secs_since_last_inbound": "Seconds since money last arrived",
    "cashout_after_inbound": "Cash-out soon after money arrived",
}
# Signals each fraud pattern's description in the data dictionary implies. This is the author's
# reading of that table, an assumption used to check that the model leans on sensible signals.
EXPECTED_SIGNALS = {
    "SIM_SWAP": ["is_new_device", "location_jump_1h", "is_night", "txn_count_10m", "txn_count_1h"],
    "SOCIAL_ENGINEERING": ["is_new_recipient", "amount_to_avg_30d", "amount"],
    "VELOCITY_BURST": ["txn_count_10m", "txn_count_1h", "secs_since_prev_txn", "amount_sum_1h"],
    "BEHAVIORAL_SHIFT": ["transaction_type", "amount_to_avg_30d", "amount"],
    "STRUCTURING": ["amount", "txn_count_24h", "amount_sum_24h"],
    "AMOUNT_ANOMALY": ["amount_to_avg_30d", "amount"],
    "LOCATION_JUMP": ["location_jump_1h", "county_changed", "is_away_from_home"],
    "MULE_CASHOUT": ["cashout_after_inbound", "secs_since_last_inbound"],
}


class ExplainError(ValueError):
    pass


def _source(column: str) -> str:
    if "=" in column:
        return column.split("=")[0]
    if column in ("hour_sin", "hour_cos"):
        return "hour"
    return column


def aggregate(phi: np.ndarray) -> np.ndarray:
    """Sum matrix-column contributions into one column per catalogue feature (registry order)."""
    cols = column_names("tree")
    if phi.shape[1] != len(cols):
        raise ExplainError(f"expected {len(cols)} contribution columns, got {phi.shape[1]}")
    names = feature_names()
    out = np.zeros((phi.shape[0], len(names)))
    index = {n: i for i, n in enumerate(names)}
    for j, c in enumerate(cols):
        out[:, index[_source(c)]] += phi[:, j]
    return out


def by_unit(phi_feat: np.ndarray) -> dict[str, np.ndarray]:
    """Contributions per judging unit (amount_kes and amount_log together)."""
    index = {n: i for i, n in enumerate(feature_names())}
    return {u: phi_feat[:, [index[f] for f in fs]].sum(axis=1) for u, fs in UNITS.items()}


def contribution_frame(model, df: pl.DataFrame) -> tuple[np.ndarray, float]:
    """(n x 28 contributions in registry order, base value), both in raw log-odds."""
    phi, base = model.contributions(df)
    return aggregate(phi), base


def global_importance(units: dict[str, np.ndarray]) -> pl.DataFrame:
    """Mean absolute contribution per unit, its share of the total, and the mean signed value."""
    rows = [
        {
            "unit": u,
            "group": GROUP_OF[UNITS[u][0]],
            "mean_abs": float(np.abs(v).mean()),
            "mean_signed": float(v.mean()),
        }
        for u, v in units.items()
    ]
    df = pl.DataFrame(rows)
    total = float(df["mean_abs"].sum()) or 1.0
    return (
        df.with_columns((pl.col("mean_abs") / total).alias("share"))
        .sort("mean_abs", descending=True)
        .with_row_index("rank", offset=1)
    )


def group_importance(units: dict[str, np.ndarray]) -> pl.DataFrame:
    """Importance by feature group: mean over rows of the sum of absolute contributions."""
    sums: dict[str, np.ndarray] = {}
    for u, v in units.items():
        g = GROUP_OF[UNITS[u][0]]
        sums[g] = sums.get(g, 0) + np.abs(v)
    df = pl.DataFrame([{"group": g, "mean_abs": float(v.mean())} for g, v in sums.items()])
    total = float(df["mean_abs"].sum()) or 1.0
    return df.with_columns((pl.col("mean_abs") / total).alias("share")).sort(
        "mean_abs", descending=True
    )


def _ranks(x: np.ndarray) -> np.ndarray:
    return _avg_rank(-x)


def rank_correlation(a: np.ndarray, b: np.ndarray) -> float:
    """Spearman correlation of two importance vectors (ties are not expected)."""
    ra, rb = _ranks(np.asarray(a, float)), _ranks(np.asarray(b, float))
    if ra.std() == 0 or rb.std() == 0:
        return float("nan")
    return float(np.corrcoef(ra, rb)[0, 1])


def top_overlap(a: pl.DataFrame, b: pl.DataFrame, k: int = 10) -> int:
    return len(set(a.head(k)["unit"].to_list()) & set(b.head(k)["unit"].to_list()))


# ---- permutation importance (a model-agnostic cross-check) -------------------------


def permutation_importance(
    scorer, df: pl.DataFrame, y: np.ndarray, sets: dict[str, list[str]], n_repeats: int = 8,
    seed: int = 0,
) -> pl.DataFrame:  # fmt: skip
    """PR-AUC lost when the listed columns are shuffled together (rows keep their other values).

    The spread is the standard deviation over shuffles; it shows shuffle noise only, not the
    sampling uncertainty of the test window.
    """
    base = pr_auc(y.astype(float), scorer.score(df))
    rng = np.random.default_rng(seed)
    rows = []
    for name, cols in sets.items():
        drops = []
        for _ in range(n_repeats):
            perm = rng.permutation(df.height)
            shuffled = df.with_columns([df[c].gather(perm) for c in cols])
            drops.append(base - pr_auc(y.astype(float), scorer.score(shuffled)))
        rows.append(
            {"unit": name, "drop": float(np.mean(drops)), "sd": float(np.std(drops, ddof=1))}
        )
    return pl.DataFrame(rows).with_columns(pl.lit(base).alias("base_pr_auc"))


def permutation_sets() -> dict[str, list[str]]:
    return {u: list(fs) for u, fs in UNITS.items()}


def permutation_group_sets() -> dict[str, list[str]]:
    groups: dict[str, list[str]] = {}
    for f in FEATURES:
        groups.setdefault(f.group, []).append(f.name)
    return groups


# ---- reason codes ----------------------------------------------------------------


def _value_text(feature: str, value) -> str:
    if value is None or (isinstance(value, float) and np.isnan(value)):
        return "no history"
    if DTYPE_OF[feature] == "bool":
        return "yes" if value else "no"
    if isinstance(value, float):
        return f"{value:,.0f}" if abs(value) >= 100 else f"{value:,.2f}"
    return f"{value:,}" if isinstance(value, int) else str(value)


def reason_codes(
    df: pl.DataFrame, phi_feat: np.ndarray, a: float = 1.0, top_k: int = 3
) -> pl.DataFrame:
    """The features that pushed each transaction's risk up the most.

    One row per (transaction, rank). `effect` is in log-odds (multiplied by `a`, the calibration
    slope, so it is on the calibrated scale). Only risk-raising features are listed; a
    transaction with fewer than `top_k` of them gets fewer rows.
    """
    if top_k < 1:
        raise ExplainError("top_k must be at least 1")
    names = feature_names()
    # an amount reason is shown once, under the unit's first feature
    ids = df["transaction_id"].to_list()
    out: list[dict] = []
    values = {n: df[n].to_list() for n in names}
    unit_cols = {u: [names.index(f) for f in fs] for u, fs in UNITS.items()}
    unit_phi = np.column_stack([phi_feat[:, c].sum(axis=1) for c in unit_cols.values()]) * a
    unit_names = list(UNITS)
    for i in range(df.height):
        order = np.argsort(-unit_phi[i])[:top_k]
        rank = 0
        for j in order:
            if unit_phi[i, j] <= 0:
                break
            rank += 1
            u = unit_names[j]
            f = UNITS[u][0]
            out.append(
                {
                    "transaction_id": ids[i],
                    "rank": rank,
                    "feature": u,
                    "effect": float(unit_phi[i, j]),
                    "text": f"{LABELS[u]}: {_value_text(f, values[f][i])}",
                }
            )
    schema = {
        "transaction_id": df.schema["transaction_id"],
        "rank": pl.Int64,
        "feature": pl.String,
        "effect": pl.Float64,
        "text": pl.String,
    }
    return pl.DataFrame(out, schema=schema)


# ---- how the contribution depends on the value --------------------------------------


def _row(label: str, n: int, effect: float, x: float = float("nan"),
         lo: float = float("nan"), hi: float = float("nan")) -> dict:  # fmt: skip
    return {"bin": label, "n": n, "mean_effect": effect, "x": x, "lo": lo, "hi": hi}


def _num(v: float) -> str:
    return f"{v:,.0f}" if abs(v) >= 1000 or float(v).is_integer() else f"{v:.3g}"


def dependence(df: pl.DataFrame, unit_phi: np.ndarray, unit: str, bins: int = 5) -> pl.DataFrame:
    """Mean contribution by value: categories and flags as they are, numbers in quantile bins.

    Many features are zero for most rows, so an exact zero gets its own bin when it is common.
    """
    f = "amount_kes" if unit == "amount" else UNITS[unit][0]
    col = df[f]
    phi = unit_phi
    rows: list[dict] = []
    nulls = col.is_null().to_numpy()
    if nulls.any():
        rows.append(_row("no history", int(nulls.sum()), float(phi[nulls].mean())))
    ok = ~nulls
    if DTYPE_OF[f] in ("bool", "category"):
        vals = col.cast(pl.String).to_numpy()
        for v in sorted(set(vals[ok])):
            m = ok & (vals == v)
            rows.append(_row(str(v), int(m.sum()), float(phi[m].mean())))
    else:
        x = col.cast(pl.Float64).to_numpy()
        rest = ok.copy()
        k = bins
        if (ok & (x == 0)).sum() >= 0.2 * ok.sum():
            m = ok & (x == 0)
            rows.append(_row("0", int(m.sum()), float(phi[m].mean()), 0.0, 0.0, 0.0))
            rest, k = ok & (x != 0), bins - 1
        if rest.any():
            edges = np.unique(np.quantile(x[rest], np.linspace(0, 1, k + 1)))
            if len(edges) < 2:
                edges = np.array([x[rest].min(), x[rest].max() + 1.0])
            idx = np.clip(np.searchsorted(edges, x, side="right") - 1, 0, len(edges) - 2)
            for b in range(len(edges) - 1):
                m = rest & (idx == b)
                if m.any():
                    label = f"{_num(x[m].min())} to {_num(x[m].max())}"
                    rows.append(
                        _row(label, int(m.sum()), float(phi[m].mean()), float(np.median(x[m])),
                             float(x[m].min()), float(x[m].max()))
                    )  # fmt: skip
    schema = {"bin": pl.String, "n": pl.Int64, "mean_effect": pl.Float64, "x": pl.Float64,
              "lo": pl.Float64, "hi": pl.Float64}  # fmt: skip
    return pl.DataFrame(rows, schema=schema)


def _avg_rank(x: np.ndarray) -> np.ndarray:
    """Ranks with ties given their average rank."""
    _, inv, counts = np.unique(x, return_inverse=True, return_counts=True)
    start = np.cumsum(counts) - counts
    return (start + (counts - 1) / 2.0)[inv]


def direction(df: pl.DataFrame, unit: str, unit_phi: np.ndarray) -> float | None:
    """Rank correlation between a numeric feature's value and its contribution (None if n/a)."""
    f = "amount_kes" if unit == "amount" else UNITS[unit][0]
    if DTYPE_OF[f] in ("category", "bool"):
        return None
    x = df[f].cast(pl.Float64).to_numpy()
    ok = ~np.isnan(x)
    if ok.sum() < 10 or np.ptp(x[ok]) == 0:
        return None
    ra, rb = _avg_rank(x[ok]), _avg_rank(unit_phi[ok])
    if ra.std() == 0 or rb.std() == 0:
        return None
    return float(np.corrcoef(ra, rb)[0, 1])
