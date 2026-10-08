"""Structural validation gates (Phase 3). Domain quality rules are added in Phase 4.

Gates: the schema matches; every value casts to its declared type; required fields are
present and non-empty. Failing rows are quarantined with a reason, never dropped silently.
"""

from __future__ import annotations

from dataclasses import dataclass

import polars as pl

from mmrisk.ingestion.specs import TableSpec


@dataclass
class Validation:
    valid: pl.DataFrame  # typed business columns + _row_number
    rejects: pl.DataFrame  # _row_number, _reason, _row_json


def schema_problems(df: pl.DataFrame, spec: TableSpec) -> list[str]:
    missing = [c for c in spec.schema if c not in df.columns]
    extra = [c for c in df.columns if c not in spec.schema]
    problems = []
    if missing:
        problems.append("MISSING_COLUMNS:" + ",".join(missing))
    if extra:
        problems.append("UNEXPECTED_COLUMNS:" + ",".join(extra))
    return problems


def _cast(col: str, src: pl.DataType, dtype: pl.DataType) -> pl.Expr:
    """Cast without raising; text is parsed explicitly (text casts to temporal are deprecated)."""
    e = pl.col(col)
    if src == pl.Utf8 and isinstance(dtype, pl.Datetime):
        return e.str.to_datetime(time_unit=dtype.time_unit, strict=False)
    if src == pl.Utf8 and dtype == pl.Date:
        return e.str.to_date(strict=False)
    return e.cast(dtype, strict=False)


def validate_frame(df: pl.DataFrame, spec: TableSpec) -> Validation:
    w = df.with_row_index("_row_number")
    w = w.with_columns(
        [_cast(c, df.schema[c], dt).alias(f"__c_{c}") for c, dt in spec.schema.items()]
    )

    gates: list[tuple[pl.Expr, str]] = []
    for col, dtype in spec.schema.items():
        cast_failed = pl.col(col).is_not_null() & pl.col(f"__c_{col}").is_null()
        src = df.schema[col]
        if src.is_float() and dtype.is_integer():  # 100.5 must not be silently truncated
            cast_failed = cast_failed | (
                pl.col(col).is_not_null() & (pl.col(col) != pl.col(f"__c_{col}").cast(pl.Float64))
            )
        gates.append((cast_failed.fill_null(False), f"CAST_FAILED:{col}"))
    for col in spec.required:
        gates.append((pl.col(f"__c_{col}").is_null(), f"NULL_REQUIRED:{col}"))
        if spec.schema[col] == pl.Utf8:
            blank = pl.col(f"__c_{col}").str.strip_chars().str.len_chars() == 0
            gates.append((blank.fill_null(False), f"EMPTY_REQUIRED:{col}"))

    reason = pl.lit(None, dtype=pl.Utf8)
    for cond, text in reversed(gates):  # first matching gate wins
        reason = pl.when(cond).then(pl.lit(text)).otherwise(reason)
    w = w.with_columns(reason.alias("_reason"))

    valid = w.filter(pl.col("_reason").is_null()).select(
        "_row_number", *[pl.col(f"__c_{c}").alias(c) for c in spec.schema]
    )
    rejects = w.filter(pl.col("_reason").is_not_null()).select(
        "_row_number",
        "_reason",
        pl.struct(list(spec.schema)).struct.json_encode().alias("_row_json"),
    )
    return Validation(valid=valid, rejects=rejects)
