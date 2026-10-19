"""Phase 11 tests with a trained model: the API must agree with batch scoring."""

from datetime import date, datetime

import polars as pl
import pytest

pytest.importorskip("sklearn")
pytest.importorskip("lightgbm")
pytest.importorskip("httpx")
pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from mmrisk.api import ModelLoadError, ScoringService  # noqa: E402
from mmrisk.api.app import create_app  # noqa: E402
from mmrisk.api.cli import main as cli  # noqa: E402
from mmrisk.api.loading import build_service  # noqa: E402
from mmrisk.features import compute_features  # noqa: E402
from mmrisk.generation import GeneratorConfig, generate  # noqa: E402
from mmrisk.ingestion import ingest_source_dir, read_layer  # noqa: E402
from mmrisk.models.boosting import fit_lightgbm  # noqa: E402
from mmrisk.models.bundle import RiskModel  # noqa: E402
from mmrisk.models.calibration import PlattCalibrator  # noqa: E402
from mmrisk.models.costs import CostModel  # noqa: E402
from mmrisk.models.split import make_split  # noqa: E402

CLOCK = lambda: datetime(2026, 3, 1, 12, 0, 0)  # noqa: E731
HELD = 400
COLUMNS = [
    "transaction_id", "customer_id", "timestamp", "transaction_type", "amount_kes",
    "counterparty_type", "counterparty_id", "device_id", "location_county", "channel",
]  # fmt: skip


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    root = tmp_path_factory.mktemp("api")
    src, lake = root / "src", root / "lake"
    generate(
        GeneratorConfig(
            num_customers=400, num_transactions=24_000, fraud_rate=0.02, seed=31,
            start_date=date(2025, 1, 1), end_date=date(2025, 12, 31), output_dir=src,
        )
    )  # fmt: skip
    ingest_source_dir(src, lake, CLOCK)
    return root, lake


@pytest.fixture(scope="module")
def data(env):
    lake = env[1]
    return {t: read_layer(lake, "cleaned", t) for t in ("transactions", "customers", "labels")}


@pytest.fixture(scope="module")
def features(data):
    return compute_features(data["transactions"], data["customers"])


@pytest.fixture(scope="module")
def model(features, data):
    split = make_split(features, data["labels"])
    booster, _ = fit_lightgbm(split, seed=0, quick=True)
    y = split.val["label_is_fraud"].to_numpy()
    cal = PlattCalibrator.fit(booster.score(split.val), y)
    # a cheap review (ASSUMED, test only) so that many transactions are flagged and get reasons
    return RiskModel(booster, cal, CostModel(review_cost=2.0))


@pytest.fixture(scope="module")
def model_dir(env, model):
    model.save(env[0] / "advanced")
    return env[0] / "advanced"


@pytest.fixture(scope="module")
def ordered(data):
    return data["transactions"].sort("timestamp", "transaction_id")


@pytest.fixture(scope="module")
def batch(model, features, ordered):
    ids = ordered.tail(HELD)["transaction_id"].to_list()
    out = model.assess(features.filter(pl.col("transaction_id").is_in(ids)))
    return {r["transaction_id"]: r for r in out.to_dicts()}


def wire(row):
    r = {k: row[k] for k in COLUMNS}
    r["timestamp"] = r["timestamp"].isoformat()
    return r


@pytest.fixture(scope="module")
def streamed(model, data, ordered):
    """The held-out transactions posted one by one over HTTP after replaying the earlier ones."""
    service = ScoringService(model, data["customers"], ordered.head(ordered.height - HELD))
    client = TestClient(create_app(service))
    out = {}
    for row in ordered.tail(HELD).to_dicts():
        r = client.post("/v1/score", json=wire(row))
        assert r.status_code == 200, r.text
        out[row["transaction_id"]] = r.json()
    return out


# ---- the API gives the batch answer ----


def test_the_api_matches_batch_scoring_exactly(streamed, batch):
    assert streamed.keys() == batch.keys() and len(batch) == HELD
    for tid, want in batch.items():
        got = streamed[tid]
        assert got["risk_probability"] == pytest.approx(want["risk_probability"], abs=1e-12), tid
        assert got["action"] == want["action"] and got["risk_level"] == want["risk_level"]
        assert got["expected_cost_assumed_kes"]["review"] == pytest.approx(
            want["expected_cost_review"], abs=1e-9
        )


def test_the_run_is_not_trivial(streamed):
    actions = {o["action"] for o in streamed.values()}
    assert len(actions) >= 2, actions  # both flagged and allowed transactions were compared
    probs = [o["risk_probability"] for o in streamed.values()]
    assert max(probs) > 10 * min(probs)


def test_reasons_match_the_model_and_appear_only_when_flagged(streamed, model, features):
    flagged = [t for t, o in streamed.items() if o["action"] != "ALLOW"]
    assert flagged
    frame = features.filter(pl.col("transaction_id").is_in(flagged))
    want = model.explain(frame, top_k=3).to_dicts()
    for tid in flagged:
        reasons = streamed[tid]["reasons"]
        expected = [r for r in want if r["transaction_id"] == tid]
        assert [r["feature"] for r in reasons] == [r["feature"] for r in expected]
        assert [r["rank"] for r in reasons] == list(range(1, len(reasons) + 1))
        assert all(r["effect"] > 0 and r["text"] for r in reasons)
    assert all(o["reasons"] == [] for o in streamed.values() if o["action"] == "ALLOW")


def test_history_matters(model, data, ordered, batch):
    """With no replayed history the same transaction scores differently (not a vacuous check)."""
    row = ordered.tail(1).to_dicts()[0]
    cold = ScoringService(model, data["customers"]).score(row)
    assert cold["risk_probability"] != pytest.approx(
        batch[row["transaction_id"]]["risk_probability"], abs=1e-9
    )


def test_failed_requests_do_not_disturb_later_scores(model, data, ordered, batch):
    service = ScoringService(model, data["customers"], ordered.head(ordered.height - 3))
    client = TestClient(create_app(service))
    a, b, c = (wire(r) for r in ordered.tail(3).to_dicts())
    assert client.post("/v1/score", json={**a, "customer_id": "C999999"}).status_code == 404
    assert client.post("/v1/score", json={**a, "amount_kes": 0}).status_code == 422
    first = client.post("/v1/score", json=a).json()
    assert client.post("/v1/score", json={**a, "amount_kes": 11}).status_code == 409
    again = client.post("/v1/score", json=a).json()
    assert again["duplicate"] is True and again["risk_probability"] == first["risk_probability"]
    for t in (a, b, c):
        got = client.post("/v1/score", json=t).json()
        assert got["risk_probability"] == pytest.approx(
            batch[t["transaction_id"]]["risk_probability"], abs=1e-12
        )


def test_latency_is_reported_and_small_on_this_dataset(streamed, model, data, ordered):
    service = ScoringService(model, data["customers"], ordered.head(ordered.height - 50))
    client = TestClient(create_app(service))
    ms = [
        float(client.post("/v1/score", json=wire(r)).headers["x-process-time-ms"])
        for r in ordered.tail(50).to_dicts()
    ]
    assert max(ms) < 2000  # a loose guard against a regression to something quadratic


# ---- loading ----


def test_build_service_replays_only_history_before_the_cutoff(model_dir, env, data):
    cutoff = datetime(2025, 7, 1)
    service = build_service(model_dir, env[1], cutoff)
    n = data["transactions"].filter(pl.col("timestamp") < cutoff).height
    assert service.info()["history_transactions"] == n
    assert build_service(model_dir, env[1], use_history=False).info()["history_transactions"] == 0
    full = build_service(model_dir, env[1])
    assert full.info()["history_transactions"] == data["transactions"].height


def test_a_model_for_other_features_is_refused(model, env, data):
    stale = RiskModel(model.scorer, model.calibrator, model.cost_model, feature_version="0.9")
    stale.save(env[0] / "stale")
    with pytest.raises(ModelLoadError, match="feature version"):
        build_service(env[0] / "stale", env[1])


def test_a_missing_lake_or_model_is_a_clear_error(model_dir, env, tmp_path):
    with pytest.raises(ModelLoadError, match="lake"):
        build_service(model_dir, tmp_path / "none")
    with pytest.raises(ModelLoadError, match="no model"):
        build_service(tmp_path, env[1])


# ---- the command line ----


def test_check_command_passes_and_reports_latency(model_dir, env, capsys):
    code = cli(["check", "--model-dir", str(model_dir), "--lake", str(env[1]), "--n", "120"])
    out = capsys.readouterr().out
    assert code == 0 and "check: PASS" in out and "p95" in out
    assert "same action as batch scoring: True" in out


def test_check_command_reports_user_mistakes(model_dir, env, tmp_path, capsys):
    assert cli(["check", "--model-dir", str(tmp_path), "--lake", str(env[1])]) == 2
    assert cli(["check", "--model-dir", str(model_dir), "--lake", str(tmp_path / "x")]) == 2
    assert cli(["check", "--model-dir", str(model_dir), "--lake", str(env[1]), "--n", "0"]) == 2
    assert "error:" in capsys.readouterr().err


def test_serve_refuses_a_public_address_without_the_flag(model_dir, env, capsys, monkeypatch):
    uvicorn = pytest.importorskip("uvicorn")
    started = []
    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: started.append(1))
    code = cli(["serve", "--model-dir", str(model_dir), "--lake", str(env[1]), "--host", "0.0.0.0"])
    assert code == 2 and "no authentication" in capsys.readouterr().err and not started
    ok = cli(["serve", "--model-dir", str(model_dir), "--lake", str(env[1]), "--host", "0.0.0.0",
              "--allow-external", "--no-history"])  # fmt: skip
    assert ok == 0 and started and "WARNING" in capsys.readouterr().out


def test_serve_reports_a_bad_model_or_time_before_listening(model_dir, env, tmp_path, capsys):
    assert cli(["serve", "--model-dir", str(tmp_path), "--lake", str(env[1])]) == 2
    bad = cli(["serve", "--model-dir", str(model_dir), "--lake", str(env[1]),
               "--history-until", "yesterday"])  # fmt: skip
    assert bad == 2 and "error:" in capsys.readouterr().err


def test_serve_starts_uvicorn_on_the_loopback_address(model_dir, env, monkeypatch, capsys):
    uvicorn = pytest.importorskip("uvicorn")
    seen = {}
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: seen.update(app=app, **kw))
    code = cli(["serve", "--model-dir", str(model_dir), "--lake", str(env[1]),
                "--history-until", "2025-07-01", "--port", "9123", "--no-docs"])  # fmt: skip
    assert code == 0 and seen["host"] == "127.0.0.1" and seen["port"] == 9123
    assert TestClient(seen["app"]).get("/ready").status_code == 200
    assert TestClient(seen["app"]).get("/docs").status_code == 404
    assert "past transactions replayed" in capsys.readouterr().out
