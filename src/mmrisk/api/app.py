"""The HTTP layer: validation, error shapes, request ids and size limits around `ScoringService`.

Local use only. There is no authentication and no TLS here; both belong to the security phase.
The service binds to the loopback address unless told otherwise (see the CLI).
"""

from __future__ import annotations

import logging
import re
import time
import uuid

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from mmrisk.api.schemas import ErrorOut, ModelInfo, ScoreOut, TransactionIn
from mmrisk.api.service import (
    ConflictError,
    ScoringService,
    UnknownCustomerError,
)

log = logging.getLogger("mmrisk.api")

MAX_BODY_BYTES = 8 * 1024  # a transaction is about 400 bytes
_REQUEST_ID = re.compile(r"^[A-Za-z0-9-]{1,64}$")

_ERRORS = {
    404: {"model": ErrorOut, "description": "Unknown customer"},
    409: {"model": ErrorOut, "description": "Out of order, or same id with other content"},
    413: {"model": ErrorOut, "description": "Body too large"},
    422: {"model": ErrorOut, "description": "Invalid transaction"},
    503: {"model": ErrorOut, "description": "No model loaded"},
}


def _error(status: int, code: str, message: str, request_id: str | None = None) -> JSONResponse:
    headers = {"X-Request-ID": request_id} if request_id else None
    return JSONResponse(
        {"error": {"code": code, "message": message}}, status_code=status, headers=headers
    )


class _LimitBody:
    """ASGI middleware: refuse a request body larger than `limit` before the app reads it."""

    def __init__(self, app, limit: int):
        self.app, self.limit = app, limit

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        declared = dict(scope["headers"]).get(b"content-length")
        too_big = False
        if declared is not None:
            try:
                too_big = int(declared) > self.limit
            except ValueError:
                too_big = True
        buffered, size = [], 0
        while not too_big:
            message = await receive()
            buffered.append(message)
            if message["type"] != "http.request":
                break
            size += len(message.get("body", b""))
            if size > self.limit:
                too_big = True
            if not message.get("more_body", False):
                break
        if too_big:
            response = _error(413, "body_too_large", f"body larger than {self.limit} bytes")
            return await response(scope, receive, send)
        replay = iter(buffered)

        async def replayed():
            try:
                return next(replay)
            except StopIteration:
                return await receive()

        return await self.app(scope, replayed, send)


def create_app(service: ScoringService | None, docs: bool = True) -> FastAPI:
    app = FastAPI(
        title="Mobile money transaction risk scoring (synthetic data only)",
        version="0.1.0",
        description="Local simulation. Not affiliated with Safaricom or M-Pesa. No authentication.",
        docs_url="/docs" if docs else None,
        redoc_url=None,
        openapi_url="/openapi.json" if docs else None,
    )
    app.state.service = service

    @app.middleware("http")
    async def request_id_and_timing(request: Request, call_next):
        incoming = request.headers.get("x-request-id", "")
        rid = incoming if _REQUEST_ID.match(incoming) else uuid.uuid4().hex
        t0 = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:  # never show internals to the caller
            log.exception("unhandled error request_id=%s path=%s", rid, request.url.path)
            return _error(500, "internal_error", "internal error", rid)
        ms = (time.perf_counter() - t0) * 1000
        response.headers["X-Request-ID"] = rid
        response.headers["X-Process-Time-Ms"] = f"{ms:.2f}"
        log.info("%s %s %s %.1fms request_id=%s", request.method, request.url.path,
                 response.status_code, ms, rid)  # fmt: skip
        return response

    app.add_middleware(_LimitBody, limit=MAX_BODY_BYTES)

    @app.exception_handler(RequestValidationError)
    async def invalid(request: Request, exc: RequestValidationError):
        # Field paths and messages only: the submitted values are not echoed back.
        fields = "; ".join(
            f"{'.'.join(str(p) for p in e['loc'][1:]) or 'body'}: {e['msg']}" for e in exc.errors()
        )
        return _error(422, "invalid_transaction", fields[:1000])

    @app.exception_handler(UnknownCustomerError)
    async def unknown(request: Request, exc: UnknownCustomerError):
        return _error(404, exc.code, str(exc))

    @app.exception_handler(ConflictError)
    async def conflict(request: Request, exc: ConflictError):
        return _error(409, exc.code, str(exc))

    def ready() -> ScoringService | None:
        return app.state.service

    @app.get("/health")
    def health():
        """Liveness: the process is up."""
        return {"status": "ok"}

    @app.get("/ready", responses={503: _ERRORS[503]})
    def readiness():
        """Readiness: a model is loaded and can score."""
        svc = ready()
        if svc is None:
            return _error(503, "not_ready", "no model loaded")
        return {"status": "ready", "feature_version": svc.model.feature_version}

    @app.get("/v1/model", response_model=ModelInfo, responses={503: _ERRORS[503]})
    def model_info():
        svc = ready()
        if svc is None:
            return _error(503, "not_ready", "no model loaded")
        return svc.info()

    @app.post("/v1/score", response_model=ScoreOut, responses=_ERRORS)
    def score(txn: TransactionIn):
        """Score one transaction and record it as history for the customer's later ones."""
        svc = ready()
        if svc is None:
            return _error(503, "not_ready", "no model loaded")
        return svc.score(txn.model_dump())

    return app
