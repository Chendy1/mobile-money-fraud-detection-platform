"""Builds small prediction stores, registries and report files for the dashboard tests."""

import json
from datetime import datetime, timedelta
from pathlib import Path

from mmrisk.registry import Registry
from mmrisk.streaming.store import PredictionStore

START = datetime(2025, 12, 1, 0, 0, 0)
COUNTIES = ["Nairobi", "Mombasa", "Kisumu"]
CHANNELS = ["app", "ussd", "agent"]


def reasons(text: str) -> list[dict]:
    return [{"rank": 1, "feature": "amount_kes", "effect": 1.2, "text": text},
            {"rank": 2, "feature": "hour", "effect": 0.4, "text": "late at night"}]  # fmt: skip


def make_store(directory: Path, n: int = 600, flush_rows: int = 250) -> Path:
    """n predictions, one every 7 minutes. Every 50th is BLOCK, every 20th REVIEW (unless BLOCK)."""
    store = PredictionStore(directory, flush_rows=flush_rows)
    for i in range(n):
        action = "BLOCK" if i % 50 == 49 else "REVIEW" if i % 20 == 19 else "ALLOW"
        p = {"ALLOW": 0.002, "REVIEW": 0.2, "BLOCK": 0.8}[action] + (i % 7) * 0.001
        txn = {
            "transaction_id": f"T{i:05d}", "customer_id": f"C{i % 40:06d}",
            "timestamp": START + timedelta(minutes=7 * i), "transaction_type": "send_money",
            "amount_kes": 500 + 10 * i, "channel": CHANNELS[i % 3],
            "location_county": COUNTIES[i % 3],
        }  # fmt: skip
        resp = {
            "risk_probability": p, "action": action,
            "risk_level": {"ALLOW": "LOW", "REVIEW": "MEDIUM", "BLOCK": "HIGH"}[action],
            "reasons": reasons(f"large amount {i}") if action != "ALLOW" else [],
            "feature_version": "1.0",
        }  # fmt: skip
        store.add(i + 1, txn, resp, processed_at=START + timedelta(days=30))
    store.close()
    return Path(directory)


def make_model_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    (path / "risk_model.joblib").write_bytes(b"x" + path.name.encode())
    (path / "risk_model.json").write_text(json.dumps({"feature_version": "1.0"}))
    return path


def make_registry(root: Path, tmp: Path, versions: int = 3) -> Registry:
    reg = Registry(root)
    for i in range(versions):
        gates = [] if i == 0 else [
            {"name": "pr_auc", "required": True, "passed": True, "detail": "d1"},
            {"name": "cost", "required": True, "passed": i == 1, "detail": "d2"},
            {"name": "agreement", "required": False, "passed": False, "detail": "d3"},
        ]  # fmt: skip
        v = reg.register(make_model_dir(tmp / f"m{i}"), source="bootstrap" if i == 0 else "retrain",
                         stage="approved" if i != 2 else "rejected", gates=gates)  # fmt: skip
        if i == 0:
            reg.promote(v, "first")
    return reg


def drift_payload(injected=None, n=12) -> dict:
    results = [{"name": f"f{i}", "status": "PASS", "psi": 0.01 * i, "noise": 0.005, "n": 900,
                "policy": "monitored", "detail": ""} for i in range(n)]  # fmt: skip
    results.append({"name": "skipped_one", "status": "SKIPPED", "psi": None, "noise": None,
                    "n": 0, "policy": "calendar", "detail": "short window"})  # fmt: skip
    return {
        "drift_detected": False, "verdict": "no major drift", "rows": 900, "window": "w1",
        "reference_window": "r1", "injected": injected, "results": results,
    }  # fmt: skip
