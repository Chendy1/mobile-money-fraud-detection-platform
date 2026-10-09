"""Dataset-level data-quality checks over the lake (run after ingestion).

Row-level rules quarantine bad rows during ingestion; these checks look at the *dataset*:
completeness, duplicates, referential integrity, volume, timing, drift and consistency.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import polars as pl

from mmrisk.ingestion.lake import LakePaths, atomic_write_text
from mmrisk.ingestion.ledger import COMPLETE, REJECTED, Ledger
from mmrisk.ingestion.reader import read_layer
from mmrisk.ingestion.reconcile import reconcile
from mmrisk.ingestion.specs import INGEST_ORDER, SPECS
from mmrisk.validation import config as cfg

PASS, WARN, FAIL, SKIPPED = "PASS", "WARN", "FAIL", "SKIPPED"
_SEVERITY = {PASS: 0, SKIPPED: 0, WARN: 1, FAIL: 2}


@dataclass
class CheckResult:
    name: str
    table: str
    status: str
    observed: str
    threshold: str
    message: str = ""


@dataclass
class DQReport:
    checks: list[CheckResult] = field(default_factory=list)
    as_of: str | None = None
    generated_at: str = ""

    @property
    def status(self) -> str:
        worst = max((_SEVERITY[c.status] for c in self.checks), default=0)
        return {0: PASS, 1: WARN, 2: FAIL}[worst]

    def counts(self) -> dict[str, int]:
        out = {PASS: 0, WARN: 0, FAIL: 0, SKIPPED: 0}
        for c in self.checks:
            out[c.status] += 1
        return out

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "counts": self.counts(),
            "as_of": self.as_of,
            "generated_at": self.generated_at,
            "checks": [asdict(c) for c in self.checks],
        }


def _pct(x: float) -> str:
    return f"{x:.4%}"


def _rate_status(rate: float, warn: float, fail: float) -> str:
    return FAIL if rate > fail else WARN if rate > warn else PASS


class _Run:
    def __init__(self, root: Path):
        self.root = root
        self.ledger = Ledger.load(LakePaths(root).ledger_path)
        self.loaded = sorted({b["table"] for b in self.ledger.complete()})
        self.report = DQReport()
        self._cleaned: dict[str, pl.DataFrame] = {}

    def cleaned(self, table: str) -> pl.DataFrame:
        if table not in self._cleaned:
            self._cleaned[table] = read_layer(self.root, "cleaned", table)
        return self._cleaned[table]

    def add(self, name, table, status, observed, threshold, message=""):
        self.report.checks.append(
            CheckResult(name, table, status, str(observed), str(threshold), message)
        )


def _table_loaded(r: _Run) -> None:
    for t in INGEST_ORDER:
        ok = t in r.loaded
        r.add(
            "table_loaded",
            t,
            PASS if ok else FAIL,
            "loaded" if ok else "missing",
            "loaded",
            "" if ok else "no COMPLETE batch for this table",
        )


def _batches_rejected(r: _Run) -> None:
    rejected = [b for b in r.ledger.batches if b["status"] == REJECTED]
    if not rejected:
        r.add("batches_rejected", "all", PASS, 0, 0)
        return
    for b in rejected:
        later = [c for c in r.ledger.complete(b["table"]) if c["ingested_at"] >= b["ingested_at"]]
        status = WARN if later else FAIL
        note = "table was loaded successfully afterwards" if later else "no later successful load"
        r.add(
            "batches_rejected",
            b["table"],
            status,
            b["source_file"],
            "none",
            f"{b['reason']}; {note}",
        )


def _schema_drift(r: _Run) -> None:
    for t in r.loaded:
        spec, drift = SPECS[t], []
        for b in r.ledger.complete(t):
            for col, dtype in (b.get("source_schema") or {}).items():
                if col in spec.schema and dtype != str(spec.schema[col]):
                    drift.append(
                        f"{b['source_file']}: {col} arrived as {dtype}, expected {spec.schema[col]}"
                    )
        r.add("schema_drift", t, WARN if drift else PASS, len(drift), 0, "; ".join(drift[:3]))


def _intake_rates(r: _Run) -> None:
    for t in r.loaded:
        done = r.ledger.complete(t)
        rows_in = sum(b["rows_in"] for b in done)
        if rows_in == 0:
            continue
        q = sum(b["rows_rejected"] + b["rows_conflict"] for b in done) / rows_in
        r.add(
            "quarantine_rate",
            t,
            _rate_status(q, cfg.QUARANTINE_WARN, cfg.QUARANTINE_FAIL),
            _pct(q),
            f"warn>{_pct(cfg.QUARANTINE_WARN)} fail>{_pct(cfg.QUARANTINE_FAIL)}",
        )
        d = sum(b["rows_duplicate"] for b in done) / rows_in
        r.add(
            "duplicate_rate",
            t,
            WARN if d > cfg.DUPLICATE_WARN else PASS,
            _pct(d),
            f"warn>{_pct(cfg.DUPLICATE_WARN)}",
            "identical duplicates were dropped" if d else "",
        )
        c = sum(b["rows_conflict"] for b in done) / rows_in
        status = FAIL if c > cfg.CONFLICT_FAIL else WARN if c > 0 else PASS
        r.add(
            "conflicting_duplicates",
            t,
            status,
            _pct(c),
            f"fail>{_pct(cfg.CONFLICT_FAIL)}",
            "same key, different content (quarantined)" if c else "",
        )


def _null_rates_and_keys(r: _Run) -> None:
    for t in r.loaded:
        df, spec = r.cleaned(t), SPECS[t]
        if df.height == 0:
            continue
        over = []
        for col in spec.columns:
            rate = df[col].null_count() / df.height
            if rate > cfg.NULL_RATE_MAX.get(col, 0.0):
                over.append(f"{col}={_pct(rate)}")
        r.add(
            "null_rates",
            t,
            FAIL if over else PASS,
            ", ".join(over) or "within limits",
            "required columns 0%; counterparty columns <=25%",
        )
        dup = df.select(list(spec.key)).is_duplicated().sum()
        r.add(
            "key_uniqueness",
            t,
            FAIL if dup else PASS,
            dup,
            0,
            "duplicate keys in cleaned layer" if dup else "",
        )


def _orphans(child: pl.DataFrame, col: str, parent: pl.DataFrame, pcol: str):
    c = child.filter(pl.col(col).is_not_null())
    keys = parent.select(pl.col(pcol).alias(col)).unique()
    return c.join(keys, on=col, how="anti").height, c.height


def _referential(r: _Run) -> None:
    txn = lambda: r.cleaned("transactions")  # noqa: E731
    relations = [
        ("transactions", "customer_id", "customers", "customer_id", lambda: txn()),
        ("transactions", "device_id", "devices", "device_id", lambda: txn()),
        ("transactions", "location_county", "counties", "county", lambda: txn()),
        ("customers", "home_county", "counties", "county", lambda: r.cleaned("customers")),
        ("merchants", "county", "counties", "county", lambda: r.cleaned("merchants")),
        ("agents", "county", "counties", "county", lambda: r.cleaned("agents")),
        ("labels", "transaction_id", "transactions", "transaction_id", lambda: r.cleaned("labels")),
    ]
    for ctable, ccol, ptable, pcol, child in relations:
        name = f"referential_{ctable}.{ccol}->{ptable}"
        if ctable not in r.loaded or ptable not in r.loaded:
            r.add(name, ctable, SKIPPED, "n/a", "0 orphans", "a table is not loaded")
            continue
        n, total = _orphans(child(), ccol, r.cleaned(ptable), pcol)
        _report_orphans(r, name, ctable, n, total)
    if "transactions" in r.loaded:
        for cp_type, ptable, pcol in (
            ("CUSTOMER", "customers", "customer_id"),
            ("MERCHANT", "merchants", "merchant_id"),
            ("AGENT", "agents", "agent_id"),
        ):
            name = f"referential_transactions.counterparty({cp_type})->{ptable}"
            if ptable not in r.loaded:
                r.add(name, "transactions", SKIPPED, "n/a", "0 orphans", f"{ptable} not loaded")
                continue
            child = r.cleaned("transactions").filter(pl.col("counterparty_type") == cp_type)
            n, total = _orphans(child, "counterparty_id", r.cleaned(ptable), pcol)
            _report_orphans(r, name, "transactions", n, total)


def _report_orphans(r: _Run, name: str, table: str, n: int, total: int) -> None:
    rate = n / total if total else 0.0
    status = PASS if n == 0 else WARN if rate <= cfg.ORPHAN_WARN_MAX else FAIL
    r.add(
        name,
        table,
        status,
        f"{n} of {total} ({_pct(rate)})",
        f"0; warn up to {_pct(cfg.ORPHAN_WARN_MAX)}",
    )


def _label_timing(r: _Run) -> None:
    if not {"labels", "transactions"} <= set(r.loaded):
        r.add(
            "label_timing",
            "labels",
            SKIPPED,
            "n/a",
            "0 violations",
            "labels or transactions not loaded",
        )
        return
    j = r.cleaned("labels").join(
        r.cleaned("transactions").select("transaction_id", "timestamp"), on="transaction_id"
    )
    bad = j.filter(pl.col("label_available_at") <= pl.col("timestamp")).height
    r.add(
        "label_timing",
        "labels",
        FAIL if bad else PASS,
        f"{bad} of {j.height}",
        "0",
        "label known before or at transaction time (leakage risk)" if bad else "",
    )


def _daily_volume(r: _Run) -> None:
    if "transactions" not in r.loaded or r.cleaned("transactions").height == 0:
        return
    daily = r.cleaned("transactions").group_by("event_date").len()
    median = daily["len"].median()
    if median < cfg.VOLUME_MIN_MEDIAN:
        r.add(
            "daily_volume",
            "transactions",
            SKIPPED,
            f"median {median:.0f}/day",
            f">= {cfg.VOLUME_MIN_MEDIAN}/day",
            "too little data per day to judge",
        )
        return
    days = pl.date_range(daily["event_date"].min(), daily["event_date"].max(), "1d", eager=True)
    missing = sorted(set(days.to_list()) - set(daily["event_date"].to_list()))
    low = daily.filter(pl.col("len") < cfg.VOLUME_LOW_RATIO * median)["event_date"].sort().to_list()
    high = (
        daily.filter(pl.col("len") > cfg.VOLUME_HIGH_RATIO * median)["event_date"].sort().to_list()
    )
    status = FAIL if missing else WARN if (low or high) else PASS
    notes = []
    for label, found in (("missing", missing), ("low", low), ("high", high)):
        if found:
            notes.append(f"{label}: {', '.join(str(d) for d in found[:5])}")
    r.add(
        "daily_volume",
        "transactions",
        status,
        f"{len(missing)} missing, {len(low)} low, {len(high)} high days",
        f"low<{cfg.VOLUME_LOW_RATIO}x, high>{cfg.VOLUME_HIGH_RATIO}x median",
        "; ".join(notes),
    )


def _freshness(r: _Run, as_of: datetime | None, max_lag_hours: float | None) -> None:
    if "transactions" not in r.loaded or r.cleaned("transactions").height == 0:
        return
    if as_of is None:
        r.add(
            "freshness",
            "transactions",
            SKIPPED,
            "n/a",
            "n/a",
            "no --as-of given (historical simulation); pass --as-of to enforce",
        )
        return
    limit = cfg.DEFAULT_MAX_LAG_HOURS if max_lag_hours is None else max_lag_hours
    newest = r.cleaned("transactions")["timestamp"].max()
    lag = (as_of - newest).total_seconds() / 3600
    r.add(
        "freshness",
        "transactions",
        PASS if lag <= limit else FAIL,
        f"newest event {newest}, lag {lag:.1f} h",
        f"<= {limit:g} h",
    )


def _row_accounting(r: _Run) -> None:
    problems = reconcile(r.root)
    r.add(
        "row_accounting",
        "all",
        FAIL if problems else PASS,
        f"{len(problems)} problem(s)",
        0,
        "; ".join(problems[:3]),
    )


def run_checks(
    lake_root: Path | str,
    as_of: datetime | None = None,
    max_lag_hours: float | None = None,
    now: datetime | None = None,
    write: bool = True,
) -> DQReport:
    root = Path(lake_root)
    r = _Run(root)
    _table_loaded(r)
    _batches_rejected(r)
    _schema_drift(r)
    _intake_rates(r)
    _null_rates_and_keys(r)
    _referential(r)
    _label_timing(r)
    _daily_volume(r)
    _freshness(r, as_of, max_lag_hours)
    _row_accounting(r)
    stamp = (now or datetime.now(UTC)).replace(tzinfo=None)
    r.report.as_of = as_of.isoformat() if as_of else None
    r.report.generated_at = stamp.isoformat(timespec="seconds")
    if write and root.exists():
        text = json.dumps(r.report.to_dict(), indent=2)
        atomic_write_text(text, LakePaths(root).meta / "dq_report.json")
    return r.report


__all__ = ["COMPLETE", "FAIL", "PASS", "SKIPPED", "WARN", "CheckResult", "DQReport", "run_checks"]
