"""After the stack is up: ask it, from outside, whether it behaves as a protected service.

Standard library only. Nothing here scores a transaction or changes the service's state.
"""

from __future__ import annotations

import http.client
import ssl
from pathlib import Path
from urllib.parse import urlparse

from mmrisk.api.smoke import _call, run_smoke, tls_context
from mmrisk.deploy.checks import Check, fail, ok

MARKER = "mmrisk"  # every metric the API exports starts with this


def verify(
    url: str, cafile: str | Path | None, score_key: str, metrics_key: str, timeout: float = 5.0
) -> list[Check]:
    base = url.rstrip("/")
    context = tls_context(base, str(cafile) if cafile else None)
    out: list[Check] = []

    def status(path, key=None, method="GET", body=None):
        try:
            return _call(method, base + path, body, timeout, key, context)
        except (OSError, ValueError) as exc:  # includes TLS verification errors
            return 0, f"{exc.__class__.__name__}: {exc}"

    def expect(name, got, want, extra=""):
        code, _ = got
        out.append(
            ok(name, f"{code}") if code == want else fail(name, f"{code}, wanted {want}. {extra}")
        )

    for name, passed, detail in run_smoke(
        base, timeout, score_key, str(cafile) if cafile else None
    ):
        out.append(ok(name, detail) if passed else fail(name, detail))
    expect("no key: /v1/model is refused", status("/v1/model"), 401, "the API is open")
    expect("no key: /docs is refused", status("/docs"), 401, "the API's own docs are open")
    expect("a wrong key is refused", status("/v1/model", "mmk_00000000_" + "A" * 43), 401)
    expect("a score key cannot read /metrics", status("/metrics", score_key), 403)
    code, text = status("/metrics", metrics_key)
    out.append(
        ok("a metrics key reads /metrics", f"{code}")
        if code == 200 and MARKER in text
        else fail("a metrics key reads /metrics", f"{code}")
    )
    expect("a metrics key cannot score or describe", status("/v1/model", metrics_key), 403)
    out.append(_plain_http_refused(base, timeout))
    return out


def _plain_http_refused(base: str, timeout: float) -> Check:
    """Speaking plain HTTP to the TLS port must not give an answer."""
    parts = urlparse(base)
    if parts.scheme != "https":
        return fail("TLS is on", "the URL is not https")
    conn = http.client.HTTPConnection(parts.hostname, parts.port or 443, timeout=timeout)
    try:
        conn.request("GET", "/health")
        code = conn.getresponse().status
        return fail("plain HTTP is refused", f"the TLS port answered {code} in clear text")
    except (OSError, http.client.HTTPException, ssl.SSLError):
        return ok("plain HTTP is refused", "no clear-text answer")
    finally:
        conn.close()
