"""The API's Prometheus metrics: what is counted, how labels stay bounded, what is never exposed."""

import re

import pytest

pytest.importorskip("sklearn")
pytest.importorskip("pydantic")
pytest.importorskip("fastapi")
pytest.importorskip("httpx")
prometheus_client = pytest.importorskip("prometheus_client")

from fastapi.testclient import TestClient  # noqa: E402
from test_api_unit import customers, stub_model, txn, wire  # noqa: E402

from mmrisk.api import ScoringService  # noqa: E402
from mmrisk.api.app import create_app  # noqa: E402
from mmrisk.monitoring.metrics import ApiMetrics, metric_names  # noqa: E402


def make_client(**kw):
    svc = ScoringService(stub_model(), customers())
    return TestClient(create_app(svc, **kw), raise_server_exceptions=False), svc


def scrape(client) -> str:
    r = client.get("/metrics")
    assert r.status_code == 200
    return r.text


def value(text: str, series: str) -> float:
    """The value of one exact series line, for example mmrisk_scored_total{action="ALLOW"}."""
    for line in text.splitlines():
        if line.startswith(series + " "):
            return float(line.rsplit(" ", 1)[1])
    raise AssertionError(f"{series} not in the output")


def present(text: str, series: str) -> bool:
    return any(line.startswith(series + " ") for line in text.splitlines())


def test_the_endpoint_serves_prometheus_text():
    client, _ = make_client()
    r = client.get("/metrics")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/plain")
    assert "# TYPE mmrisk_http_requests_total counter" in r.text


def test_every_documented_name_is_exposed_and_nothing_else_is():
    client, _ = make_client()
    client.post("/v1/score", json=wire(txn()))
    text = scrape(client)
    names = set(re.findall(r"^(mmrisk_[a-z_]+)[{ ]", text, re.M))
    names = {n for n in names if not n.endswith("_created")}
    assert names == metric_names()


def test_the_documented_names_match_the_registry():
    m = ApiMetrics(lambda: None)
    collected = {s.name for family in m.registry.collect() for s in family.samples}
    collected = {n for n in collected if not n.endswith("_created")}
    # labelled counters have no sample until used; the registry's families give their base names
    families = {f.name for f in m.registry.collect()}
    assert {n.removesuffix("_total") for n in metric_names() if n.endswith("_total")} <= families
    assert collected <= metric_names()


def test_a_scored_transaction_is_counted_by_action_and_in_the_probability_histogram():
    client, _ = make_client()
    r = client.post("/v1/score", json=wire(txn()))
    action = r.json()["action"]
    text = scrape(client)
    assert value(text, f'mmrisk_transactions_scored_total{{action="{action}"}}') == 1
    assert value(text, "mmrisk_risk_probability_count") == 1
    other = [a for a in ("ALLOW", "REVIEW", "BLOCK") if a != action]
    for a in other:
        assert value(text, f'mmrisk_transactions_scored_total{{action="{a}"}}') == 0
    assert (
        value(text, 'mmrisk_http_requests_total{endpoint="/v1/score",method="POST",status="200"}')
        == 1
    )
    assert value(text, 'mmrisk_http_request_duration_seconds_count{endpoint="/v1/score"}') == 1


def test_a_repeated_id_is_counted_as_a_repeat_not_as_a_new_score():
    client, _ = make_client()
    body = wire(txn())
    client.post("/v1/score", json=body)
    again = client.post("/v1/score", json=body)
    assert again.json()["duplicate"] is True
    text = scrape(client)
    scored = sum(
        value(text, f'mmrisk_transactions_scored_total{{action="{a}"}}')
        for a in ("ALLOW", "REVIEW", "BLOCK")
    )
    assert scored == 1 and value(text, "mmrisk_duplicate_requests_total") == 1
    assert value(text, "mmrisk_risk_probability_count") == 1


def test_refused_requests_are_counted_with_their_status():
    client, _ = make_client()
    client.post("/v1/score", json={})  # 422
    client.post("/v1/score", json=wire(txn(1, "C999999")))  # 404
    client.post("/v1/score", json=wire(txn(2, minutes=10)))
    client.post("/v1/score", json=wire(txn(3, minutes=1)))  # 409, older than the last
    client.post("/v1/score", content=b"x" * 9000)  # 413, refused before the app reads it
    text = scrape(client)
    for status in ("422", "404", "409"):
        series = (
            f'mmrisk_http_requests_total{{endpoint="/v1/score",method="POST",status="{status}"}}'
        )
        assert value(text, series) == 1, status
    assert (
        value(text, 'mmrisk_http_requests_total{endpoint="unmatched",method="POST",status="413"}')
        == 1
    )
    # refused requests are not scored
    assert value(text, "mmrisk_risk_probability_count") == 1


def test_an_unexpected_error_is_counted_as_a_500():
    client, svc = make_client()
    app = client.app

    @app.get("/boom")
    def boom():
        raise RuntimeError("secret detail")

    client.get("/boom")
    text = scrape(client)
    assert (
        value(text, 'mmrisk_http_requests_total{endpoint="/boom",method="GET",status="500"}') == 1
    )
    assert "secret detail" not in text


def test_label_values_stay_bounded_whatever_clients_send():
    client, _ = make_client()
    for i in range(60):
        client.get(f"/random/{i}/path?x={i}")
    for method in ("FOO", "BAR", "LOCK", "TRACE"):
        client.request(method, "/health")
    text = scrape(client)
    series = [ln for ln in text.splitlines() if ln.startswith("mmrisk_http_requests_total{")]
    assert len(series) <= 4
    assert (
        value(text, 'mmrisk_http_requests_total{endpoint="unmatched",method="GET",status="404"}')
        == 60
    )
    assert 'method="FOO"' not in text and 'method="OTHER"' in text
    assert "/random/" not in text


def test_scraping_is_not_counted_as_traffic():
    client, _ = make_client()
    for _ in range(3):
        client.get("/metrics")
    assert 'endpoint="/metrics"' not in scrape(client)


def test_no_identifier_or_amount_is_exposed():
    client, _ = make_client()
    body = wire(txn(7, "C000007"))
    client.post("/v1/score", json=body)
    text = scrape(client)
    for secret in ("T000007", "C000007", str(body["amount_kes"]), body["location_county"]):
        assert secret not in text, secret


def test_gauges_follow_the_service():
    client, svc = make_client()
    text = scrape(client)
    assert value(text, "mmrisk_service_ready") == 1
    assert value(text, "mmrisk_customers") == 40
    assert value(text, 'mmrisk_model_info{feature_version="1.0"}') == 1
    before = value(text, "mmrisk_history_transactions")
    client.post("/v1/score", json=wire(txn()))
    assert value(scrape(client), "mmrisk_history_transactions") == before + 1


def test_without_a_model_the_service_is_reported_not_ready():
    client = TestClient(create_app(None))
    text = scrape(client)
    assert value(text, "mmrisk_service_ready") == 0
    assert value(text, "mmrisk_history_transactions") == 0
    assert not present(text, 'mmrisk_model_info{feature_version="1.0"}')
    client.get("/ready")
    assert (
        value(
            scrape(client),
            'mmrisk_http_requests_total{endpoint="/ready",method="GET",status="503"}',
        )
        == 1
    )


def test_metrics_can_be_switched_off():
    client, _ = make_client(metrics=False)
    assert client.get("/metrics").status_code == 404
    assert client.app.state.metrics is None
    assert client.post("/v1/score", json=wire(txn())).status_code == 200


def test_each_app_has_its_own_registry():
    a, _ = make_client()
    b, _ = make_client()
    a.post("/v1/score", json=wire(txn()))
    assert value(scrape(a), "mmrisk_risk_probability_count") == 1
    assert value(scrape(b), "mmrisk_risk_probability_count") == 0


def test_the_metrics_endpoint_is_not_in_the_published_api_description():
    client, _ = make_client()
    assert "/metrics" not in client.get("/openapi.json").json()["paths"]
