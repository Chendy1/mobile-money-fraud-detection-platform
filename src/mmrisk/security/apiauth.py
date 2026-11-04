"""Authentication, rate limiting and auditing for the scoring API, as one ASGI middleware.

Order for a request: open paths pass; everything else needs `Authorization: Bearer <key>`; a client
that keeps failing is blocked for a while; a valid key must hold the role the route needs; each key
has a rate limit. Every refusal is written to the audit log (key id and route, never the key, the
body, or any identifier). Unknown paths also need a key, so the routes cannot be listed by probing.
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, field

from mmrisk.security.audit import AuditLog
from mmrisk.security.keys import KeyStore
from mmrisk.security.ratelimit import FailureLimiter, TokenBucket

OPEN = {("GET", "/health"), ("GET", "/ready")}
ROUTE_ROLES = {
    ("POST", "/v1/score"): {"score", "admin"},
    ("GET", "/v1/model"): {"score", "admin"},
    ("GET", "/metrics"): {"metrics", "admin"},
}
_REQUEST_ID = re.compile(r"^[A-Za-z0-9-]{1,64}$")
MAX_HEADER = 256  # a token is 56 characters; anything longer is not one


@dataclass
class Security:
    keys: KeyStore
    audit: AuditLog | None = None
    bucket: TokenBucket = field(default_factory=lambda: TokenBucket(600, 50))  # ASSUMED limits
    failures: FailureLimiter = field(default_factory=lambda: FailureLimiter(10, 60.0))  # ASSUMED

    def log(self, event: str, **fields) -> None:
        if self.audit is not None:
            self.audit.record(event, **fields)


def _bearer(headers: list[tuple[bytes, bytes]]) -> str | None:
    for name, value in headers:
        if name == b"authorization":
            text = value[:MAX_HEADER].decode("latin-1")
            scheme, _, token = text.partition(" ")
            return token.strip() if scheme.lower() == "bearer" else None
    return None


def _request_id(headers) -> str:
    for name, value in headers:
        if name == b"x-request-id":
            text = value.decode("latin-1")
            if _REQUEST_ID.match(text):
                return text
    return uuid.uuid4().hex


class AuthMiddleware:
    def __init__(self, app, security: Security):
        self.app, self.sec = app, security

    async def _refuse(self, send, status, code, message, rid, extra=None):
        body = json.dumps({"error": {"code": code, "message": message}}).encode()
        headers = [(b"content-type", b"application/json"), (b"x-request-id", rid.encode()),
                   (b"content-length", str(len(body)).encode())]  # fmt: skip
        if status == 401:
            headers.append((b"www-authenticate", b"Bearer"))
        headers += extra or []
        await send({"type": "http.response.start", "status": status, "headers": headers})
        await send({"type": "http.response.body", "body": body})

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        method, path = scope.get("method", ""), scope.get("path", "")
        if (method, path) in OPEN:
            return await self.app(scope, receive, send)
        sec = self.sec
        rid = _request_id(scope["headers"])
        client = (scope.get("client") or ("unknown", 0))[0]
        base = {"method": method, "route": path[:100], "client": client, "request_id": rid}

        if sec.failures.blocked(client):
            wait = int(sec.failures.retry_after(client)) + 1
            sec.log("blocked", status=429, **base)
            return await self._refuse(send, 429, "too_many_failures",
                                      "too many failed attempts; wait and try again", rid,
                                      [(b"retry-after", str(wait).encode())])  # fmt: skip
        token = _bearer(scope["headers"])
        key = sec.keys.check(token) if token else None
        if key is None:
            sec.failures.fail(client)
            sec.log("auth_failed", status=401, detail="no key" if not token else "bad key", **base)
            return await self._refuse(send, 401, "unauthorized", "a valid API key is required", rid)
        sec.failures.reset(client)
        allowed = ROUTE_ROLES.get((method, path))
        if allowed is None or key["role"] not in allowed:
            sec.log("forbidden", key_id=key["id"], role=key["role"], status=403, **base)
            return await self._refuse(send, 403, "forbidden", "this key may not do that", rid)
        ok, wait = sec.bucket.allow(key["id"])
        if not ok:
            sec.log("rate_limited", key_id=key["id"], role=key["role"], status=429, **base)
            return await self._refuse(send, 429, "rate_limited", "too many requests", rid,
                                      [(b"retry-after", str(int(wait) + 1).encode())])  # fmt: skip
        return await self.app(scope, receive, send)
