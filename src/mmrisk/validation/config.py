"""Data-quality configuration: allowed values, assumptions and thresholds."""

from __future__ import annotations

from datetime import datetime, timedelta

# ---- assumptions (synthetic; NOT verified real-world rules) -----------------------------------
MAX_AMOUNT_KES = 250_000  # ASSUMPTION (TO VERIFY); must equal generation.config.MAX_TXN_KES
PLAUSIBLE_AFTER = datetime(2000, 1, 1)  # timestamps before this are treated as corrupt
FUTURE_TOLERANCE = timedelta(days=1)  # clock skew and EAT vs UTC
KENYA_LAT = (-5.0, 5.5)  # ASSUMPTION: generous approximate bounding box
KENYA_LON = (33.0, 42.5)

# ---- allowed values ---------------------------------------------------------------------------
TRANSACTION_TYPES = (
    "SEND_MONEY",
    "PAYBILL",
    "TILL",
    "AGENT_WITHDRAWAL",
    "AGENT_DEPOSIT",
    "AIRTIME",
)
CHANNELS = ("USSD", "APP")
SEGMENTS = ("SALARIED", "SMALL_BUSINESS", "INFORMAL_WORKER", "STUDENT")
MERCHANT_TYPES = ("PAYBILL", "TILL")
DEVICE_TYPES = ("SMARTPHONE", "FEATURE_PHONE")
# AIRTIME has no counterparty
COUNTERPARTY_FOR_TYPE = {
    "SEND_MONEY": "CUSTOMER",
    "PAYBILL": "MERCHANT",
    "TILL": "MERCHANT",
    "AGENT_WITHDRAWAL": "AGENT",
    "AGENT_DEPOSIT": "AGENT",
}

# ---- dataset-level thresholds -----------------------------------------------------------------
NULL_RATE_MAX = {"counterparty_id": 0.25, "counterparty_type": 0.25}  # default is 0.0
QUARANTINE_WARN, QUARANTINE_FAIL = 0.01, 0.05  # share of incoming rows quarantined
DUPLICATE_WARN = 0.05  # share of incoming rows that were identical duplicates
CONFLICT_FAIL = 0.01  # share of incoming rows that conflicted with earlier data
ORPHAN_WARN_MAX = 0.001  # orphan rate above 0 and up to this is WARN; above is FAIL
VOLUME_LOW_RATIO, VOLUME_HIGH_RATIO = 0.25, 4.0  # of the median daily volume
VOLUME_MIN_MEDIAN = 20  # below this the volume check is skipped (too little data)
DEFAULT_MAX_LAG_HOURS = 24.0
