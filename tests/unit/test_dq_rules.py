"""Unit tests: each domain rule fires on a violating row and stays silent on a valid one."""

from datetime import date, datetime

import polars as pl
import pytest

from mmrisk.generation import reference as gen_ref
from mmrisk.generation.config import MAX_TXN_KES
from mmrisk.ingestion.specs import SPECS
from mmrisk.validation import config as cfg
from mmrisk.validation.rules import RULES, RuleContext, apply_rules

AS_OF = datetime(2025, 7, 1)

BASE = {
    "transactions": {
        "transaction_id": "TXN000000001",
        "customer_id": "C000001",
        "timestamp": datetime(2025, 1, 1, 10, 0),
        "transaction_type": "SEND_MONEY",
        "amount_kes": 1000,
        "counterparty_type": "CUSTOMER",
        "counterparty_id": "C000002",
        "device_id": "D000001",
        "location_county": "Nairobi",
        "channel": "APP",
    },
    "labels": {
        "transaction_id": "TXN000000001",
        "label_is_fraud": 0,
        "label_available_at": datetime(2025, 2, 1),
    },
    "customers": {
        "customer_id": "C000001",
        "segment": "SALARIED",
        "home_county": "Nairobi",
        "registration_date": date(2020, 1, 1),
    },
    "merchants": {
        "merchant_id": "M00001",
        "merchant_type": "TILL",
        "category": "GROCERY",
        "county": "Nairobi",
    },
    "agents": {"agent_id": "A0001", "county": "Nairobi"},
    "devices": {"device_id": "D000001", "device_type": "SMARTPHONE"},
    "counties": {"county": "Nairobi", "latitude": -1.29, "longitude": 36.82},
}


def reason_for(table: str, **overrides) -> str | None:
    row = {**BASE[table], **overrides}
    df = pl.DataFrame([row], schema=SPECS[table].schema).with_row_index("_row_number")
    good, rejects = apply_rules(df, table, SPECS[table].columns, RuleContext(as_of=AS_OF))
    assert good.height + rejects.height == 1
    return rejects["_reason"][0] if rejects.height else None


@pytest.mark.parametrize("table", sorted(BASE))
def test_a_valid_row_passes_every_rule(table):
    assert reason_for(table) is None


CASES = [
    ("transactions", {"amount_kes": 0}, "amount_positive"),
    ("transactions", {"amount_kes": -5}, "amount_positive"),
    ("transactions", {"amount_kes": cfg.MAX_AMOUNT_KES + 1}, "amount_within_cap"),
    ("transactions", {"amount_kes": cfg.MAX_AMOUNT_KES}, None),
    ("transactions", {"timestamp": datetime(1999, 12, 31)}, "timestamp_plausible"),
    ("transactions", {"timestamp": datetime(2025, 7, 2, 0, 0, 1)}, "timestamp_not_future"),
    ("transactions", {"timestamp": datetime(2025, 7, 2, 0, 0, 0)}, None),  # inside tolerance
    ("transactions", {"transaction_type": "WIRE_TRANSFER"}, "allowed_transaction_type"),
    ("transactions", {"channel": "SMS"}, "allowed_channel"),
    ("transactions", {"transaction_id": "T1"}, "transaction_id_format"),
    ("transactions", {"customer_id": "cust-1"}, "customer_id_format"),
    ("transactions", {"device_id": "dev"}, "device_id_format"),
    (
        "transactions",
        {"transaction_type": "AIRTIME", "counterparty_type": None, "counterparty_id": None},
        None,
    ),
    ("transactions", {"transaction_type": "AIRTIME"}, "counterparty_consistent"),
    (
        "transactions",
        {"counterparty_type": None, "counterparty_id": None},
        "counterparty_consistent",
    ),
    ("transactions", {"counterparty_type": "MERCHANT"}, "counterparty_consistent"),
    (
        "transactions",
        {
            "transaction_type": "PAYBILL",
            "counterparty_type": "MERCHANT",
            "counterparty_id": "M00001",
        },
        None,
    ),
    (
        "transactions",
        {
            "transaction_type": "AGENT_WITHDRAWAL",
            "counterparty_type": "AGENT",
            "counterparty_id": "A0001",
        },
        None,
    ),
    ("transactions", {"counterparty_id": "X12"}, "counterparty_id_format"),
    ("transactions", {"counterparty_id": "C000001"}, "no_self_transfer"),
    ("labels", {"label_is_fraud": 2}, "label_value_valid"),
    ("labels", {"label_available_at": datetime(2026, 1, 1)}, "timestamp_not_future"),
    ("labels", {"label_available_at": datetime(1970, 1, 1)}, "timestamp_plausible"),
    ("labels", {"transaction_id": "bad"}, "transaction_id_format"),
    ("customers", {"segment": "VIP"}, "allowed_segment"),
    ("customers", {"registration_date": date(2026, 1, 1)}, "registration_not_future"),
    ("customers", {"registration_date": date(1990, 1, 1)}, "registration_plausible"),
    ("customers", {"customer_id": "1"}, "customer_id_format"),
    ("merchants", {"merchant_type": "KIOSK"}, "allowed_merchant_type"),
    ("merchants", {"merchant_id": "X"}, "merchant_id_format"),
    ("agents", {"agent_id": "A"}, "agent_id_format"),
    ("devices", {"device_type": "TABLET"}, "allowed_device_type"),
    ("devices", {"device_id": "D1"}, "device_id_format"),
    ("counties", {"latitude": 50.0}, "latitude_in_range"),
    ("counties", {"longitude": 10.0}, "longitude_in_range"),
]


@pytest.mark.parametrize(("table", "overrides", "rule"), CASES)
def test_rule_behaviour(table, overrides, rule):
    expected = f"RULE:{rule}" if rule else None
    assert reason_for(table, **overrides) == expected


def test_every_rule_is_exercised_by_a_case():
    covered = {(t, r) for t, _, r in CASES if r}
    defined = {(t, rule.name) for t, rules in RULES.items() for rule in rules}
    assert defined <= covered, f"rules without a test: {sorted(defined - covered)}"


# ---- guards against configuration drifting away from the generator ------------------------------


def test_config_matches_generator_reference():
    assert cfg.MAX_AMOUNT_KES == MAX_TXN_KES
    assert set(cfg.TRANSACTION_TYPES) == set(gen_ref.TXN_TYPES)
    assert set(cfg.SEGMENTS) == set(gen_ref.SEGMENTS)
    assert set(cfg.CHANNELS) == {"USSD", "APP"}
    expected = {
        gen_ref.TXN_TYPES[i]: gen_ref.CP_NAMES[cp]
        for i, cp in enumerate(gen_ref.TYPE_CP)
        if cp in gen_ref.CP_NAMES
    }
    assert cfg.COUNTERPARTY_FOR_TYPE == expected
    lat = [c[1] for c in gen_ref.COUNTIES]
    lon = [c[2] for c in gen_ref.COUNTIES]
    assert cfg.KENYA_LAT[0] <= min(lat) and max(lat) <= cfg.KENYA_LAT[1]
    assert cfg.KENYA_LON[0] <= min(lon) and max(lon) <= cfg.KENYA_LON[1]
