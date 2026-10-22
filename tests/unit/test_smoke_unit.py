"""The smoke command against real local servers: healthy, empty, broken and absent."""

import socket
import threading
import time
from datetime import date

import polars as pl
import pytest

pytest.importorskip("sklearn")
pytest.importorskip("fastapi")
uvicorn = pytest.importorskip("uvicorn")

from fastapi import FastAPI  # noqa: E402
from fastapi.responses import JSONResponse  # noqa: E402

from mmrisk.api import ScoringService  # noqa: E402
from mmrisk.api.app import create_app  # noqa: E402
from mmrisk.api.cli import main as cli  # noqa: E402
from mmrisk.api.smoke import SmokeError, check_url, run_smoke  # noqa: E402
from mmrisk.models.bundle import RiskModel  # noqa: E402
from mmrisk.models.calibration import PlattCalibrator  # noqa: E402
from mmrisk.models.costs import CostModel  # noqa: E402


class Stub:
    name = "stub"

    def score(self, df):
        return df["amount_kes"].to_numpy().astype(float) / 50_000.0 - 3.0


def service():
    n = 5
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


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Running:
    """A real uvicorn server on a free local port, in a thread."""

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


def failed(results):
    return [name for name, ok, _ in results if not ok]


def test_every_check_passes_against_a_healthy_service(capsys):
    with Running(create_app(service())) as url:
        assert failed(run_smoke(url)) == []
        assert cli(["smoke", "--url", url]) == 0
    out = capsys.readouterr().out
    assert out.count("PASS") == 6 and "smoke: PASS" in out  # five checks and the summary


def test_the_smoke_checks_do_not_change_the_service():
    svc = service()
    with Running(create_app(svc)) as url:
        run_smoke(url)
        run_smoke(url)
    assert svc.info()["history_transactions"] == 0 and not svc._seen  # nothing was recorded


def test_a_service_with_no_model_fails_readiness_and_model_checks(capsys):
    with Running(create_app(None)) as url:
        bad = failed(run_smoke(url))
        assert cli(["smoke", "--url", url]) == 1
    assert any("/ready" in n for n in bad) and any("/v1/model" in n for n in bad)
    assert "smoke: FAIL" in capsys.readouterr().out


def test_an_api_that_echoes_the_input_back_fails_the_echo_check():
    app = FastAPI()

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.get("/ready")
    def ready():
        return {"status": "ready"}

    @app.get("/v1/model")
    def model():
        return {"synthetic_data_only": True, "cost_model_ASSUMED": {}}

    @app.post("/v1/score")
    def score(body: dict):
        if body.get("amount_kes") == 0:
            return JSONResponse(
                {"error": {"code": "invalid_transaction", "message": str(body)}}, status_code=422
            )
        return JSONResponse(
            {"error": {"code": "unknown_customer", "message": "x"}}, status_code=404
        )

    with Running(app) as url:
        bad = failed(run_smoke(url))
    assert bad == ["POST an invalid transaction is 422 and the input is not echoed"]


def test_nothing_listening_fails_cleanly_without_raising(capsys):
    url = f"http://127.0.0.1:{free_port()}"
    results = run_smoke(url, timeout=1)
    assert len(failed(results)) == 5
    assert cli(["smoke", "--url", url, "--timeout", "1"]) == 1


@pytest.mark.parametrize(
    "url", ["file:///etc/passwd", "ftp://example.com", "http://", "localhost:8000", ""]
)
def test_only_http_urls_are_accepted(url, capsys):
    with pytest.raises(SmokeError):
        check_url(url)
    assert cli(["smoke", "--url", url]) == 2
    assert "error:" in capsys.readouterr().err
