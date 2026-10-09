"""Row-level domain rules. A row that violates a rule is quarantined as `RULE:<name>`.

Rules run on typed, standardised rows after the structural gates. The first matching rule
(in list order) names the reason. Dataset-level checks live in `checks.py`.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

import polars as pl

from mmrisk.validation import config as cfg


@dataclass(frozen=True)
class RuleContext:
    as_of: datetime  # "now" for the future-timestamp rules (the ingestion time)


@dataclass(frozen=True)
class Rule:
    name: str
    bad: Callable[[RuleContext], pl.Expr]  # True where the row violates the rule


def _fmt(col: str, pattern: str) -> Callable[[RuleContext], pl.Expr]:
    return lambda ctx: ~pl.col(col).str.contains(pattern)


def _in(col: str, allowed) -> Callable[[RuleContext], pl.Expr]:
    return lambda ctx: ~pl.col(col).is_in(list(allowed))


def _not_future(col: str) -> Callable[[RuleContext], pl.Expr]:
    return lambda ctx: pl.col(col) > pl.lit(ctx.as_of + cfg.FUTURE_TOLERANCE)


def _implausible(col: str) -> Callable[[RuleContext], pl.Expr]:
    return lambda ctx: pl.col(col) < pl.lit(cfg.PLAUSIBLE_AFTER)


def _counterparty_consistent(ctx: RuleContext) -> pl.Expr:
    t = pl.col("transaction_type")
    cp_t, cp_id = pl.col("counterparty_type"), pl.col("counterparty_id")
    expected = (
        pl.when(t == "SEND_MONEY")
        .then(pl.lit("CUSTOMER"))
        .when(t.is_in(["PAYBILL", "TILL"]))
        .then(pl.lit("MERCHANT"))
        .when(t.is_in(["AGENT_WITHDRAWAL", "AGENT_DEPOSIT"]))
        .then(pl.lit("AGENT"))
        .otherwise(pl.lit(None, dtype=pl.Utf8))
    )
    airtime = t == "AIRTIME"
    bad_airtime = airtime & (cp_t.is_not_null() | cp_id.is_not_null())
    bad_other = ~airtime & (cp_t.is_null() | cp_id.is_null() | (cp_t != expected))
    return (bad_airtime | bad_other).fill_null(False)


def _counterparty_id_format(ctx: RuleContext) -> pl.Expr:
    cid, ct = pl.col("counterparty_id"), pl.col("counterparty_type")
    bad = (
        ((ct == "CUSTOMER") & ~cid.str.contains(r"^C\d{6,}$"))
        | ((ct == "MERCHANT") & ~cid.str.contains(r"^M\d{5,}$"))
        | ((ct == "AGENT") & ~cid.str.contains(r"^A\d{4,}$"))
    )
    return bad.fill_null(False)


RULES: dict[str, list[Rule]] = {
    "transactions": [
        Rule("transaction_id_format", _fmt("transaction_id", r"^TXN\d{9,}$")),
        Rule("customer_id_format", _fmt("customer_id", r"^C\d{6,}$")),
        Rule("device_id_format", _fmt("device_id", r"^D\d{6,}$")),
        Rule("amount_positive", lambda ctx: pl.col("amount_kes") <= 0),
        Rule("amount_within_cap", lambda ctx: pl.col("amount_kes") > cfg.MAX_AMOUNT_KES),
        Rule("timestamp_plausible", _implausible("timestamp")),
        Rule("timestamp_not_future", _not_future("timestamp")),
        Rule("allowed_transaction_type", _in("transaction_type", cfg.TRANSACTION_TYPES)),
        Rule("allowed_channel", _in("channel", cfg.CHANNELS)),
        Rule("counterparty_consistent", _counterparty_consistent),
        Rule("counterparty_id_format", _counterparty_id_format),
        Rule("no_self_transfer", lambda ctx: pl.col("customer_id") == pl.col("counterparty_id")),
    ],
    "labels": [
        Rule("transaction_id_format", _fmt("transaction_id", r"^TXN\d{9,}$")),
        Rule("label_value_valid", _in("label_is_fraud", (0, 1))),
        Rule("timestamp_plausible", _implausible("label_available_at")),
        Rule("timestamp_not_future", _not_future("label_available_at")),
    ],
    "customers": [
        Rule("customer_id_format", _fmt("customer_id", r"^C\d{6,}$")),
        Rule("allowed_segment", _in("segment", cfg.SEGMENTS)),
        Rule(
            "registration_plausible",
            lambda ctx: pl.col("registration_date") < pl.lit(cfg.PLAUSIBLE_AFTER.date()),
        ),
        Rule(
            "registration_not_future",
            lambda ctx: pl.col("registration_date") > pl.lit(ctx.as_of.date()),
        ),
    ],
    "merchants": [
        Rule("merchant_id_format", _fmt("merchant_id", r"^M\d{5,}$")),
        Rule("allowed_merchant_type", _in("merchant_type", cfg.MERCHANT_TYPES)),
    ],
    "agents": [Rule("agent_id_format", _fmt("agent_id", r"^A\d{4,}$"))],
    "devices": [
        Rule("device_id_format", _fmt("device_id", r"^D\d{6,}$")),
        Rule("allowed_device_type", _in("device_type", cfg.DEVICE_TYPES)),
    ],
    "counties": [
        Rule(
            "latitude_in_range",
            lambda ctx: ~pl.col("latitude").is_between(*cfg.KENYA_LAT),
        ),
        Rule(
            "longitude_in_range",
            lambda ctx: ~pl.col("longitude").is_between(*cfg.KENYA_LON),
        ),
    ],
}


def apply_rules(
    df: pl.DataFrame, table: str, columns: list[str], ctx: RuleContext
) -> tuple[pl.DataFrame, pl.DataFrame]:
    """Split `df` (must hold `_row_number`) into (good rows, rejects with reason and row JSON)."""
    reason = pl.lit(None, dtype=pl.Utf8)
    for rule in reversed(RULES[table]):  # first matching rule wins
        reason = (
            pl.when(rule.bad(ctx).fill_null(False))
            .then(pl.lit(f"RULE:{rule.name}"))
            .otherwise(reason)
        )
    w = df.with_columns(reason.alias("_reason"))
    good = w.filter(pl.col("_reason").is_null()).drop("_reason")
    rejects = w.filter(pl.col("_reason").is_not_null()).select(
        "_row_number",
        "_reason",
        pl.struct(columns).struct.json_encode().alias("_row_json"),
    )
    return good, rejects
