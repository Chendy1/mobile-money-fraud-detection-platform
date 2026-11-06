"""Black-box checks of a running API (standard library only).

None of the checks change the service's state: the requests it sends are rejected before any
transaction is scored (an invalid body, and a customer that does not exist). Use it against the
container or a local server: `python -m mmrisk.api smoke --url http://127.0.0.1:8000`.
"""

from __future__ import annotations

import json
import ssl
import urllib.error
import urllib.request
from urllib.parse import urlparse

MARKER = "SMOKE-MARKER-VALUE"
_VALID = {
    "transaction_id": "SMOKE000001",
    "customer_id": "C999999999",  # a well-formed id that no customer has
    "timestamp": "2025-06-01T12:00:00",
    "transaction_type": "SEND_MONEY",
    "amount_kes": 1000,
    "counterparty_type": "CUSTOMER",
    "counterparty_id": "C000002",
    "device_id": "D000001",
    "location_county": "Nairobi",
    "channel": "APP",
}


class SmokeError(ValueError):
    """The URL cannot be used."""


def check_url(url: str) -> str:
    parts = urlparse(url)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise SmokeError("url must start with http:// or https:// and name a host")
    return url.rstrip("/")


def tls_context(url: str, cafile: str | None) -> ssl.SSLContext | None:
    """A verifying context for https URLs (trusting `cafile` when given); None for http."""
    if urlparse(url).scheme != "https":
        return None
    return ssl.create_default_context(cafile=str(cafile) if cafile else None)


def _call(
    method: str, url: str, body: dict | None, timeout: float,
    key: str | None = None, context: ssl.SSLContext | None = None,
) -> tuple[int, str]:  # fmt: skip
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("content-type", "application/json")
    if key:
        req.add_header("authorization", f"Bearer {key}")
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=context) as resp:  # noqa: S310
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")


def _json(text: str) -> dict:
    try:
        out = json.loads(text)
    except ValueError:
        return {}
    return out if isinstance(out, dict) else {}


def run_smoke(
    url: str, timeout: float = 5.0, key: str | None = None, cafile: str | None = None
) -> list[tuple[str, bool, str]]:
    """Each result is (check name, passed, detail). `key` and `cafile` are for an API that has
    keys and TLS on (a key that may call /v1/score and /v1/model: score or admin)."""
    base = check_url(url)
    context = tls_context(base, cafile)
    results: list[tuple[str, bool, str]] = []

    def add(name, ok, detail=""):
        results.append((name, bool(ok), detail))

    def get(path):
        try:
            return _call("GET", base + path, None, timeout, key, context)
        except (OSError, ValueError) as exc:
            return 0, str(exc)

    def post(body):
        try:
            return _call("POST", base + "/v1/score", body, timeout, key, context)
        except (OSError, ValueError) as exc:
            return 0, str(exc)

    code, text = get("/health")
    add("GET /health is 200 and ok", code == 200 and _json(text).get("status") == "ok", f"{code}")
    code, text = get("/ready")
    add("GET /ready is 200 (a model is loaded)", code == 200, f"{code}")
    code, text = get("/v1/model")
    info = _json(text)
    add(
        "GET /v1/model says synthetic data and labels the costs as assumed",
        code == 200 and info.get("synthetic_data_only") is True and "cost_model_ASSUMED" in info,
        f"{code}",
    )
    code, text = post({**_VALID, "amount_kes": 0, "channel": MARKER})
    err = _json(text).get("error", {})
    add(
        "POST an invalid transaction is 422 and the input is not echoed",
        code == 422 and err.get("code") == "invalid_transaction" and MARKER not in text,
        f"{code}",
    )
    code, text = post(_VALID)
    add(
        "POST an unknown customer is 404",
        code == 404 and _json(text).get("error", {}).get("code") == "unknown_customer",
        f"{code}",
    )
    return results
