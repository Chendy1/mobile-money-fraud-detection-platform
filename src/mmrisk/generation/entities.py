"""Customers, merchants, agents, devices and their hidden latent attributes."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from mmrisk.generation.config import MULE_SHARE, GeneratorConfig
from mmrisk.generation.reference import (
    CONTACT_K,
    COUNTIES,
    COUNTY_P,
    SEGMENT_ACTIVITY,
    SEGMENT_AMT_MULT,
    SEGMENT_FEATURE_PHONE_P,
    SEGMENT_P,
)


@dataclass
class Entities:
    n: int
    segment: np.ndarray
    home: np.ndarray
    scale: np.ndarray  # customer amount scale (segment multiplier included)
    typ: np.ndarray  # typical send amount in KES
    activity: np.ndarray
    reg_days_before: np.ndarray
    contacts: np.ndarray
    n_contacts: np.ndarray
    fav_paybill: np.ndarray
    fav_till: np.ndarray
    fav_agent: np.ndarray
    pool_paybill: np.ndarray
    pool_till: np.ndarray
    m_type: np.ndarray  # 0 PAYBILL, 1 TILL
    m_cat: np.ndarray
    m_county: np.ndarray
    a_county: np.ndarray
    second_dev: np.ndarray
    switch_dev: np.ndarray
    switch_t: np.ndarray
    trip_start: np.ndarray
    trip_end: np.ndarray
    trip_county: np.ndarray
    device_is_feature: np.ndarray
    is_mule: np.ndarray
    vulnerability: np.ndarray


def build_entities(cfg: GeneratorConfig, rng: np.random.Generator) -> Entities:
    n, d = cfg.num_customers, cfg.num_days
    n_counties = len(COUNTIES)
    seg = rng.choice(len(SEGMENT_P), n, p=SEGMENT_P)
    home = rng.choice(n_counties, n, p=COUNTY_P)
    scale = SEGMENT_AMT_MULT[seg] * np.exp(rng.normal(0, 0.45, n))
    activity = SEGMENT_ACTIVITY[seg] * np.exp(rng.normal(0, 0.6, n))
    reg_days_before = rng.integers(30, 1500, n)

    # contacts (P2P counterparties): 60% from the same county
    n_contacts = rng.integers(3, CONTACT_K + 1, n)
    contacts = rng.integers(0, n, (n, CONTACT_K))
    for c in range(n_counties):
        members = np.flatnonzero(home == c)
        if len(members) < 2:
            continue
        same = rng.random((len(members), CONTACT_K)) < 0.6
        pick = members[rng.integers(0, len(members), (len(members), CONTACT_K))]
        contacts[members] = np.where(same, pick, contacts[members])
    me = contacts == np.arange(n)[:, None]
    contacts[me] = (contacts[me] + 1) % n

    # merchants and agents
    n_m = max(300, n // 8)
    m_type = (rng.random(n_m) >= 0.35).astype(np.int64)  # 0 PAYBILL (35%), 1 TILL
    m_cat = rng.integers(0, 7, n_m)
    m_county = rng.choice(n_counties, n_m, p=COUNTY_P)
    pool_pb, pool_tl = np.flatnonzero(m_type == 0), np.flatnonzero(m_type == 1)
    fav_pb = pool_pb[rng.integers(0, len(pool_pb), (n, 4))]
    fav_tl = pool_tl[rng.integers(0, len(pool_tl), (n, 5))]
    n_a = max(100, n // 30)
    a_county = rng.choice(n_counties, n_a, p=COUNTY_P)
    fav_agent = np.zeros((n, 2), dtype=np.int64)
    for c in range(n_counties):
        members = np.flatnonzero(home == c)
        local = np.flatnonzero(a_county == c)
        if len(local) == 0:
            local = np.arange(n_a)
        if len(members):
            fav_agent[members] = local[rng.integers(0, len(local), (len(members), 2))]

    # devices: primary id == customer index; then second devices; then replacement phones
    has2 = rng.random(n) < 0.20
    second_dev = np.full(n, -1, dtype=np.int64)
    second_dev[has2] = n + np.arange(has2.sum())
    sw = rng.random(n) < 0.07
    switch_dev = np.full(n, -1, dtype=np.int64)
    switch_dev[sw] = n + has2.sum() + np.arange(sw.sum())
    switch_t = rng.integers(7 * 86400, (d - 1) * 86400, n)
    feat = np.r_[
        rng.random(n) < SEGMENT_FEATURE_PHONE_P[seg],
        rng.random(has2.sum()) < 0.10,
        rng.random(sw.sum()) < 0.20,
    ]

    # trips (legitimate location changes)
    trip = rng.random(n) < 0.08
    trip_start = np.full(n, -1, dtype=np.int64)
    trip_end = np.full(n, -1, dtype=np.int64)
    trip_start[trip] = rng.integers(0, max(1, (d - 9)) * 86400, trip.sum())
    trip_end[trip] = trip_start[trip] + rng.integers(2, 9, trip.sum()) * 86400
    trip_county = rng.integers(0, n_counties, n)
    trip_county = np.where(trip_county == home, (trip_county + 1) % n_counties, trip_county)

    # hidden latent attributes (never exposed in observable tables)
    n_mules = max(3, int(round(MULE_SHARE * n)))
    is_mule = np.zeros(n, dtype=bool)
    is_mule[rng.choice(n, n_mules, replace=False)] = True
    vulnerability = np.exp(rng.normal(0, 0.8, n))
    vulnerability[is_mule] = 0.0

    return Entities(
        n=n,
        segment=seg,
        home=home,
        scale=scale,
        typ=1000.0 * scale,
        activity=activity,
        reg_days_before=reg_days_before,
        contacts=contacts,
        n_contacts=n_contacts,
        fav_paybill=fav_pb,
        fav_till=fav_tl,
        fav_agent=fav_agent,
        pool_paybill=pool_pb,
        pool_till=pool_tl,
        m_type=m_type,
        m_cat=m_cat,
        m_county=m_county,
        a_county=a_county,
        second_dev=second_dev,
        switch_dev=switch_dev,
        switch_t=switch_t,
        trip_start=trip_start,
        trip_end=trip_end,
        trip_county=trip_county,
        device_is_feature=feat,
        is_mule=is_mule,
        vulnerability=vulnerability,
    )
