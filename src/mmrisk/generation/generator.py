"""Orchestrates generation and writes Parquet files plus a run manifest."""

from __future__ import annotations

import hashlib
import json
import platform
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import polars as pl

from mmrisk import __version__
from mmrisk.generation.config import GeneratorConfig
from mmrisk.generation.entities import build_entities
from mmrisk.generation.fraud import DeviceAllocator, generate_fraud
from mmrisk.generation.labels import make_labels
from mmrisk.generation.legit import FIELDS, generate_legit
from mmrisk.generation.reference import (
    COUNTIES,
    COUNTY_NAMES,
    CP_NAMES,
    PATTERNS,
    PAYBILL_CATEGORIES,
    SEGMENTS,
    TILL_CATEGORIES,
    TXN_TYPES,
)


def _ids(prefix: str, idx: np.ndarray, width: int) -> np.ndarray:
    return np.char.mod(f"{prefix}%0{width}d", idx + 1)


def _ts(epoch: np.ndarray) -> pl.Series:
    return pl.from_epoch(pl.Series(epoch.astype(np.int64)), time_unit="s").cast(pl.Datetime("us"))


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _build_frames(cfg: GeneratorConfig) -> dict[str, pl.DataFrame]:
    streams = np.random.SeedSequence(cfg.seed).spawn(5)
    r_ent, r_leg, r_fr, r_lab, r_dev = (np.random.default_rng(s) for s in streams)
    ents = build_entities(cfg, r_ent)
    n_fraud = cfg.num_fraud
    legit = generate_legit(cfg, ents, r_leg, cfg.num_transactions - n_fraud)
    alloc = DeviceAllocator(len(ents.device_is_feature))
    fraud = generate_fraud(cfg, ents, legit, r_fr, n_fraud, alloc)

    # Device ids are randomly permuted so their numeric order carries no information
    # (otherwise attacker devices, allocated last, would have the highest numbers).
    n_dev = alloc.next
    dev_perm = r_dev.permutation(n_dev)
    legit["device"] = dev_perm[legit["device"]]
    fraud["device"] = dev_perm[fraud["device"]]

    m_leg = len(legit["cust"])
    cols = {k: np.concatenate([legit[k], fraud[k]]) for k in FIELDS}
    is_fraud = np.r_[np.zeros(m_leg, bool), np.ones(len(fraud["cust"]), bool)]
    pattern = np.r_[np.full(m_leg, -1), fraud["pattern"]]
    episode = np.r_[np.full(m_leg, -1), fraud["episode"]]
    order = np.lexsort((np.arange(len(is_fraud)), cols["t"]))
    cols = {k: v[order] for k, v in cols.items()}
    is_fraud, pattern, episode = is_fraud[order], pattern[order], episode[order]
    m = len(is_fraud)

    start_epoch = int(np.datetime64(cfg.start_date, "s").astype("int64"))
    t_epoch = start_epoch + cols["t"]
    txn_ids = _ids("TXN", np.arange(m), 9)

    cp_id = np.full(m, None, dtype=object)
    cp_type_name = np.full(m, None, dtype=object)
    for code, prefix, width in ((1, "C", 6), (2, "M", 5), (3, "A", 4)):
        mask = cols["cp_type"] == code
        cp_id[mask] = _ids(prefix, cols["cp_idx"][mask], width)
        cp_type_name[mask] = CP_NAMES[code]

    transactions = pl.DataFrame(
        {
            "transaction_id": txn_ids,
            "customer_id": _ids("C", cols["cust"], 6),
            "timestamp": _ts(t_epoch),
            "transaction_type": np.array(TXN_TYPES)[cols["ttype"]],
            "amount_kes": cols["amount"],
            "counterparty_type": pl.Series(cp_type_name.tolist(), dtype=pl.Utf8),
            "counterparty_id": pl.Series(cp_id.tolist(), dtype=pl.Utf8),
            "device_id": _ids("D", cols["device"], 6),
            "location_county": np.array(COUNTY_NAMES)[cols["county"]],
            "channel": np.where(cols["channel"] == 1, "APP", "USSD"),
        }
    )

    label, avail = make_labels(is_fraud, t_epoch, r_lab)
    labels = pl.DataFrame(
        {
            "transaction_id": txn_ids,
            "label_is_fraud": label,
            "label_available_at": _ts(avail),
        }
    )
    pat_names = [*PATTERNS, ""]
    truth = pl.DataFrame(
        {
            "transaction_id": txn_ids,
            "true_is_fraud": is_fraud,
            "fraud_pattern": pl.Series(
                [pat_names[p] if p >= 0 else None for p in pattern.tolist()],
                dtype=pl.Utf8,
            ),
            "episode_id": pl.Series(
                [e if e >= 0 else None for e in episode.tolist()], dtype=pl.Int64
            ),
        }
    )

    n = ents.n
    reg = np.datetime64(cfg.start_date) - ents.reg_days_before
    customers = pl.DataFrame(
        {
            "customer_id": _ids("C", np.arange(n), 6),
            "segment": np.array(SEGMENTS)[ents.segment],
            "home_county": np.array(COUNTY_NAMES)[ents.home],
            "registration_date": pl.Series(reg),
        }
    )
    merchants = pl.DataFrame(
        {
            "merchant_id": _ids("M", np.arange(len(ents.m_type)), 5),
            "merchant_type": np.where(ents.m_type == 0, "PAYBILL", "TILL"),
            "category": [
                (PAYBILL_CATEGORIES if t == 0 else TILL_CATEGORIES)[c]
                for t, c in zip(ents.m_type, ents.m_cat, strict=True)
            ],
            "county": np.array(COUNTY_NAMES)[ents.m_county],
        }
    )
    agents = pl.DataFrame(
        {
            "agent_id": _ids("A", np.arange(len(ents.a_county)), 4),
            "county": np.array(COUNTY_NAMES)[ents.a_county],
        }
    )
    is_feat_alloc = np.r_[ents.device_is_feature, np.array(alloc.extra_is_feature, dtype=bool)]
    is_feat = np.empty(n_dev, dtype=bool)
    is_feat[dev_perm] = is_feat_alloc
    devices = pl.DataFrame(
        {
            "device_id": _ids("D", np.arange(len(is_feat)), 6),
            "device_type": np.where(is_feat, "FEATURE_PHONE", "SMARTPHONE"),
        }
    )
    counties = pl.DataFrame(
        {
            "county": COUNTY_NAMES,
            "latitude": [c[1] for c in COUNTIES],
            "longitude": [c[2] for c in COUNTIES],
        }
    )
    latent = pl.DataFrame(
        {
            "customer_id": _ids("C", np.arange(n), 6),
            "is_mule": ents.is_mule,
            "vulnerability": ents.vulnerability,
        }
    )
    return {
        "customers": customers,
        "merchants": merchants,
        "agents": agents,
        "devices": devices,
        "counties": counties,
        "transactions": transactions,
        "labels": labels,
        "ground_truth/transaction_truth": truth,
        "ground_truth/customer_latent": latent,
    }


def generate(cfg: GeneratorConfig, write: bool = True) -> dict[str, pl.DataFrame]:
    """Generate all tables (deterministic for a given config) and optionally write Parquet."""
    cfg.validate()
    frames = _build_frames(cfg)
    if write:
        out = Path(cfg.output_dir)
        (out / "ground_truth").mkdir(parents=True, exist_ok=True)
        files = {}
        for name, df in frames.items():
            path = out / f"{name}.parquet"
            df.write_parquet(path)
            files[f"{name}.parquet"] = {"rows": df.height, "sha256": _sha256(path)}
        truth = frames["ground_truth/transaction_truth"]
        patterns = (
            truth.filter(pl.col("true_is_fraud"))
            .group_by("fraud_pattern")
            .len()
            .sort("fraud_pattern")
            .to_dicts()
        )
        manifest = {
            "generator_version": __version__,
            "generated_at_utc": datetime.now(UTC).isoformat(timespec="seconds"),
            "synthetic_data_only": True,
            "config": {
                "num_customers": cfg.num_customers,
                "num_transactions": cfg.num_transactions,
                "fraud_rate": cfg.fraud_rate,
                "seed": cfg.seed,
                "start_date": cfg.start_date.isoformat(),
                "end_date": cfg.end_date.isoformat(),
            },
            "true_fraud_rows": int(truth["true_is_fraud"].sum()),
            "fraud_rows_by_pattern": {p["fraud_pattern"]: p["len"] for p in patterns},
            "environment": {
                "python": platform.python_version(),
                "numpy": np.__version__,
                "polars": pl.__version__,
            },
            "files": files,
        }
        (out / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True))
    return frames
