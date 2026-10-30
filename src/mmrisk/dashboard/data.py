"""What the dashboard shows, as plain functions on files. No Streamlit in this module.

Every loader returns a `Loaded` instead of raising, so one missing or damaged file shows a clear
message in its own place and never stops the other pages. The dashboard only reads: it writes no
file and calls no service.

All data is synthetic. Customer identifiers are masked by default in anything built for display.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import polars as pl

from mmrisk.monitoring import checks as mchecks
from mmrisk.registry import Registry, RegistryError
from mmrisk.streaming import store as pstore

OK, MISSING, BROKEN = "ok", "missing", "unreadable"
ACTIONS = ("ALLOW", "REVIEW", "BLOCK")
WINDOWS = ("30m", "1h", "6h", "1d")
QUEUE_LIMIT_MAX = 1000
HISTORY_ROWS = 20
DRIFT_TOP = 10


@dataclass(frozen=True)
class Sources:
    store: Path = Path("data/predictions")
    monitoring_baseline: Path = Path("data/monitoring/baseline.json")
    drift_report: Path = Path("data/drift/report.json")
    registry: Path = Path("models/registry")
    retrain_dir: Path = Path("data/retrain")


@dataclass
class Loaded:
    """A value, or the reason there is none."""

    state: str
    value: Any = None
    message: str = ""
    hint: str = ""

    @property
    def ok(self) -> bool:
        return self.state == OK


def _missing(what: str, path: Path, hint: str) -> Loaded:
    return Loaded(MISSING, message=f"no {what} at {path}", hint=hint)


def _broken(what: str, path: Path, why: object) -> Loaded:
    return Loaded(BROKEN, message=f"cannot read the {what} {path}: {why}")


def _json(path: Path) -> tuple[dict | None, str | None]:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return None, str(exc)
    if not isinstance(data, dict):
        return None, "not a JSON object"
    return data, None


# ---- the prediction store ---------------------------------------------------------------------


def store_signature(path: Path) -> tuple:
    """Changes whenever a part file is added: lets a cache know when to reload."""
    return tuple((p.name, p.stat().st_size, p.stat().st_mtime_ns) for p in pstore.parts(path))


def load_store(path: Path) -> Loaded:
    path = Path(path)
    if not pstore.parts(path):
        return _missing("predictions", path, "run: python -m mmrisk.streaming run")
    try:
        df = pstore.read(path)
    except Exception as exc:  # noqa: BLE001 - any unreadable part file is shown, not raised
        return _broken("predictions", path, exc)
    missing = [c for c in pstore.SCHEMA if c not in df.columns]
    if missing:
        return Loaded(BROKEN, message=f"the predictions lack columns: {', '.join(missing)}")
    return Loaded(OK, df)


def overview(df: pl.DataFrame, window: str) -> dict:
    """Headline numbers and the per-window table for the Overview page."""
    if df.is_empty():
        return {"rows": 0}
    n = df.height
    p = df["risk_probability"]
    share = {a: float((df["action"] == a).sum()) / n for a in ACTIONS}
    return {
        "rows": n,
        "first_event": df["event_time"].min(),
        "last_event": df["event_time"].max(),
        "action_share": share,
        "action_count": {a: int((df["action"] == a).sum()) for a in ACTIONS},
        "flagged_share": share["REVIEW"] + share["BLOCK"],
        "mean_probability": float(p.mean()),
        "p99_probability": float(p.quantile(0.99)),
        "feature_versions": sorted(df["feature_version"].unique().to_list()),
        "windows": mchecks.windows(df, window),
    }


def probability_histogram(df: pl.DataFrame, bins: int = 20) -> pl.DataFrame:
    """Rows per equal-width probability bin between 0 and 1 (`bin` is the lower edge)."""
    if bins < 1:
        raise ValueError("bins must be at least 1")
    width = 1.0 / bins
    idx = (df["risk_probability"] / width).floor().clip(0, bins - 1).cast(pl.Int64)
    counted = pl.DataFrame({"i": idx}).group_by("i").len().rename({"len": "rows"})
    full = pl.DataFrame({"i": list(range(bins))})
    return (
        full.join(counted, on="i", how="left")
        .with_columns(pl.col("rows").fill_null(0), (pl.col("i") * width).alias("bin"))
        .select("bin", "rows")
        .sort("bin")
    )


# ---- the review queue -------------------------------------------------------------------------


def mask_id(value: str | None) -> str:
    """`C000123` becomes `C000***`: enough to tell customers apart on screen, not to look one up."""
    if not value:
        return ""
    keep = max(1, len(value) - 3)
    return value[:keep] + "*" * (len(value) - keep)


def parse_reasons(raw: str | None) -> list[dict]:
    """The reason codes stored with a prediction; anything unreadable gives an empty list."""
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except ValueError:
        return []
    if not isinstance(data, list):
        return []
    return [r for r in data if isinstance(r, dict)]


def _top_reason(raw: str | None) -> str:
    reasons = parse_reasons(raw)
    return str(reasons[0].get("text", "")) if reasons else ""


def review_queue(
    df: pl.DataFrame,
    *,
    actions: tuple[str, ...] = ("REVIEW", "BLOCK"),
    min_probability: float = 0.0,
    counties: list[str] | None = None,
    channels: list[str] | None = None,
    limit: int = 100,
    mask: bool = True,
) -> pl.DataFrame:
    """The transactions a reviewer would look at first: highest probability first.

    Ties are broken by event time and then transaction id, so the order never changes between
    refreshes.
    """
    if not 1 <= limit <= QUEUE_LIMIT_MAX:
        raise ValueError(f"limit must be between 1 and {QUEUE_LIMIT_MAX}")
    if not 0.0 <= min_probability <= 1.0:
        raise ValueError("min_probability must be between 0 and 1")
    unknown = [a for a in actions if a not in ACTIONS]
    if unknown:
        raise ValueError(f"unknown actions: {unknown}")
    q = df.filter(
        pl.col("action").is_in(list(actions)) & (pl.col("risk_probability") >= min_probability)
    )
    if counties:
        q = q.filter(pl.col("location_county").is_in(counties))
    if channels:
        q = q.filter(pl.col("channel").is_in(channels))
    q = q.sort(
        ["risk_probability", "event_time", "transaction_id"], descending=[True, False, False]
    ).head(limit)
    customer = [mask_id(c) for c in q["customer_id"]] if mask else q["customer_id"].to_list()
    return pl.DataFrame(
        {
            "transaction_id": q["transaction_id"],
            "event_time": q["event_time"],
            "customer": customer,
            "type": q["transaction_type"],
            "amount_kes": q["amount_kes"],
            "channel": q["channel"],
            "county": q["location_county"],
            "probability": q["risk_probability"],
            "action": q["action"],
            "top_reason": [_top_reason(r) for r in q["reasons"]],
        },
        schema={
            "transaction_id": pl.Utf8, "event_time": pl.Datetime("us"), "customer": pl.Utf8,
            "type": pl.Utf8, "amount_kes": pl.Int64, "channel": pl.Utf8, "county": pl.Utf8,
            "probability": pl.Float64, "action": pl.Utf8, "top_reason": pl.Utf8,
        },
    )  # fmt: skip


def transaction_detail(df: pl.DataFrame, transaction_id: str, mask: bool = True) -> dict | None:
    rows = df.filter(pl.col("transaction_id") == transaction_id)
    if rows.is_empty():
        return None
    row = rows.row(0, named=True)  # the first scoring of a repeated id; the store keeps one
    customer = mask_id(row["customer_id"]) if mask else row["customer_id"]
    return {
        "transaction_id": row["transaction_id"],
        "customer": customer,
        "event_time": row["event_time"],
        "type": row["transaction_type"],
        "amount_kes": row["amount_kes"],
        "channel": row["channel"],
        "county": row["location_county"],
        "probability": row["risk_probability"],
        "action": row["action"],
        "risk_level": row["risk_level"],
        "feature_version": row["feature_version"],
        "reasons": parse_reasons(row["reasons"]),
    }


# ---- health, drift ----------------------------------------------------------------------------


def health(
    df: pl.DataFrame,
    baseline_path: Path | None,
    window: str,
    as_of: datetime | None = None,
    review_capacity: float | None = None,
) -> Loaded:
    """The prediction-store checks of Phase 14, run on what is in the store now."""
    baseline = None
    note = ""
    if baseline_path is not None and Path(baseline_path).exists():
        baseline, why = _json(Path(baseline_path))
        need = {"flagged_share", "action_share", "rows_per_hour"}
        if (
            baseline is None
            or not need <= baseline.keys()
            or not isinstance(baseline["action_share"], dict)
            or "BLOCK" not in baseline["action_share"]
        ):
            return _broken("monitoring baseline", Path(baseline_path), why or "not a baseline")
    else:
        note = "no baseline: the comparisons with a healthy period are skipped"
    try:
        checks, table = mchecks.run_checks(df, baseline, window, as_of, review_capacity)
    except mchecks.MonitoringError as exc:
        return Loaded(BROKEN, message=str(exc))
    return Loaded(
        OK,
        {"overall": mchecks.overall(checks), "checks": checks, "windows": table, "note": note},
    )


def drift_summary(path: Path) -> Loaded:
    path = Path(path)
    if not path.exists():
        return _missing("drift report", path, "run: .\\scripts\\dev.ps1 driftref, then drift")
    data, why = _json(path)
    if data is None or "drift_detected" not in data or "results" not in data:
        return _broken("drift report", path, why or "not a report written by mmrisk.drift")
    scored = [r for r in data["results"] if isinstance(r, dict) and r.get("psi") is not None]
    top = sorted(scored, key=lambda r: r["psi"], reverse=True)[:DRIFT_TOP]
    return Loaded(OK, {
        "detected": bool(data["drift_detected"]),
        "verdict": data.get("verdict", ""),
        "window": data.get("window", ""),
        "reference_window": data.get("reference_window", ""),
        "injected": data.get("injected"),
        "rows": data.get("rows"),
        "top": pl.DataFrame(
            {
                "feature": [r["name"] for r in top],
                "psi": [float(r["psi"]) for r in top],
                "noise": [r.get("noise") for r in top],
                "status": [r.get("status", "") for r in top],
                "policy": [r.get("policy", "") for r in top],
            },
            schema={"feature": pl.Utf8, "psi": pl.Float64, "noise": pl.Float64,
                    "status": pl.Utf8, "policy": pl.Utf8},
        ),
    })  # fmt: skip


# ---- models -----------------------------------------------------------------------------------


@dataclass
class ModelsView:
    versions: pl.DataFrame
    production: dict | None
    history: list[dict] = field(default_factory=list)
    details: dict[str, dict] = field(default_factory=dict)


def models_view(root: Path) -> Loaded:
    root = Path(root)
    if not root.exists():
        return _missing("model registry", root, "run: .\\scripts\\dev.ps1 regboot")
    reg = Registry(root)
    try:
        infos = reg.versions()
        production = reg.production()
        history = reg.history()
    except (RegistryError, ValueError, OSError, KeyError) as exc:
        return _broken("model registry", root, exc)
    if not infos:
        return _missing("model versions", root, "run: .\\scripts\\dev.ps1 regboot")
    live = production["version"] if production else None
    frame = pl.DataFrame(
        {
            "version": [i["version"] for i in infos],
            "stage": [i["stage"] for i in infos],
            "production": [i["version"] == live for i in infos],
            "created_at": [i["created_at"] for i in infos],
            "source": [i["source"] for i in infos],
            "parent": [i["parent"] for i in infos],
            "quick": [bool(i["quick"]) for i in infos],
            "note": [i["note"] for i in infos],
        },
        schema={"version": pl.Utf8, "stage": pl.Utf8, "production": pl.Boolean,
                "created_at": pl.Utf8, "source": pl.Utf8, "parent": pl.Utf8,
                "quick": pl.Boolean, "note": pl.Utf8},
    )  # fmt: skip
    return Loaded(OK, ModelsView(frame, production, history[-HISTORY_ROWS:][::-1],
                                 {i["version"]: i for i in infos}))  # fmt: skip


def gates_table(info: dict) -> pl.DataFrame:
    """The gate results saved with a version (empty for the bootstrap model, which had none)."""
    gates = [g for g in info.get("gates", []) if isinstance(g, dict)]
    return pl.DataFrame(
        {
            "gate": [g.get("name", "") for g in gates],
            "kind": ["required" if g.get("required") else "advisory" for g in gates],
            "result": [
                "PASS" if g.get("passed") else ("FAIL" if g.get("required") else "WARN")
                for g in gates
            ],
            "detail": [g.get("detail", "") for g in gates],
        },
        schema={"gate": pl.Utf8, "kind": pl.Utf8, "result": pl.Utf8, "detail": pl.Utf8},
    )


def retrain_summary(directory: Path) -> Loaded:
    directory = Path(directory)
    check, why_check = _json(directory / "check.json")
    run, why_run = _json(directory / "report.json")
    if check is None and run is None:
        if (directory / "check.json").exists() or (directory / "report.json").exists():
            return _broken("retraining results", directory, why_check or why_run)
        return _missing("retraining results", directory, "run: .\\scripts\\dev.ps1 retrain-check")
    return Loaded(OK, {"check": check, "run": run})


# ---- where the data comes from ---------------------------------------------------------------


def source_status(src: Sources) -> list[dict]:
    rows = [
        ("predictions", src.store, bool(pstore.parts(src.store))),
        ("monitoring baseline", src.monitoring_baseline, src.monitoring_baseline.exists()),
        ("drift report", src.drift_report, src.drift_report.exists()),
        ("model registry", src.registry, (src.registry / "production.json").exists()),
        ("retraining results", src.retrain_dir, any(
            (src.retrain_dir / f).exists() for f in ("check.json", "report.json"))),
    ]  # fmt: skip
    return [{"source": n, "path": str(p).replace("\\", "/"), "found": f} for n, p, f in rows]


__all__ = [
    "ACTIONS", "BROKEN", "MISSING", "OK", "WINDOWS", "Loaded", "ModelsView", "Sources",
    "drift_summary", "gates_table", "health", "load_store", "mask_id", "models_view",
    "overview", "parse_reasons", "probability_histogram", "retrain_summary", "review_queue",
    "source_status", "store_signature", "transaction_detail",
]  # fmt: skip
