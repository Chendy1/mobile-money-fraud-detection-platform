"""Reference constants for the simulation (approximate, synthetic)."""

from __future__ import annotations

import numpy as np

# name, latitude, longitude (approximate centroids), population weight
COUNTIES = [
    ("Nairobi", -1.29, 36.82, 0.30),
    ("Mombasa", -4.04, 39.67, 0.10),
    ("Kisumu", -0.09, 34.77, 0.07),
    ("Nakuru", -0.30, 36.08, 0.08),
    ("Uasin Gishu", 0.51, 35.27, 0.06),
    ("Kiambu", -1.17, 36.83, 0.10),
    ("Machakos", -1.52, 37.26, 0.06),
    ("Nyeri", -0.42, 36.95, 0.04),
    ("Kakamega", 0.28, 34.75, 0.05),
    ("Garissa", -0.45, 39.65, 0.03),
    ("Meru", 0.05, 37.65, 0.05),
    ("Kisii", -0.68, 34.78, 0.06),
]
COUNTY_NAMES = [c[0] for c in COUNTIES]
COUNTY_P = np.array([c[3] for c in COUNTIES])
COUNTY_P = COUNTY_P / COUNTY_P.sum()

SEGMENTS = ["SALARIED", "SMALL_BUSINESS", "INFORMAL_WORKER", "STUDENT"]
SEGMENT_P = np.array([0.30, 0.20, 0.35, 0.15])
SEGMENT_ACTIVITY = np.array([1.0, 1.8, 0.9, 0.7])
SEGMENT_AMT_MULT = np.array([1.4, 1.8, 0.7, 0.45])
SEGMENT_APP_P = np.array([0.5, 0.4, 0.2, 0.6])
SEGMENT_FEATURE_PHONE_P = np.array([0.10, 0.25, 0.45, 0.15])
SEGMENT_SALARY_BOOST = np.array([0.9, 0.2, 0.1, 0.1])

TXN_TYPES = [
    "SEND_MONEY",
    "PAYBILL",
    "TILL",
    "AGENT_WITHDRAWAL",
    "AGENT_DEPOSIT",
    "AIRTIME",
]
SEND, PAYBILL, TILL, WITHDRAW, DEPOSIT, AIRTIME = range(6)

# rows: segment; columns: transaction type (same order as TXN_TYPES)
SEGMENT_MIX = np.array(
    [
        [0.22, 0.22, 0.20, 0.16, 0.04, 0.16],
        [0.20, 0.12, 0.22, 0.14, 0.22, 0.10],
        [0.30, 0.08, 0.22, 0.20, 0.06, 0.14],
        [0.35, 0.05, 0.18, 0.10, 0.02, 0.30],
    ]
)
TYPE_MEDIAN_KES = np.array([1000, 1500, 400, 2000, 3000, 100], dtype=float)
TYPE_SIGMA = np.array([0.8, 0.7, 0.8, 0.7, 0.7, 0.6])
NIGHT_MULT = np.array([1.0, 0.8, 0.5, 0.3, 0.3, 1.0])
AMOUNT_FLOOR = np.array([1, 10, 5, 50, 50, 5])

CP_NONE, CP_CUSTOMER, CP_MERCHANT, CP_AGENT = 0, 1, 2, 3
CP_NAMES = {CP_CUSTOMER: "CUSTOMER", CP_MERCHANT: "MERCHANT", CP_AGENT: "AGENT"}
TYPE_CP = np.array([CP_CUSTOMER, CP_MERCHANT, CP_MERCHANT, CP_AGENT, CP_AGENT, CP_NONE])

TILL_CATEGORIES = [
    "GROCERY",
    "FUEL",
    "RESTAURANT",
    "RETAIL",
    "PHARMACY",
    "TRANSPORT",
    "ELECTRONICS",
]
PAYBILL_CATEGORIES = [
    "ELECTRICITY",
    "WATER",
    "TV_SUBSCRIPTION",
    "SCHOOL_FEES",
    "INSURANCE",
    "RENT",
    "SACCO_LOAN",
]

_BASE_HOUR = np.array(
    [0.15] * 5
    + [
        0.5,
        1.5,
        3.5,
        4.5,
        4.0,
        3.5,
        3.5,
        4.5,
        4.5,
        3.5,
        3.5,
        3.5,
        4.5,
        5.0,
        4.5,
        3.5,
        2.0,
        1.0,
        0.5,
    ]
)
NIGHT_HOURS = (0, 5)  # hours 0..4 are "odd hours"


def hour_probs(ttype: int) -> np.ndarray:
    w = _BASE_HOUR.copy()
    late = np.r_[np.arange(0, 5), np.arange(22, 24)]
    w[late] *= NIGHT_MULT[ttype]
    return w / w.sum()


HOUR_P = [hour_probs(t) for t in range(6)]

CONTACT_K = 10

PATTERNS = [
    "SIM_SWAP",
    "SOCIAL_ENGINEERING",
    "VELOCITY_BURST",
    "BEHAVIORAL_SHIFT",
    "STRUCTURING",
    "AMOUNT_ANOMALY",
    "LOCATION_JUMP",
    "MULE_CASHOUT",
]
EPISODE_P = np.array([0.18, 0.25, 0.12, 0.12, 0.08, 0.12, 0.13])


def haversine_km(lat1, lon1, lat2, lon2):
    p = np.pi / 180
    a = (
        np.sin((lat2 - lat1) * p / 2) ** 2
        + np.cos(lat1 * p) * np.cos(lat2 * p) * np.sin((lon2 - lon1) * p / 2) ** 2
    )
    return 12742 * np.arcsin(np.sqrt(a))


_lat = np.array([c[1] for c in COUNTIES])
_lon = np.array([c[2] for c in COUNTIES])
COUNTY_DIST_KM = haversine_km(_lat[:, None], _lon[:, None], _lat[None, :], _lon[None, :])
FAR_COUNTIES = [np.flatnonzero(COUNTY_DIST_KM[i] > 250) for i in range(len(COUNTIES))]
