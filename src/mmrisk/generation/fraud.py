"""Fraud episode injection.

Fraud is generated as multi-transaction *episodes* (e.g. a SIM-swap takeover followed by a
mule cash-out). A hidden per-episode "sophistication" value makes some fraud mimic normal
behaviour, so no single signal separates fraud from legitimate activity.
"""

from __future__ import annotations

import numpy as np

from mmrisk.generation.config import (
    MAX_TXN_KES,
    STRUCTURING_HIGH,
    STRUCTURING_LOW,
    GeneratorConfig,
)
from mmrisk.generation.entities import Entities
from mmrisk.generation.legit import FIELDS, finalize_amount
from mmrisk.generation.reference import (
    AIRTIME,
    CP_AGENT,
    CP_CUSTOMER,
    CP_MERCHANT,
    CP_NONE,
    EPISODE_P,
    FAR_COUNTIES,
    HOUR_P,
    PATTERNS,
    PAYBILL,
    SEND,
    TILL,
    WITHDRAW,
)

MULE_CASHOUT = PATTERNS.index("MULE_CASHOUT")


class DeviceAllocator:
    """Allocates device ids beyond the initial population (attacker and extra devices)."""

    def __init__(self, first_free: int):
        self.next = first_free
        self.extra_is_feature: list[bool] = []
        self.pool: list[int] = []

    def new(self, rng, feature_p: float = 0.1) -> int:
        idx = self.next
        self.next += 1
        self.extra_is_feature.append(bool(rng.random() < feature_p))
        return idx

    def attacker(self, rng) -> int:
        """Attacker devices are sometimes reused across victims (fraud rings)."""
        if len(self.pool) >= 3 and rng.random() < 0.35:
            return int(self.pool[rng.integers(0, len(self.pool))])
        idx = self.new(rng)
        if rng.random() < 0.6:
            self.pool.append(idx)
        return idx


class FraudInjector:
    def __init__(self, cfg: GeneratorConfig, ents: Entities, legit: dict, rng, alloc):
        self.cfg, self.e, self.rng, self.alloc = cfg, ents, rng, alloc
        self.d = cfg.num_days
        self.end_t = self.d * 86400 - 1
        self.vuln_p = ents.vulnerability / ents.vulnerability.sum()
        self.mules = np.flatnonzero(ents.is_mule)
        order = np.lexsort((legit["t"], legit["cust"]))
        self.a_cust = legit["cust"][order]
        self.a_t = legit["t"][order]
        self.ep = 0

    # ---- helpers -----------------------------------------------------------------------
    def _victim(self) -> int:
        return int(self.rng.choice(self.e.n, p=self.vuln_p))

    def _time(self, p_odd: float) -> int:
        r = self.rng
        day = int(r.integers(7, self.d))
        hour = int(r.integers(0, 5)) if r.random() < p_odd else int(r.choice(24, p=HOUR_P[SEND]))
        return day * 86400 + hour * 3600 + int(r.integers(0, 3600))

    def _anchor(self, c: int):
        lo = np.searchsorted(self.a_cust, c, "left")
        hi = np.searchsorted(self.a_cust, c, "right")
        if hi <= lo:
            return None
        return int(self.a_t[self.rng.integers(lo, hi)])

    def _vdev(self, c: int, t: int) -> int:
        sw = self.e.switch_dev[c]
        return int(sw) if sw >= 0 and t >= self.e.switch_t[c] else int(c)

    def _dev(self, c: int, t: int, p_attacker: float) -> int:
        if self.rng.random() < p_attacker:
            return self.alloc.attacker(self.rng)
        return self._vdev(c, t)

    def _county(self, c: int, p_far: float) -> int:
        home = int(self.e.home[c])
        if self.rng.random() < p_far:
            far = FAR_COUNTIES[home]
            return int(far[self.rng.integers(0, len(far))])
        return home

    def _amount(self, c: int, mult: float, s: float, ttype: int) -> int:
        eff = np.exp(np.log(mult) * (1 - 0.6 * s)) * np.exp(self.rng.normal(0, 0.15))
        base = self.e.typ[c] * eff * (0.1 if ttype == AIRTIME else 1.0)
        floor = 10 if ttype == AIRTIME else 50
        return int(finalize_amount(np.array([base]), self.rng, floor)[0])

    def _recipient(self, c: int, p_new: float):
        r = self.rng
        if r.random() < p_new:
            if len(self.mules) and r.random() < 0.85:
                return int(self.mules[r.integers(0, len(self.mules))])
            x = int(r.integers(0, self.e.n))
            return x if x != c else (x + 1) % self.e.n
        return int(self.e.contacts[c, r.integers(0, self.e.n_contacts[c])])

    def _counterparty(self, c: int, ttype: int, p_new: float, fixed: int | None = None):
        r = self.rng
        if ttype == SEND:
            return CP_CUSTOMER, fixed if fixed is not None else self._recipient(c, p_new)
        if ttype == PAYBILL:
            return CP_MERCHANT, int(self.e.pool_paybill[r.integers(0, len(self.e.pool_paybill))])
        if ttype == TILL:
            return CP_MERCHANT, int(self.e.pool_till[r.integers(0, len(self.e.pool_till))])
        if ttype == WITHDRAW:
            if r.random() < 0.4:
                return CP_AGENT, int(self.e.fav_agent[c, 0])
            return CP_AGENT, int(r.integers(0, len(self.e.a_county)))
        return CP_NONE, -1

    def _row(self, c, t, ttype, amount, cp, dev, county, pattern):
        return (
            c,
            int(t),
            int(ttype),
            int(min(amount, MAX_TXN_KES)),
            cp[0],
            cp[1],
            dev,
            county,
            int(self.rng.random() < 0.55),
            pattern,
            self.ep,
        )

    # ---- episodes ----------------------------------------------------------------------
    def sim_swap(self):
        r, c, s = self.rng, self._victim(), self.rng.beta(2, 2)
        anchor = self._anchor(c) if r.random() < 0.5 else None
        t = anchor + int(r.integers(300, 3600)) if anchor else self._time(0.55 * (1 - 0.6 * s))
        dev = self.alloc.attacker(r) if r.random() < 0.85 else self._vdev(c, t)
        county = self._county(c, 0.45)
        rows = []
        for _ in range(int(r.integers(2, 7))):
            tt = int(r.choice([SEND, PAYBILL, AIRTIME], p=[0.75, 0.15, 0.10]))
            amt = self._amount(c, r.uniform(1.5, 8), s, tt)
            rows.append(self._row(c, t, tt, amt, self._counterparty(c, tt, 0.9), dev, county, 0))
            t += int(r.integers(60, 1200))
        return rows

    def social_engineering(self):
        r, c, s = self.rng, self._victim(), self.rng.beta(2, 2)
        t = self._time(0.08)
        dev = self._dev(c, t, 0.03)
        county = self._county(c, 0.05)
        rows = []
        for _ in range(int(r.integers(1, 4))):
            tt = int(r.choice([SEND, PAYBILL, AIRTIME], p=[0.82, 0.12, 0.06]))
            amt = self._amount(c, r.uniform(1.2, 6), s, tt)
            rows.append(self._row(c, t, tt, amt, self._counterparty(c, tt, 0.85), dev, county, 1))
            t += int(r.integers(300, 7200))
        return rows

    def velocity_burst(self):
        r, c, s = self.rng, self._victim(), self.rng.beta(2, 2)
        t0 = self._time(0.35 * (1 - 0.6 * s))
        n = int(r.integers(6, 21))
        times = t0 + np.sort(r.integers(0, int(r.integers(2, 16)) * 60, n))
        dev = self.alloc.attacker(r) if r.random() < 0.4 else self._vdev(c, t0)
        county = self._county(c, 0.1)
        mule = self._recipient(c, 1.0)
        rows = []
        for t in times:
            tt = int(r.choice([SEND, AIRTIME, TILL], p=[0.55, 0.25, 0.20]))
            amt = self._amount(c, r.uniform(0.2, 1.2), s, tt)
            fixed = mule if r.random() >= 0.7 else None
            cp = self._counterparty(c, tt, 1.0, fixed)
            rows.append(self._row(c, t, tt, amt, cp, dev, county, 2))
        return rows

    def behavioral_shift(self):
        r, c, s = self.rng, self._victim(), self.rng.beta(2, 2)
        t0 = self._time(0.30 * (1 - 0.6 * s))
        n = int(r.integers(3, 9))
        times = t0 + np.sort(r.integers(0, int(r.integers(1, 4)) * 86400, n))
        rows = []
        for t in times:
            tt = int(r.choice([SEND, WITHDRAW], p=[0.5, 0.5]))
            amt = self._amount(c, r.uniform(2, 5), s, tt)
            dev = self._dev(c, int(t), 0.25)
            cp = self._counterparty(c, tt, 0.7)
            rows.append(self._row(c, t, tt, amt, cp, dev, self._county(c, 0.2), 3))
        return rows

    def structuring(self):
        r, c, s = self.rng, self._victim(), self.rng.beta(2, 2)
        t = self._time(0.25 * (1 - 0.6 * s))
        mule = self._recipient(c, 1.0)
        rows = []
        for _ in range(int(r.integers(4, 10))):
            amt = int(r.integers(STRUCTURING_LOW, STRUCTURING_HIGH + 1))
            fixed = mule if r.random() < 0.8 else None
            cp = self._counterparty(c, SEND, 1.0, fixed)
            rows.append(self._row(c, t, SEND, amt, cp, self._dev(c, t, 0.2), self._county(c, 0), 4))
            t += int(r.integers(120, 1500))
        return rows

    def amount_anomaly(self):
        r, c, s = self.rng, self._victim(), self.rng.beta(2, 2)
        t = self._time(0.30 * (1 - 0.6 * s))
        tt = int(r.choice([SEND, WITHDRAW], p=[0.6, 0.4]))
        amt = self._amount(c, r.uniform(6, 25), s, tt)
        dev = self._dev(c, t, 0.1)
        return [
            self._row(
                c,
                t,
                tt,
                amt,
                self._counterparty(c, tt, 0.6),
                dev,
                self._county(c, 0.1),
                5,
            )
        ]

    def location_jump(self):
        r, c, s = self.rng, self._victim(), self.rng.beta(2, 2)
        anchor = self._anchor(c)
        t = anchor + int(r.integers(300, 2700)) if anchor else self._time(0.2 * (1 - 0.6 * s))
        dev = self._dev(c, t, 0.3)
        county = self._county(c, 1.0)
        rows = []
        for _ in range(int(r.integers(1, 4))):
            tt = int(r.choice([SEND, PAYBILL, WITHDRAW, AIRTIME], p=[0.5, 0.15, 0.25, 0.10]))
            amt = self._amount(c, r.uniform(1, 5), s, tt)
            rows.append(self._row(c, t, tt, amt, self._counterparty(c, tt, 0.5), dev, county, 6))
            t += int(r.integers(60, 900))
        return rows

    def mule_cashouts(self, rows):
        r, out = self.rng, []
        for row in rows:
            _, t, ttype, amount, cp_type, cp_idx = row[:6]
            if ttype != SEND or cp_type != CP_CUSTOMER or not self.e.is_mule[cp_idx]:
                continue
            if r.random() >= 0.7:
                continue
            t2 = t + int(r.integers(180, 5400))
            m = int(cp_idx)
            amt = int(finalize_amount(np.array([amount * r.uniform(0.7, 1.0)]), r, 50)[0])
            agent = (CP_AGENT, int(r.integers(0, len(self.e.a_county))))
            out.append(
                self._row(
                    m,
                    t2,
                    WITHDRAW,
                    amt,
                    agent,
                    self._vdev(m, t2),
                    self._county(m, 0.5),
                    7,
                )
            )
        return out

    # ---- driver ------------------------------------------------------------------------
    def run(self, target: int) -> dict:
        builders = [
            self.sim_swap,
            self.social_engineering,
            self.velocity_burst,
            self.behavioral_shift,
            self.structuring,
            self.amount_anomaly,
            self.location_jump,
        ]
        rows, count, guard = [], 0, 0
        while count < target:
            guard += 1
            if guard > 50 * target + 1000:
                raise RuntimeError("could not generate the requested number of fraud rows")
            self.ep += 1
            ep_rows = builders[int(self.rng.choice(len(builders), p=EPISODE_P))]()
            ep_rows += self.mule_cashouts(ep_rows)
            ep_rows = sorted((x for x in ep_rows if x[1] <= self.end_t), key=lambda x: x[1])
            ep_rows = ep_rows[: target - count]
            rows += ep_rows
            count += len(ep_rows)
        return _to_arrays(rows)


def _to_arrays(rows: list) -> dict:
    if not rows:
        empty = np.zeros(0, dtype=np.int64)
        return {**{k: empty for k in FIELDS}, "pattern": empty, "episode": empty}
    cols = list(zip(*rows, strict=True))
    names = (*FIELDS, "pattern", "episode")
    return {n: np.array(c, dtype=np.int64) for n, c in zip(names, cols, strict=True)}


def generate_fraud(cfg, ents, legit, rng, target, alloc) -> dict:
    return FraudInjector(cfg, ents, legit, rng, alloc).run(target)
