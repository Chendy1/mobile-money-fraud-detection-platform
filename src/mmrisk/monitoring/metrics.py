"""Prometheus metrics for the scoring API.

Labels are chosen so the number of time series stays small whatever a client sends: the endpoint
is the route template (not the URL), anything that matched no route is "unmatched", and the HTTP
method is one of a fixed few. Metrics never contain transaction ids, customer ids or amounts.
The registry belongs to one app (not the process-wide default), so tests and several apps in one
process do not collide.
"""

from __future__ import annotations

import time

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)

PREFIX = "mmrisk"
METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"})
ACTIONS = ("ALLOW", "REVIEW", "BLOCK")
LATENCY_BUCKETS = (0.001, 0.0025, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5)
PROBABILITY_BUCKETS = (0.001, 0.003, 0.01, 0.03, 0.1, 0.3, 0.5, 0.8, 0.95, 1.0)
UNMATCHED = "unmatched"
METRICS_PATH = "/metrics"


def metric_names() -> set[str]:
    """Every series name the API can expose (for checking dashboards and alert rules)."""
    names = set()
    for base, kind in (
        ("http_requests", "counter"),
        ("http_request_duration_seconds", "histogram"),
        ("transactions_scored", "counter"),
        ("duplicate_requests", "counter"),
        ("risk_probability", "histogram"),
        ("service_ready", "gauge"),
        ("history_transactions", "gauge"),
        ("customers", "gauge"),
        ("model_info", "gauge"),
    ):
        full = f"{PREFIX}_{base}"
        if kind == "counter":
            names.add(full + "_total")
        elif kind == "histogram":
            names.update({full + "_bucket", full + "_sum", full + "_count"})
        else:
            names.add(full)
    return names


class ApiMetrics:
    """Counters and histograms for one API app, plus gauges read from the service at scrape time."""

    def __init__(self, get_service):
        self.registry = CollectorRegistry()
        self._get_service = get_service
        r = self.registry
        self.requests = Counter(
            f"{PREFIX}_http_requests_total", "HTTP requests by route, method and status",
            ["endpoint", "method", "status"], registry=r,
        )  # fmt: skip
        self.latency = Histogram(
            f"{PREFIX}_http_request_duration_seconds", "Time to answer a request",
            ["endpoint"], buckets=LATENCY_BUCKETS, registry=r,
        )  # fmt: skip
        self.scored = Counter(
            f"{PREFIX}_transactions_scored_total",
            "Transactions scored for the first time, by action", ["action"], registry=r,
        )  # fmt: skip
        self.duplicates = Counter(
            f"{PREFIX}_duplicate_requests_total",
            "Requests for a transaction id that was already scored", registry=r,
        )  # fmt: skip
        self.probability = Histogram(
            f"{PREFIX}_risk_probability", "Calibrated fraud probability of scored transactions",
            buckets=PROBABILITY_BUCKETS, registry=r,
        )  # fmt: skip
        for action in ACTIONS:  # the series exist (at 0) before the first score
            self.scored.labels(action)
        ready = Gauge(f"{PREFIX}_service_ready", "1 when a model is loaded", registry=r)
        ready.set_function(lambda: 1.0 if self._get_service() is not None else 0.0)
        hist = Gauge(f"{PREFIX}_history_transactions", "Transactions in memory", registry=r)
        hist.set_function(lambda: self._info("history_transactions"))
        cust = Gauge(f"{PREFIX}_customers", "Customers known to the service", registry=r)
        cust.set_function(lambda: self._info("customers"))
        self.model = Gauge(
            f"{PREFIX}_model_info", "1 for the loaded model's feature version",
            ["feature_version"], registry=r,
        )  # fmt: skip
        svc = get_service()
        if svc is not None:
            self.model.labels(str(svc.model.feature_version)).set(1)

    def _info(self, key: str) -> float:
        svc = self._get_service()
        return float(svc.info()[key]) if svc is not None else 0.0

    def observe_request(self, endpoint: str, method: str, status: int, seconds: float) -> None:
        if endpoint == METRICS_PATH:
            return  # scraping the metrics is not traffic
        method = method if method in METHODS else "OTHER"
        self.requests.labels(endpoint, method, str(status)).inc()
        self.latency.labels(endpoint).observe(seconds)

    def observe_score(self, result: dict) -> None:
        if result.get("duplicate"):
            self.duplicates.inc()
            return
        self.scored.labels(result["action"]).inc()
        self.probability.observe(float(result["risk_probability"]))

    def render(self) -> tuple[bytes, str]:
        return generate_latest(self.registry), CONTENT_TYPE_LATEST


class MetricsMiddleware:
    """ASGI middleware (outermost, so it also sees refused and failed requests)."""

    def __init__(self, app, metrics: ApiMetrics):
        self.app, self.metrics = app, metrics

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        status = {"code": 500}

        async def capture(message):
            if message["type"] == "http.response.start":
                status["code"] = message["status"]
            await send(message)

        t0 = time.perf_counter()
        try:
            return await self.app(scope, receive, capture)
        finally:
            route = scope.get("route")
            endpoint = getattr(route, "path", None) or UNMATCHED
            self.metrics.observe_request(
                endpoint, scope.get("method", ""), status["code"], time.perf_counter() - t0
            )
