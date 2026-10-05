"""Legitimate transaction stream, including legitimate-but-unusual behaviour."""

from __future__ import annotations

import numpy as np

from mmrisk.generation.config import MAX_TXN_KES, GeneratorConfig
from mmrisk.generation.entities import Entities
from mmrisk.generation.reference import (
    AMOUNT_FLOOR,
    CONTACT_K,
    COUNTIES,
    COUNTY_P,
    HOUR_P,
    PAYBILL,
    SEGMENT_APP_P,
    SEGMENT_MIX,
    SEGMENT_SALARY_BOOST,
    SEND,
    TILL,
    TYPE_CP,
    TYPE_MEDIAN_KES,
    TYPE_SIGMA,
    WITHDRAW,
)

FIELDS = (
    "cust",
    "t",
    "ttype",
    "amount",
    "cp_type",
    "cp_idx",
    "device",
    "county",
    "channel",
)


def finalize_amount(raw: np.ndarray, rng: np.random.Generator, floor) -> np.ndarray:
    """Round like real wallets do (many round figures) and clip to [floor, MAX_TXN_KES]."""
    amt = np.maximum(raw, floor)
    round50 = np.round(amt / 50) * 50
    use = (amt >= 500) & (rng.random(len(amt)) < 0.6)
    amt = np.where(use, round50, np.round(amt))
    return np.clip(amt, floor, MAX_TXN_KES).astype(np.int64)


def day_weights(cfg: GeneratorConfig) -> list[np.ndarray]:
    days = np.arange(
        np.datetime64(cfg.start_date), np.datetime64(cfg.end_date) + np.timedelta64(1, "D")
    )
    dow = (days.astype("int64") + 3) % 7  # Monday = 0
    month_start = days.astype("datetime64[M]").astype("datetime64[D]")
    dom = (days - month_start).astype("int64") + 1
    salary = (dom >= 25) | (dom <= 3)
    dow_w = np.array([1.0, 1.0, 1.0, 1.0, 1.15, 1.1, 0.9])
    out = []
    for boost in SEGMENT_SALARY_BOOST:
        w = dow_w[dow] * (1 + boost * salary)
        out.append(w / w.sum())
    return out


def _slot_probs(k: int) -> np.ndarray:
    w = 1.0 / (np.arange(k) + 1) ** 1.2
    return w / w.sum()


def _pick_contacts(ents: Entities, rng, cust, p_new) -> np.ndarray:
    m = len(cust)
    slot = rng.choice(CONTACT_K, size=m, p=_slot_probs(CONTACT_K))
    nc = ents.n_contacts[cust]
    slot = np.where(slot >= nc, slot % nc, slot)
    idx = ents.contacts[cust, slot]
    rnd = rng.integers(0, ents.n, m)
    rnd = np.where(rnd == cust, (rnd + 1) % ents.n, rnd)
    return np.where(rng.random(m) < p_new, rnd, idx)


def _pick_favs(favs, pool, cust, rng, p_fav) -> np.ndarray:
    m = len(cust)
    slot = rng.choice(favs.shape[1], size=m, p=_slot_probs(favs.shape[1]))
    rnd = pool[rng.integers(0, len(pool), m)]
    return np.where(rng.random(m) < p_fav, favs[cust, slot], rnd)


def assign_devices(ents: Entities, rng, cust, t) -> np.ndarray:
    dev = cust.astype(np.int64)
    sec = ents.second_dev[cust]
    use_sec = (sec >= 0) & (rng.random(len(cust)) < 0.25)
    dev = np.where(use_sec, sec, dev)
    sw = ents.switch_dev[cust]
    switched = (sw >= 0) & (t >= ents.switch_t[cust]) & ~use_sec
    return np.where(switched, sw, dev)


def assign_counties(ents: Entities, rng, cust, t) -> np.ndarray:
    county = ents.home[cust]
    in_trip = (
        (ents.trip_start[cust] >= 0) & (t >= ents.trip_start[cust]) & (t < ents.trip_end[cust])
    )
    county = np.where(in_trip, ents.trip_county[cust], county)
    noise = rng.random(len(cust)) < 0.03
    return np.where(noise, rng.choice(len(COUNTIES), len(cust), p=COUNTY_P), county)


def build_rows(ents: Entities, rng, cust, t, ttype, amount, p_new=0.07) -> dict:
    m = len(cust)
    cp_type = TYPE_CP[ttype].astype(np.int64)
    cp_idx = np.full(m, -1, dtype=np.int64)
    for code, fn in (
        (SEND, lambda c: _pick_contacts(ents, rng, c, p_new)),
        (
            PAYBILL,
            lambda c: _pick_favs(ents.fav_paybill, ents.pool_paybill, c, rng, 0.85),
        ),
        (TILL, lambda c: _pick_favs(ents.fav_till, ents.pool_till, c, rng, 0.80)),
    ):
        mask = ttype == code
        if mask.any():
            cp_idx[mask] = fn(cust[mask])
    agent = (ttype == WITHDRAW) | (ttype == 4)
    if agent.any():
        pool = np.arange(len(ents.a_county))
        cp_idx[agent] = _pick_favs(ents.fav_agent, pool, cust[agent], rng, 0.85)
    return {
        "cust": cust.astype(np.int64),
        "t": t.astype(np.int64),
        "ttype": ttype.astype(np.int64),
        "amount": amount.astype(np.int64),
        "cp_type": cp_type,
        "cp_idx": cp_idx,
        "device": assign_devices(ents, rng, cust, t),
        "county": assign_counties(ents, rng, cust, t),
        "channel": (rng.random(m) < SEGMENT_APP_P[ents.segment[cust]]).astype(np.int64),
    }


def _expand(rng, ep_cust, ep_t0, ep_n, gap_lo, gap_hi, horizon):
    total = int(ep_n.sum())
    cust = np.repeat(ep_cust, ep_n)
    t0 = np.repeat(ep_t0, ep_n)
    gaps = rng.integers(gap_lo, gap_hi, total)
    cs = np.cumsum(gaps) - gaps
    start_idx = np.repeat(np.cumsum(ep_n) - ep_n, ep_n)
    offset = cs - cs[start_idx]
    return cust, np.minimum(t0 + offset, horizon - 1)


def _base_rows(cfg, ents, rng, n_rows, day_w):
    horizon = cfg.num_days * 86400
    w = ents.activity / ents.activity.sum()
    cust = np.repeat(np.arange(ents.n), rng.multinomial(n_rows, w))
    seg = ents.segment[cust]
    m = len(cust)
    ttype = np.empty(m, dtype=np.int64)
    day = np.empty(m, dtype=np.int64)
    for s in range(len(SEGMENT_MIX)):
        mask = seg == s
        k = int(mask.sum())
        ttype[mask] = rng.choice(6, k, p=SEGMENT_MIX[s])
        day[mask] = rng.choice(cfg.num_days, k, p=day_w[s])
    hour = np.empty(m, dtype=np.int64)
    for tt in range(6):
        mask = ttype == tt
        hour[mask] = rng.choice(24, int(mask.sum()), p=HOUR_P[tt])
    t = day * 86400 + hour * 3600 + rng.integers(0, 3600, m)
    raw = TYPE_MEDIAN_KES[ttype] * ents.scale[cust] * np.exp(rng.normal(0, TYPE_SIGMA[ttype]))
    amount = finalize_amount(raw, rng, AMOUNT_FLOOR[ttype])
    base = build_rows(ents, rng, cust, t, ttype, amount)

    # legitimate chain: recipient cashes out part of a transfer shortly after receiving it
    chain = (ttype == SEND) & (amount >= 200) & (rng.random(m) < 0.12)
    parts = [base]
    if chain.any():
        rc = base["cp_idx"][chain]
        tc = np.minimum(t[chain] + rng.integers(300, 12 * 3600, chain.sum()), horizon - 1)
        ac = finalize_amount(amount[chain] * rng.uniform(0.6, 1.0, chain.sum()), rng, 50)
        parts.append(build_rows(ents, rng, rc, tc, np.full(len(rc), WITHDRAW), ac))

    # emergencies: unusually large transfers, 25% at odd hours, 40% to new recipients
    n_ep = max(1, int(round(0.006 * n_rows / 2)))
    ec = rng.choice(ents.n, n_ep, p=w)
    en = rng.integers(1, 4, n_ep)
    night = rng.random(n_ep) < 0.25
    eh = np.where(night, rng.integers(0, 5, n_ep), rng.choice(24, n_ep, p=HOUR_P[SEND]))
    et0 = rng.integers(0, cfg.num_days, n_ep) * 86400 + eh * 3600 + rng.integers(0, 3600, n_ep)
    c_e, t_e = _expand(rng, ec, et0, en, 60, 1800, horizon)
    tt_e = np.where(rng.random(len(c_e)) < 0.85, SEND, WITHDRAW)
    am_e = finalize_amount(ents.typ[c_e] * rng.uniform(3, 12, len(c_e)), rng, 50)
    parts.append(build_rows(ents, rng, c_e, t_e, tt_e, am_e, p_new=0.4))

    # bulk payments by small businesses (rapid, many recipients, legitimate)
    biz = np.flatnonzero(ents.segment == 1)
    n_bulk = max(1, int(round(0.008 * n_rows / 9)))
    bc = biz[rng.integers(0, len(biz), n_bulk)]
    bn = rng.integers(5, 16, n_bulk)
    bd = rng.choice(cfg.num_days, n_bulk, p=day_w[1])
    bt0 = bd * 86400 + rng.integers(8, 17, n_bulk) * 3600 + rng.integers(0, 3600, n_bulk)
    c_b, t_b = _expand(rng, bc, bt0, bn, 60, 400, horizon)
    tt_b = rng.choice([SEND, PAYBILL, TILL], len(c_b), p=[0.5, 0.3, 0.2])
    am_b = finalize_amount(
        TYPE_MEDIAN_KES[tt_b] * ents.scale[c_b] * rng.uniform(0.7, 3.0, len(c_b)),
        rng,
        AMOUNT_FLOOR[tt_b],
    )
    parts.append(build_rows(ents, rng, c_b, t_b, tt_b, am_b, p_new=0.5))
    return {k: np.concatenate([p[k] for p in parts]) for k in FIELDS}


def generate_legit(cfg: GeneratorConfig, ents: Entities, rng, target: int) -> dict:
    day_w = day_weights(cfg)
    parts, total = [], 0
    while total < target:
        batch = _base_rows(cfg, ents, rng, max(1, int(np.ceil((target - total) / 1.045))), day_w)
        parts.append(batch)
        total += len(batch["cust"])
    rows = {k: np.concatenate([p[k] for p in parts]) for k in FIELDS}
    if total > target:
        keep = np.sort(rng.choice(total, target, replace=False))
        rows = {k: v[keep] for k, v in rows.items()}
    return rows
