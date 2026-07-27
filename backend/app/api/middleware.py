"""ASGI middleware stack (docs/04-backend-architecture.md §4.1) — four
layers, applied in the documented request-flow order:

1. `RequestIdMiddleware`     — mint/propagate `request_id`, bind to contextvars
2. `TracingMiddleware`       — open the request's OTel span
3. `AuthMiddleware`          — verify the demo JWT, attach `SessionUser`
4. `ErrorEnvelopeMiddleware` — map any raised `AppError`/unclassified
                                exception to the `{success,data,error,meta}`
                                envelope

`app/main.py`'s `create_app()` registers these via `add_middleware()` in
the *reverse* of the list above — Starlette's `add_middleware()` prepends
to its internal stack, so the last call registered ends up outermost (the
first to see an inbound request). See that module's comment for the
mechanical detail; this module only defines the four layers themselves.

Why `AuthMiddleware` renders its own `{success:false,...}` envelope
instead of raising for `ErrorEnvelopeMiddleware` to catch: ASGI middleware
nests strictly by call order — a middleware can only catch what happens
*inside* the call it makes onward, never something raised before that
call. Auth (3) runs before it calls onward into ErrorEnvelope (4); an
exception raised during Auth's own JWT verification, *before* that call,
therefore never reaches ErrorEnvelope's `except` clauses. Rather than
inverting the documented 1-2-3-4 flow order to make that plumbing work,
`AuthMiddleware` is self-contained: it already has the one thing it needs
(`error_envelope()`) to render its own 401 body directly, and
RequestId/Tracing (1-2) still wrap it either way — `request_id`/`trace_id`
land in the auth-failure log line regardless.

`validation_exception_handler` (registered via
`app.add_exception_handler(RequestValidationError, ...)` in
`app/main.py`, not one of the four `add_middleware()` calls) maps Pydantic
body-validation failures into the docs/13 §1.1-pinned `400
VALIDATION_SCHEMA` shape. It cannot live inside `ErrorEnvelopeMiddleware`:
Starlette's internal `ExceptionMiddleware` (where handlers registered via
`add_exception_handler` run) sits *inside* every user-added ASGI
middleware, wrapping the router directly — a validation failure during
route dependency resolution is resolved into a response there, before it
would ever reach `ErrorEnvelopeMiddleware`'s `except` clauses.
"""

from __future__ import annotations

from uuid import uuid4

import structlog
from fastapi.exceptions import RequestValidationError
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from app.api.deps import verify_demo_jwt
from app.api.errors import (
    AppError,
    AuthMissingTokenError,
    InternalError,
    RateLimitedError,
    ValidationSchemaError,
    error_envelope,
)
from app.obs import get_logger, get_tracer

log = get_logger(__name__)

# The one route that must work with no merchant JWT at all — a load
# balancer / container-orchestrator health probe can't be expected to
# carry one (task brief: "no auth, no envelope").
_UNAUTHENTICATED_PATHS = frozenset({"/health"})


class RequestIdMiddleware(BaseHTTPMiddleware):
    """docs/04 §4.1 #1. Reuses an inbound `X-Request-Id` if the caller
    already set one (proxy/gateway convention), mints one otherwise;
    always echoes it back so a client can correlate its own logs against
    this service's.
    """

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        request_id = request.headers.get("X-Request-Id") or uuid4().hex
        structlog.contextvars.bind_contextvars(request_id=request_id)
        try:
            response = await call_next(request)
        finally:
            # Cleared on every exit path, per logging.py's module
            # docstring ("MUST clear/reset... never only on the happy
            # path") — an unreset context can bleed this request_id into
            # an unrelated later log line on the same worker.
            structlog.contextvars.clear_contextvars()
        response.headers["X-Request-Id"] = request_id
        return response


class TracingMiddleware(BaseHTTPMiddleware):
    """docs/04 §4.1 #2. Opens one span per HTTP request so every log line
    emitted downstream — including from `AuthMiddleware`/
    `ErrorEnvelopeMiddleware` — has a `trace_id` to correlate against
    (logging.py's `_add_otel_ids` processor).

    `"http.request"` is not one of the canon `turn`/`context.build`/
    `llm.*` span names `app/obs/tracing.py` pins (those belong to the
    agent-loop turn a later batch adds) — this is the outer HTTP-request
    span those will nest inside once that code exists.
    """

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        tracer = get_tracer(__name__)
        with tracer.start_as_current_span("http.request") as span:
            # Plain `span.set_attribute`, not `safe_set_attribute`:
            # tracing.py's allowlist exists to keep raw *business* values
            # (a balance, an account number) off exported spans. An HTTP
            # method and route path are standard OTel semantic-convention
            # attributes every request span carries — neither business
            # data nor in that allowlist's domain.
            span.set_attribute("http.method", request.method)
            span.set_attribute("http.target", request.url.path)
            response = await call_next(request)
            span.set_attribute("http.status_code", response.status_code)
            return response


class AuthMiddleware(BaseHTTPMiddleware):
    """docs/04 §4.1 #3, docs/13 §1.2 — verify the demo JWT once, attach
    the `SessionUser` principal to `request.state.principal` for
    `app/api/deps.py`'s `get_principal` to hand to route handlers.
    """

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        if request.url.path in _UNAUTHENTICATED_PATHS:
            return await call_next(request)

        auth_header = request.headers.get("Authorization")
        if not auth_header or not auth_header.startswith("Bearer "):
            err = AuthMissingTokenError("Missing or malformed Authorization header")
            return JSONResponse(status_code=err.status_code, content=error_envelope(err))

        token = auth_header.removeprefix("Bearer ").strip()
        try:
            principal = verify_demo_jwt(token, request.app.state.settings)
        except AppError as err:
            return JSONResponse(status_code=err.status_code, content=error_envelope(err))

        request.state.principal = principal
        return await call_next(request)


class ErrorEnvelopeMiddleware(BaseHTTPMiddleware):
    """docs/04 §4.1 #4. Catches every `AppError` a route body or route
    dependency raises — business errors from the repositories, and
    `RateLimitedError` from `require_rate_limit` (app/api/deps.py) — plus
    anything unclassified, wrapped as `InternalError`. The real exception
    is logged server-side first (structlog, with the traceback); only the
    fixed generic message ever reaches the client
    (`InternalError`'s own docstring, app/api/errors.py).
    """

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        try:
            return await call_next(request)
        except AppError as err:
            response = JSONResponse(status_code=err.status_code, content=error_envelope(err))
            if isinstance(err, RateLimitedError):
                # docs/13 §1.1: "429 RATE_LIMITED ... Retry-After header set".
                response.headers["Retry-After"] = str(err.retry_after)
            return response
        except Exception as exc:
            log.error("api.unhandled_exception", error=str(exc), exc_info=True)
            internal_err = InternalError()
            return JSONResponse(
                status_code=internal_err.status_code, content=error_envelope(internal_err)
            )


async def validation_exception_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    """Maps FastAPI/Pydantic's default 422 `{"detail": [...]}` body into
    this app's envelope with the docs/13 §1.1-pinned `400
    VALIDATION_SCHEMA` code and a `details.fields` list of offenders.
    Registered via `app.add_exception_handler(RequestValidationError,
    ...)` in `app/main.py` — see this module's docstring for why it can't
    live inside `ErrorEnvelopeMiddleware` instead.
    """
    err = ValidationSchemaError(
        "Request body failed validation",
        details={"fields": [{"loc": list(e["loc"]), "msg": e["msg"]} for e in exc.errors()]},
    )
    return JSONResponse(status_code=err.status_code, content=error_envelope(err))


__all__ = [
    "AuthMiddleware",
    "ErrorEnvelopeMiddleware",
    "RequestIdMiddleware",
    "TracingMiddleware",
    "validation_exception_handler",
]
