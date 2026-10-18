"""Phase 9 experiment: explain the shipped model and check the explanations.

Reads the Phase 8 bundle (`models/advanced`), recomputes the split from the data, and refuses to run
if the bundle was built on a different split or feature version. Nothing here changes the model.
The explanations are hypotheses about what the model uses, so they are cross-checked three ways:
permutation importance, stability between two time windows, and refitting without a feature
(validation window only, the test window is never used to make a choice).
"""

from __future__ import annotations

import json
from pathlib import Path

import lightgbm as lgb
import numpy as np
import polars as pl

from mmrisk.features.registry import FEATURE_VERSION
from mmrisk.models import explain as X
from mmrisk.models.boosting import lgbm_params
from mmrisk.models.bundle import META_FILE, RiskModel
from mmrisk.models.calibration import wilson
from mmrisk.models.metrics import pr_auc
from mmrisk.models.preprocess import column_names, to_matrix
from mmrisk.models.split import Split, make_split

SEGMENT_COLUMNS = ("segment", "channel", "is_away_from_home")
AGE_BINS = ((0, 90, "under 90 days"), (90, 365, "90 to 364 days"), (365, 10**9, "1 year or more"))


class BundleMismatchError(ValueError):
    pass


def load_bundle(models_dir: Path, split: Split) -> RiskModel:
    models_dir = Path(models_dir)
    if not (models_dir / META_FILE).exists():
        raise BundleMismatchError(f"no risk model in {models_dir}; run the advanced step first")
    meta = json.loads((models_dir / META_FILE).read_text())
    if meta.get("feature_version") != FEATURE_VERSION:
        raise BundleMismatchError(
            f"the model uses feature set {meta.get('feature_version')}, code has {FEATURE_VERSION}"
        )
    saved = meta.get("split", {})
    now = {k: str(v) for k, v in split.bounds.items()}
    if saved and saved != now:
        raise BundleMismatchError(
            "the model was built on a different split; rerun the advanced step"
        )
    model = RiskModel.load(models_dir)
    if not hasattr(model.scorer, "contributions"):
        raise BundleMismatchError("the saved scorer cannot be explained (expected LightGBM)")
    return model


def _flagged(action: np.ndarray) -> np.ndarray:
    return action != "ALLOW"


def _importance_block(split_df: pl.DataFrame, model: RiskModel) -> dict:
    phi, base = X.contribution_frame(model.scorer, split_df)
    raw = model.scorer.score(split_df)
    logit = np.log(np.clip(raw, 1e-12, 1 - 1e-12) / (1 - np.clip(raw, 1e-12, 1 - 1e-12)))
    units = X.by_unit(phi)
    return {
        "phi": phi,
        "base": base,
        "units": units,
        "additivity_error": float(np.abs(base + phi.sum(axis=1) - logit).max()),
    }


def refit_without(model: RiskModel, split: Split, unit_features: list[str], seed: int) -> float:
    """Validation PR-AUC of the same settings refit with the listed features blanked out."""
    cols = column_names("tree")
    drop = [i for i, c in enumerate(cols) if X._source(c) in unit_features]
    x_tr, x_va = to_matrix(split.train, "tree"), to_matrix(split.val, "tree")
    x_tr[:, drop], x_va[:, drop] = np.nan, np.nan
    cfg = model.scorer.params
    params = lgbm_params(cfg["num_leaves"], cfg["min_child_samples"], cfg["pos_weight"], seed)
    y_tr = split.train["label_is_fraud"].to_numpy()
    booster = lgb.train(
        params, lgb.Dataset(x_tr, label=y_tr), num_boost_round=model.scorer.n_rounds
    )
    y_va = split.val["label_is_fraud"].to_numpy()
    return pr_auc(y_va, booster.predict(x_va))


def _slice_table(test: pl.DataFrame, assess: pl.DataFrame) -> list[dict]:
    y = test["label_is_fraud"].to_numpy().astype(bool)
    flagged = _flagged(assess["action"].to_numpy())
    blocked = assess["action"].to_numpy() == "BLOCK"
    rows = []
    age = test["account_age_days"].to_numpy()
    specs = [(c, test[c].cast(pl.String).to_numpy()) for c in SEGMENT_COLUMNS]
    specs.append(
        (
            "account_age",
            np.array([next(lbl for lo, hi, lbl in AGE_BINS if lo <= a < hi) for a in age]),
        )
    )
    for name, vals in specs:
        for v in sorted(set(vals)):
            m = vals == v
            n_fraud = int((m & y).sum())
            n_legit = int((m & ~y).sum())
            hit = int((m & y & flagged).sum())
            ci = wilson(hit, n_fraud) if n_fraud else None
            rows.append(
                {
                    "slice": name,
                    "value": str(v),
                    "n": int(m.sum()),
                    "fraud_labelled": n_fraud,
                    "flagged_per_10k": float((m & flagged).sum() / m.sum() * 10_000),
                    "blocked_per_10k": float((m & blocked).sum() / m.sum() * 10_000),
                    "fraud_flagged": hit / n_fraud if n_fraud else None,
                    "fraud_flagged_ci": None if ci is None else list(ci),
                    "legit_flagged_per_10k": float(
                        (m & ~y & flagged).sum() / max(1, n_legit) * 10_000
                    ),
                }
            )
    return rows


def _examples(test: pl.DataFrame, assess: pl.DataFrame) -> dict[str, int | None]:
    """Row positions: a caught fraud, a false alarm and a missed fraud (fixed, stated rules)."""
    y = test["label_is_fraud"].to_numpy().astype(bool)
    p = assess["risk_probability"].to_numpy()
    act = assess["action"].to_numpy()
    amount = test["amount_kes"].to_numpy().astype(float)

    def pick(mask, key):
        idx = np.flatnonzero(mask)
        return int(idx[np.argmax(key[idx])]) if len(idx) else None

    return {
        "caught_fraud": pick(y & (act == "BLOCK"), p),
        "false_alarm": pick(~y & (act == "BLOCK"), p),
        "missed_fraud": pick(y & (act == "ALLOW"), amount),
    }


def run_explain(
    features: pl.DataFrame,
    labels: pl.DataFrame,
    models_dir: Path,
    report_path: Path,
    figures_dir: Path,
    truth_file: Path | None = None,
    n_repeats: int = 8,
    ablation_seeds: int = 3,
    seed: int = 42,
    quick: bool = False,
) -> object:
    from mmrisk.models import explain_report as R

    split = make_split(features, labels)
    model = load_bundle(models_dir, split)
    a = model.calibrator.a
    test, val = split.test, split.val
    y_test = test["label_is_fraud"].to_numpy()

    # ---- contributions on both windows ----------
    tb, vb = _importance_block(test, model), _importance_block(val, model)
    fraud_rows = y_test == 1
    glob_all = X.global_importance(tb["units"])
    glob_fraud = X.global_importance({u: v[fraud_rows] for u, v in tb["units"].items()})
    glob_val = X.global_importance(vb["units"])
    y_val = val["label_is_fraud"].to_numpy()
    glob_val_fraud = X.global_importance({u: v[y_val == 1] for u, v in vb["units"].items()})
    groups = X.group_importance(tb["units"])
    groups_fraud = X.group_importance({u: v[fraud_rows] for u, v in tb["units"].items()})

    # ---- permutation importance on the test window ----------
    reps = 3 if quick else n_repeats
    perm = X.permutation_importance(model.scorer, test, y_test, X.permutation_sets(), reps, seed)
    perm_groups = X.permutation_importance(
        model.scorer, test, y_test, X.permutation_group_sets(), reps, seed
    )
    order = glob_all["unit"].to_list()
    agree = {
        "spearman_shap_all_vs_permutation": X.rank_correlation(
            glob_all.sort("unit")["mean_abs"].to_numpy(), perm.sort("unit")["drop"].to_numpy()
        ),
        "spearman_shap_fraud_vs_permutation": X.rank_correlation(
            glob_fraud.sort("unit")["mean_abs"].to_numpy(), perm.sort("unit")["drop"].to_numpy()
        ),
        "spearman_val_vs_test": X.rank_correlation(
            glob_val.sort("unit")["mean_abs"].to_numpy(),
            glob_all.sort("unit")["mean_abs"].to_numpy(),
        ),
        "top10_overlap_val_test": X.top_overlap(glob_val, glob_all, 10),
        "spearman_fraud_rows_val_vs_test": X.rank_correlation(
            glob_val_fraud.sort("unit")["mean_abs"].to_numpy(),
            glob_fraud.sort("unit")["mean_abs"].to_numpy(),
        ),
        "top10_overlap_fraud_rows_val_test": X.top_overlap(glob_val_fraud, glob_fraud, 10),
        "n_validation_fraud_labelled": int(y_val.sum()),
        "top10_overlap_shap_all_permutation": len(
            set(order[:10]) & set(perm.sort("drop", descending=True).head(10)["unit"].to_list())
        ),
        "top10_overlap_shap_fraud_permutation": len(
            set(glob_fraud.head(10)["unit"].to_list())
            & set(perm.sort("drop", descending=True).head(10)["unit"].to_list())
        ),
    }

    # ---- decisions, reason codes and examples ----------
    assess = model.assess(test)
    action = assess["action"].to_numpy()
    flagged = _flagged(action)
    codes = X.reason_codes(test.filter(pl.Series(flagged)), tb["phi"][flagged], a=a, top_k=3)
    top_reason = (
        codes.filter(pl.col("rank") == 1).group_by("feature").len().sort("len", descending=True)
    )
    coverage = float(codes["transaction_id"].n_unique() / max(1, int(flagged.sum())))
    examples = _examples(test, assess)

    # ---- dependence for the units that matter for fraud rows ----------
    focus = glob_fraud.head(6)["unit"].to_list()
    dep = {u: X.dependence(test, tb["units"][u] * a, u) for u in focus}
    dirs = {u: X.direction(test, u, tb["units"][u]) for u in focus}

    # ---- does the model lean on the signals each fraud pattern is built on? ----------
    patterns = _pattern_check(test, tb, flagged, a, truth_file)

    # ---- refit without a feature (validation only) ----------
    cand = list(dict.fromkeys(
        glob_fraud.head(4)["unit"].to_list()
        + perm.sort("drop", descending=True).head(4)["unit"].to_list()
        + ["segment", "channel", "n_prior_txns"]
    ))  # fmt: skip
    seeds = 1 if quick else ablation_seeds
    base_runs = [refit_without(model, split, [], seed + i) for i in range(seeds)]
    ablation = []
    for u in cand:
        feats = X.UNITS[u]
        runs = [refit_without(model, split, feats, seed + i) for i in range(seeds)]
        ablation.append({"unit": u, "val_pr_auc": float(np.mean(runs)),
                         "delta": float(np.mean(runs) - np.mean(base_runs)),
                         "sd": float(np.std(runs, ddof=1)) if seeds > 1 else None})  # fmt: skip
    ablation_base = {"val_pr_auc": float(np.mean(base_runs)), "seeds": seeds}

    slices = _slice_table(test, assess)

    metrics = {
        "additivity_error_max": max(tb["additivity_error"], vb["additivity_error"]),
        "base_log_odds": tb["base"],
        "calibration_slope": a,
        "n_test": test.height,
        "n_test_fraud_labelled": int(fraud_rows.sum()),
        "n_flagged": int(flagged.sum()),
        "reason_code_coverage": coverage,
        "agreement": agree,
        "importance_all": glob_all.to_dicts(),
        "importance_fraud_rows": glob_fraud.to_dicts(),
        "importance_validation": glob_val.to_dicts(),
        "groups_all": groups.to_dicts(),
        "groups_fraud_rows": groups_fraud.to_dicts(),
        "permutation": perm.sort("drop", descending=True).to_dicts(),
        "permutation_groups": perm_groups.sort("drop", descending=True).to_dicts(),
        "top_reason_among_flagged": top_reason.to_dicts(),
        "direction": dirs,
        "patterns": patterns,
        "ablation_base": ablation_base,
        "ablation": ablation,
        "slices": slices,
        "seed": seed,
        "quick": quick,
        "permutation_repeats": reps,
        "split_summary": [
            {k: str(v) if k in ("first", "last") else v for k, v in r.items()}
            for r in split.summary().to_dicts()
        ],
        "split_bounds": {k: str(v) for k, v in split.bounds.items()},
    }
    result = R.write(
        split=split, model=model, tb=tb, glob_all=glob_all, glob_fraud=glob_fraud, perm=perm,
        perm_groups=perm_groups, groups=groups, dep=dep, examples=examples, test=test,
        assess=assess, codes=codes, metrics=metrics, report_path=Path(report_path),
        figures_dir=Path(figures_dir), focus=focus,
    )  # fmt: skip
    models_dir = Path(models_dir)
    (models_dir / "explain_metrics.json").write_text(
        json.dumps(metrics, indent=2, default=str) + "\n"
    )
    return result


def _pattern_check(test, tb, flagged, a, truth_file) -> list[dict] | None:
    if truth_file is None or not Path(truth_file).exists():
        return None
    truth = pl.read_parquet(truth_file).select("transaction_id", "fraud_pattern")
    pat = test.join(truth, on="transaction_id", how="left")["fraud_pattern"].to_numpy()
    unit_names = list(X.UNITS)
    mat = np.column_stack([tb["units"][u] for u in unit_names]) * a
    out = []
    for p in sorted({v for v in pat if v is not None}):
        m = (pat == p) & flagged
        n_all = int((pat == p).sum())
        row = {"pattern": p, "rows": n_all, "flagged": int(m.sum()),
               "expected": X.EXPECTED_SIGNALS.get(p, [])}  # fmt: skip
        if m.sum():
            mean = mat[m].mean(axis=0)
            top = [unit_names[i] for i in np.argsort(-mean)[:3]]
            top3 = np.argsort(-mat[m], axis=1)[:, :3]
            exp_idx = {unit_names.index(u) for u in row["expected"] if u in unit_names}
            hit = float(np.mean([bool(exp_idx & set(r)) for r in top3]))
            row |= {"leading_features": top, "expected_in_top3": hit}
        out.append(row)
    return out
