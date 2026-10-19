"""The scoring API without a trained model: input rules, errors and the service bookkeeping."""

import threading
import time
from datetime import date, datetime, timedelta

import polars as pl
import pytest

pytest.importorskip("sklearn")
pytest.importorskip("pydantic")
fastapi = pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient  # noqa: E402
from pydantic import ValidationError  # noqa: E402

from mmrisk.api import (  # noqa: E402
    ModelLoadError,
    ScoringService,
    UnknownCustomerError,
    load_model,
)
from mmrisk.api.app import MAX_BODY_BYTES, create_app  # noqa: E402
from mmrisk.api.schemas import TransactionIn  # noqa: E402
from mmrisk.api.service import (  # noqa: E402
    DuplicateConflictError,
    OutOfOrderTransactionError,
)
from mmrisk.models.bundle import RiskModel  # noqa: E402
from mmrisk.models.calibration import PlattCalibrator  # noqa: E402
from mmrisk.models.costs import CostModel  # noqa: E402

T0 = datetime(2025, 6, 1, 12, 0, 0)


def txn(i=1, cid="C000001", minutes=0, **kw):
    base = {
        "transaction_id": f"T{i:06d}", "customer_id": cid,
        "timestamp": T0 + timedelta(minutes=minutes),
        "transaction_type": "SEND_MONEY", "amount_kes": 1500, "counterparty_type": "CUSTOMER",
        "counterparty_id": "C000002", "device_id": "D000001", "location_county": "Nairobi",
        "channel": "APP",
    }  # fmt: skip
    return {**base, **kw}


def wire(t):
    return {**t, "timestamp": t["timestamp"].isoformat()}


# ---- input rules ----


def test_a_valid_transaction_is_accepted():
    assert TransactionIn(**wire(txn())).amount_kes == 1500


@pytest.mark.parametrize(
    "change",
    [
        {"extra_field": 1},
        {"transaction_type": "WIRE"},
        {"channel": "FAX"},
        {"location_county": "Atlantis"},
        {"amount_kes": 0},
        {"amount_kes": -5},
        {"amount_kes": 250_001},
        {"amount_kes": 12.5},
        {"amount_kes": "1500"},
        {"amount_kes": True},
        {"timestamp": "2025-06-01T12:00:00+03:00"},
        {"timestamp": "1999-01-01T00:00:00"},
        {"timestamp": "not a time"},
        {"transaction_id": "has space"},
        {"transaction_id": ""},
        {"customer_id": "../../etc"},
        {"device_id": "x" * 65},
        {"counterparty_type": "MERCHANT"},  # SEND_MONEY goes to a customer
        {"counterparty_id": None},
        {"transaction_type": "AIRTIME"},  # airtime has no counterparty
    ],
)
def test_invalid_transactions_are_refused(change):
    with pytest.raises(ValidationError):
        TransactionIn(**{**wire(txn()), **change})


def test_airtime_without_a_counterparty_is_valid():
    t = wire(txn(transaction_type="AIRTIME", counterparty_type=None, counterparty_id=None))
    assert TransactionIn(**t).counterparty_id is None


def test_the_amount_limit_is_the_ingestion_limit():
    from mmrisk.validation import config as cfg

    assert TransactionIn(**{**wire(txn()), "amount_kes": cfg.MAX_AMOUNT_KES}).amount_kes == 250_000


# ---- a stub model to test the bookkeeping ----


class Stub:
    """Scores by amount and remembers the frames it saw; can be told to fail once."""

    name = "stub"

    def __init__(self, pause=0.0):
        self.seen: list[pl.DataFrame] = []
        self.fail_next = False
        self.pause, self.active, self.peak = pause, 0, 0

    def score(self, df):
        self.active += 1
        self.peak = max(self.peak, self.active)
        try:
            time.sleep(self.pause)
            if self.fail_next:
                self.fail_next = False
                raise RuntimeError("scorer failed")
            self.seen.append(df)
            return df["amount_kes"].to_numpy().astype(float) / 50_000.0 - 3.0
        finally:
            self.active -= 1


def customers(n=40):
    return pl.DataFrame(
        {
            "customer_id": [f"C{i:06d}" for i in range(1, n + 1)],
            "segment": ["SALARIED"] * n,
            "home_county": ["Nairobi"] * n,
            "registration_date": [date(2024, 1, 1)] * n,
        }
    )


def stub_model(version="1.0", pause=0.0):
    return RiskModel(Stub(pause), PlattCalibrator(1.0, 0.0, "identity"), CostModel(), version)


@pytest.fixture
def svc():
    return ScoringService(stub_model(), customers())


def n_prior(service):
    return service.model.scorer.seen[-1]["n_prior_txns"][0]


def test_scoring_records_the_transaction_for_the_next_one(svc):
    svc.score(txn(1, minutes=0))
    assert n_prior(svc) == 0
    svc.score(txn(2, minutes=5))
    assert n_prior(svc) == 1
    svc.score(txn(3, "C000009", minutes=6))  # another customer has no history
    assert n_prior(svc) == 0
    assert svc.info()["history_transactions"] == 3


def test_a_response_has_the_documented_fields(svc):
    out = svc.score(txn(1, amount_kes=250_000))
    assert set(out) == {
        "transaction_id", "risk_probability", "action", "risk_level",
        "expected_cost_assumed_kes", "reasons", "feature_version", "duplicate",
    }  # fmt: skip
    assert out["action"] in ("ALLOW", "REVIEW", "BLOCK") and 0 <= out["risk_probability"] <= 1
    assert out["reasons"] == []  # this scorer cannot explain itself; the score is still returned
    assert set(out["expected_cost_assumed_kes"]) == {"allow", "review", "block"}


def test_the_same_transaction_twice_gives_the_first_answer_and_is_recorded_once(svc):
    first = svc.score(txn(1))
    again = svc.score(txn(1))
    assert again["duplicate"] is True and first["duplicate"] is False
    assert {k: v for k, v in again.items() if k != "duplicate"} == {
        k: v for k, v in first.items() if k != "duplicate"
    }
    assert svc.info()["history_transactions"] == 1
    svc.score(txn(2, minutes=5))
    assert n_prior(svc) == 1  # not 2: the repeat was not counted as history


def test_the_same_id_with_other_content_is_refused(svc):
    svc.score(txn(1))
    with pytest.raises(DuplicateConflictError):
        svc.score(txn(1, amount_kes=9_999))
    assert svc.info()["history_transactions"] == 1


def test_an_unknown_customer_is_refused_and_changes_nothing(svc):
    with pytest.raises(UnknownCustomerError):
        svc.score(txn(1, "C999999"))
    assert svc.info()["history_transactions"] == 0
    svc.score(txn(1, minutes=1))  # the id is free to use again
    assert n_prior(svc) == 0


def test_an_older_transaction_is_refused_and_changes_nothing(svc):
    svc.score(txn(1, minutes=10))
    with pytest.raises(OutOfOrderTransactionError):
        svc.score(txn(2, minutes=5))
    assert svc.info()["history_transactions"] == 1
    svc.score(txn(3, minutes=20))
    assert n_prior(svc) == 1


def test_a_failure_while_scoring_leaves_the_state_as_it_was(svc):
    svc.model.scorer.fail_next = True
    with pytest.raises(RuntimeError):
        svc.score(txn(1))
    assert svc.info()["history_transactions"] == 0
    svc.score(txn(1))  # a retry works and sees no phantom history
    assert n_prior(svc) == 0


def test_the_answer_cache_is_bounded(svc):
    small = ScoringService(stub_model(), customers(), cache_size=2)
    for i in range(1, 4):
        small.score(txn(i, minutes=i))
    assert small.score(txn(3, minutes=3))["duplicate"] is True
    with pytest.raises(OutOfOrderTransactionError):  # id 1 was forgotten, and it is old
        small.score(txn(1, minutes=1))


def test_replayed_history_is_used_whatever_its_order():
    rows = [txn(i, minutes=i) for i in range(1, 6)]
    a = ScoringService(stub_model(), customers(), pl.DataFrame(rows))
    b = ScoringService(stub_model(), customers(), pl.DataFrame(rows[::-1]))
    a.score(txn(9, minutes=30))
    b.score(txn(9, minutes=30))
    assert n_prior(a) == n_prior(b) == 5
    assert a.info()["history_transactions"] == 6


def test_a_model_for_other_features_is_refused(tmp_path):
    with pytest.raises(ModelLoadError, match="feature version"):
        ScoringService(stub_model("0.9"), customers())
    with pytest.raises(ModelLoadError, match="no model"):
        load_model(tmp_path)


def test_scoring_from_several_threads_keeps_every_transaction():
    service = ScoringService(stub_model(pause=0.003), customers(40))
    errors = []

    def work(k):
        try:
            for j in range(10):
                service.score(txn(k * 100 + j, f"C{k:06d}", minutes=j))
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=work, args=(k,)) for k in range(1, 9)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert not errors and service.info()["history_transactions"] == 80
    assert service.model.scorer.peak == 1  # the lock lets one request score at a time


def test_invalid_cache_or_top_k_is_refused():
    with pytest.raises(ValueError):
        ScoringService(stub_model(), customers(), cache_size=0)


# ---- the HTTP layer ----


@pytest.fixture
def client(svc):
    return TestClient(create_app(svc))


def post(client, body, **kw):
    return client.post("/v1/score", json=body, **kw)


def test_health_is_up_and_ready_needs_a_model(client):
    assert client.get("/health").json() == {"status": "ok"}
    assert client.get("/ready").json()["feature_version"] == "1.0"
    empty = TestClient(create_app(None))
    assert empty.get("/health").status_code == 200
    for call in (empty.get("/ready"), empty.get("/v1/model"), post(empty, wire(txn()))):
        assert call.status_code == 503 and call.json()["error"]["code"] == "not_ready"


def test_the_model_description_labels_the_costs_as_assumed(client):
    info = client.get("/v1/model").json()
    assert "cost_model_ASSUMED" in info and info["synthetic_data_only"] is True
    assert info["customers"] == 40 and info["scorer"] == "stub"


def test_scoring_over_http(client):
    r = post(client, wire(txn()))
    assert r.status_code == 200 and r.json()["transaction_id"] == "T000001"
    assert float(r.headers["x-process-time-ms"]) >= 0


def test_error_codes(client):
    assert post(client, wire(txn(1, "C999999"))).status_code == 404
    post(client, wire(txn(2, minutes=10)))
    old = post(client, wire(txn(3, minutes=1)))
    assert old.status_code == 409 and old.json()["error"]["code"] == "out_of_order"
    clash = post(client, wire(txn(2, minutes=10, amount_kes=7)))
    assert clash.status_code == 409
    assert clash.json()["error"]["code"] == "duplicate_id_different_content"
    bad = post(client, {**wire(txn(4)), "amount_kes": 0})
    assert bad.status_code == 422 and bad.json()["error"]["code"] == "invalid_transaction"


def test_an_invalid_request_is_not_echoed_back(client):
    secret = "SECRET-VALUE-123"
    r = post(client, {**wire(txn()), "channel": secret, "surprise": secret})
    assert r.status_code == 422
    assert secret not in r.text and "channel" in r.text and "surprise" in r.text


def test_malformed_json_gives_the_standard_error_shape(client):
    r = client.post("/v1/score", content=b"{not json", headers={"content-type": "application/json"})
    assert r.status_code == 422 and set(r.json()) == {"error"}


def test_a_large_body_is_refused_before_it_is_read(client):
    big = b'{"x": "' + b"a" * (MAX_BODY_BYTES + 10) + b'"}'
    r = client.post("/v1/score", content=big, headers={"content-type": "application/json"})
    assert r.status_code == 413 and r.json()["error"]["code"] == "body_too_large"


def test_a_large_body_without_a_declared_length_is_refused_too(client):
    def chunks():
        for _ in range(MAX_BODY_BYTES // 1000 + 2):
            yield b"a" * 1000

    r = client.post("/v1/score", content=chunks(), headers={"content-type": "application/json"})
    assert r.status_code == 413


def test_a_request_id_is_kept_only_when_it_is_safe(client):
    assert (
        client.get("/health", headers={"x-request-id": "abc-123"}).headers["x-request-id"]
        == "abc-123"
    )
    unsafe = client.get("/health", headers={"x-request-id": "a b;c"}).headers["x-request-id"]
    assert unsafe != "a b;c" and len(unsafe) == 32
    assert client.get("/health").headers["x-request-id"]


def test_the_docs_can_be_turned_off(svc):
    assert TestClient(create_app(svc)).get("/docs").status_code == 200
    off = TestClient(create_app(svc, docs=False))
    assert off.get("/docs").status_code == 404 and off.get("/openapi.json").status_code == 404
    assert TestClient(create_app(svc)).get("/openapi.json").status_code == 200


def test_an_unexpected_error_hides_its_details(svc):
    app = create_app(svc)

    @app.get("/boom")
    def boom():
        raise RuntimeError("password=hunter2")

    r = TestClient(app, raise_server_exceptions=False).get("/boom")
    assert r.status_code == 500 and "hunter2" not in r.text
    assert r.json()["error"]["code"] == "internal_error"
