"""The HTTP scorer against real local servers, and its agreement with the in-process scorer."""

import socket
import threading
import time
from datetime import date, datetime, timedelta

import polars as pl
import pytest

pytest.importorskip("sklearn")
pytest.importorskip("fastapi")
uvicorn = pytest.importorskip("uvicorn")

from fastapi import FastAPI  # noqa: E402
from fastapi.responses import JSONResponse  # noqa: E402

from mmrisk.api import ScoringService  # noqa: E402
from mmrisk.api.app import create_app  # noqa: E402
from mmrisk.api.smoke import SmokeError  # noqa: E402
from mmrisk.models.bundle import RiskModel  # noqa: E402
from mmrisk.models.calibration import PlattCalibrator  # noqa: E402
from mmrisk.models.costs import CostModel  # noqa: E402
from mmrisk.streaming import HttpScorer, LocalScorer  # noqa: E402
from mmrisk.streaming.scorers import ERROR, INVALID, UNAVAILABLE  # noqa: E402

T0 = datetime(2025, 6, 1, 12, 0, 0)


class Stub:
    name = "stub"

    def score(self, df):
        return df["amount_kes"].to_numpy().astype(float) / 50_000.0 - 3.0


def service(n=40):
    customers = pl.DataFrame(
        {
            "customer_id": [f"C{i:06d}" for i in range(1, n + 1)],
            "segment": ["SALARIED"] * n,
            "home_county": ["Nairobi"] * n,
            "registration_date": [date(2024, 1, 1)] * n,
        }
    )
    model = RiskModel(Stub(), PlattCalibrator(1.0, 0.0, "identity"), CostModel())
    return ScoringService(model, customers)


def row(i, seconds, cid="C000001", **kw):
    base = {
        "transaction_id": f"T{i:06d}", "customer_id": cid,
        "timestamp": T0 + timedelta(seconds=seconds), "transaction_type": "SEND_MONEY",
        "amount_kes": 1500, "counterparty_type": "CUSTOMER", "counterparty_id": "C000002",
        "device_id": "D000001", "location_county": "Nairobi", "channel": "APP",
    }  # fmt: skip
    return {**base, **kw}


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Running:
    def __init__(self, app):
        self.port = free_port()
        cfg = uvicorn.Config(app, host="127.0.0.1", port=self.port, log_level="error")
        self.server = uvicorn.Server(cfg)
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def __enter__(self):
        self.thread.start()
        for _ in range(200):
            if self.server.started:
                return f"http://127.0.0.1:{self.port}"
            time.sleep(0.05)
        raise RuntimeError("server did not start")

    def __exit__(self, *exc):
        self.server.should_exit = True
        self.thread.join(timeout=10)


SCENARIO = [
    row(1, 10),
    row(1, 10),  # a repeat
    row(1, 10, amount_kes=9),  # the same id, other content
    row(2, 5),  # older than the customer's last
    row(3, 20, cid="C999999"),  # unknown customer
    row(4, 30, amount_kes=0),  # invalid
    row(5, 40, amount_kes=250_000),  # a high score
]


def test_the_http_and_local_scorers_give_the_same_answers():
    local = LocalScorer(service())
    with Running(create_app(service())) as url:
        remote = HttpScorer(url)
        for txn in SCENARIO:
            a, b = local.score(txn), remote.score(txn)
            assert a.status == b.status, txn["transaction_id"]
            assert (a.response or {}) == (b.response or {})


def test_the_scenario_covers_every_kind_of_answer():
    local = LocalScorer(service())
    assert [local.score(t).status for t in SCENARIO] == [
        "scored", "duplicate", "conflict", "out_of_order", "unknown_customer", "invalid", "scored",
    ]  # fmt: skip


def test_readiness_is_reported():
    with Running(create_app(service())) as url:
        assert HttpScorer(url).ready()
    with Running(create_app(None)) as url:
        assert not HttpScorer(url).ready()
    assert not HttpScorer(f"http://127.0.0.1:{free_port()}", timeout=1).ready()


def test_a_service_with_no_model_or_no_listener_is_unavailable():
    with Running(create_app(None)) as url:
        out = HttpScorer(url).score(row(1, 1))
    assert out.status == UNAVAILABLE and out.detail == "503"
    gone = HttpScorer(f"http://127.0.0.1:{free_port()}", timeout=1).score(row(1, 1))
    assert gone.status == UNAVAILABLE


def test_unexpected_status_codes_are_errors_or_invalid_not_exceptions():
    app = FastAPI()

    @app.post("/v1/score")
    def score(body: dict):
        code = {"T000001": 500, "T000002": 413, "T000003": 418}[body["transaction_id"]]
        return JSONResponse({"oops": True}, status_code=code)

    with Running(app) as url:
        scorer = HttpScorer(url)
        assert scorer.score(row(1, 1)).status == ERROR
        assert scorer.score(row(2, 1)).status == INVALID
        assert scorer.score(row(3, 1)).status == ERROR


@pytest.mark.parametrize("url", ["file:///etc/passwd", "ftp://x", "localhost:8000"])
def test_only_http_urls_are_accepted(url):
    with pytest.raises(SmokeError):
        HttpScorer(url)
