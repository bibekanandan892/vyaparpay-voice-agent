"""Unit + light integration tests for app/api/middleware.py.

Each of the four middleware layers gets its own minimal FastAPI app (no
DB, no Redis, no lifespan) so its behavior is verified in isolation; a
final section assembles the real `create_app()` to prove the documented
request-flow order (docs/04 §4.1) actually holds once all four layers and
routing are wired together, not just individually.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
import pytest
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.testclient import TestClient
from opentelemetry import trace as otel_trace
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.util._once import Once
from pydantic import BaseModel

from app.api.errors import RateLimitedError, ResourceNotFoundError
from app.api.middleware import (
    AuthMiddleware,
    ErrorEnvelopeMiddleware,
    RequestIdMiddleware,
    TracingMiddleware,
    validation_exception_handler,
)
from app.config import Settings, get_settings
from app.obs import setup_observability

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


def _settings(**overrides: object) -> Settings:
    defaults: dict[str, object] = {
        "jwt_secret": "test-jwt-secret",
        "database_url": "postgresql+asyncpg://test:test@localhost/test",
        "openrouter_api_key": "test-openrouter-key",
    }
    defaults.update(overrides)
    return Settings(**defaults)  # type: ignore[arg-type]


def _make_token(secret: str, **claims: object) -> str:
    payload: dict[str, object] = {
        "sub": "usr_rajesh01",
        "name": "Rajesh Kumar",
        "exp": datetime.now(UTC) + timedelta(hours=24),
    }
    payload.update(claims)
    return jwt.encode(payload, secret, algorithm="HS256")


@pytest.fixture(autouse=True)
def _reset_tracer_provider() -> Any:
    """Same reset dance as tests/obs/test_tracing.py — OTel's
    `set_tracer_provider` is set-once, guarded by a private `Once()`, not
    just the provider reference itself."""
    otel_trace._TRACER_PROVIDER = None
    otel_trace._TRACER_PROVIDER_SET_ONCE = Once()
    yield
    otel_trace._TRACER_PROVIDER = None
    otel_trace._TRACER_PROVIDER_SET_ONCE = Once()


# --------------------------------------------------------------------------
# RequestIdMiddleware
# --------------------------------------------------------------------------


def _request_id_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(RequestIdMiddleware)

    @app.get("/ping")
    async def ping() -> dict[str, bool]:
        return {"ok": True}

    return app


def test_request_id_middleware_mints_a_header_when_absent() -> None:
    with TestClient(_request_id_app()) as client:
        resp = client.get("/ping")

    assert resp.status_code == 200
    assert resp.headers["X-Request-Id"]


def test_request_id_middleware_echoes_an_inbound_header() -> None:
    with TestClient(_request_id_app()) as client:
        resp = client.get("/ping", headers={"X-Request-Id": "req-abc-123"})

    assert resp.headers["X-Request-Id"] == "req-abc-123"


# --------------------------------------------------------------------------
# TracingMiddleware
# --------------------------------------------------------------------------


def _tracing_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(TracingMiddleware)

    @app.get("/ping")
    async def ping() -> dict[str, bool]:
        return {"ok": True}

    return app


def test_tracing_middleware_opens_an_http_request_span() -> None:
    exporter = InMemorySpanExporter()
    setup_observability(_settings(otel_exporter_otlp_endpoint=None), exporter=exporter)

    with TestClient(_tracing_app()) as client:
        resp = client.get("/ping")

    # `get_tracer_provider()`'s declared return type is the base `TracerProvider`
    # Protocol, which doesn't promise `force_flush` — `setup_observability`
    # always installs the concrete SDK `TracerProvider`, which does (same
    # pattern as tests/obs/test_tracing.py).
    otel_trace.get_tracer_provider().force_flush()  # type: ignore[attr-defined]
    spans = exporter.get_finished_spans()

    assert resp.status_code == 200
    assert len(spans) == 1
    span = spans[0]
    assert span.name == "http.request"
    assert span.attributes is not None
    assert span.attributes.get("http.method") == "GET"
    assert span.attributes.get("http.target") == "/ping"
    assert span.attributes.get("http.status_code") == 200


# --------------------------------------------------------------------------
# AuthMiddleware
# --------------------------------------------------------------------------


def _auth_app(settings: Settings) -> FastAPI:
    app = FastAPI()
    app.state.settings = settings
    app.add_middleware(AuthMiddleware)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/protected")
    async def protected(request: Request) -> dict[str, str]:
        return {"user_id": request.state.principal.user_id}

    return app


def test_auth_middleware_rejects_a_missing_token() -> None:
    with TestClient(_auth_app(_settings())) as client:
        resp = client.get("/protected")

    body = resp.json()
    assert resp.status_code == 401
    assert body["success"] is False
    assert body["error"]["code"] == "AUTH_MISSING_TOKEN"


def test_auth_middleware_rejects_a_malformed_header() -> None:
    with TestClient(_auth_app(_settings())) as client:
        resp = client.get("/protected", headers={"Authorization": "NotBearer abc"})

    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "AUTH_MISSING_TOKEN"


def test_auth_middleware_rejects_a_bad_signature() -> None:
    settings = _settings()
    token = _make_token("wrong-secret")

    with TestClient(_auth_app(settings)) as client:
        resp = client.get("/protected", headers={"Authorization": f"Bearer {token}"})

    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "AUTH_INVALID_TOKEN"


def test_auth_middleware_rejects_an_expired_token() -> None:
    settings = _settings()
    expired = datetime.now(UTC) - timedelta(hours=1)
    token = _make_token(settings.jwt_secret.get_secret_value(), exp=expired)

    with TestClient(_auth_app(settings)) as client:
        resp = client.get("/protected", headers={"Authorization": f"Bearer {token}"})

    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "AUTH_EXPIRED_TOKEN"


def test_auth_middleware_attaches_the_principal_for_a_valid_token() -> None:
    settings = _settings()
    token = _make_token(settings.jwt_secret.get_secret_value())

    with TestClient(_auth_app(settings)) as client:
        resp = client.get("/protected", headers={"Authorization": f"Bearer {token}"})

    assert resp.status_code == 200
    assert resp.json() == {"user_id": "usr_rajesh01"}


def test_auth_middleware_lets_health_through_with_no_token() -> None:
    with TestClient(_auth_app(_settings())) as client:
        resp = client.get("/health")

    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


# --------------------------------------------------------------------------
# ErrorEnvelopeMiddleware
# --------------------------------------------------------------------------


def _error_envelope_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(ErrorEnvelopeMiddleware)

    @app.get("/ok")
    async def ok() -> dict[str, bool]:
        return {"fine": True}

    @app.get("/boom-app-error")
    async def boom_app_error() -> dict[str, bool]:
        raise ResourceNotFoundError("No payment found for this id", details={"txn_id": "x"})

    @app.get("/boom-rate-limited")
    async def boom_rate_limited() -> dict[str, bool]:
        raise RateLimitedError(retry_after=42)

    @app.get("/boom-unclassified")
    async def boom_unclassified() -> dict[str, bool]:
        raise ValueError("some internal secret detail")

    return app


def test_error_envelope_middleware_passes_success_responses_through_unchanged() -> None:
    with TestClient(_error_envelope_app()) as client:
        resp = client.get("/ok")

    assert resp.status_code == 200
    assert resp.json() == {"fine": True}


def test_error_envelope_middleware_maps_app_error_to_the_envelope() -> None:
    with TestClient(_error_envelope_app()) as client:
        resp = client.get("/boom-app-error")

    body = resp.json()
    assert resp.status_code == 404
    assert body == {
        "success": False,
        "data": None,
        "error": {
            "code": "RESOURCE_NOT_FOUND",
            "message": "No payment found for this id",
            "details": {"txn_id": "x"},
        },
        "meta": None,
    }


def test_error_envelope_middleware_sets_retry_after_for_rate_limited() -> None:
    with TestClient(_error_envelope_app()) as client:
        resp = client.get("/boom-rate-limited")

    assert resp.status_code == 429
    assert resp.headers["Retry-After"] == "42"
    assert resp.json()["error"]["code"] == "RATE_LIMITED"


def test_error_envelope_middleware_wraps_unclassified_exceptions_as_internal() -> None:
    """The whole point of InternalError's fixed message (app/api/errors.py
    docstring): the raw exception text never reaches the client body."""
    with TestClient(_error_envelope_app(), raise_server_exceptions=False) as client:
        resp = client.get("/boom-unclassified")

    body = resp.json()
    assert resp.status_code == 500
    assert body["error"]["code"] == "INTERNAL"
    assert "some internal secret detail" not in resp.text


# --------------------------------------------------------------------------
# validation_exception_handler
# --------------------------------------------------------------------------


class _Body(BaseModel):
    amount_paise: int


def _validation_app() -> FastAPI:
    app = FastAPI()
    app.add_exception_handler(
        RequestValidationError, validation_exception_handler  # type: ignore[arg-type]
    )

    @app.post("/thing")
    async def create_thing(body: _Body) -> dict[str, bool]:
        return {"ok": True}

    return app


def test_validation_exception_handler_maps_to_400_validation_schema() -> None:
    with TestClient(_validation_app()) as client:
        resp = client.post("/thing", json={"amount_paise": "not-an-int"})

    body = resp.json()
    assert resp.status_code == 400
    assert body["success"] is False
    assert body["error"]["code"] == "VALIDATION_SCHEMA"
    assert body["error"]["details"]["fields"]


# --------------------------------------------------------------------------
# Full stack — proves the documented request-flow order (docs/04 §4.1)
# holds once RequestId/Tracing/Auth/ErrorEnvelope + routing are all wired
# together via app/api/main.py's create_app(), not just each layer alone.
# --------------------------------------------------------------------------


@pytest.fixture
def full_app_client(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setenv("JWT_SECRET", "test-jwt-secret")
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-openrouter-key")
    get_settings.cache_clear()

    from app.api.main import create_app

    with TestClient(create_app()) as client:
        yield client

    get_settings.cache_clear()


def test_full_stack_health_bypasses_auth_and_carries_a_request_id(
    full_app_client: TestClient,
) -> None:
    resp = full_app_client.get("/health")

    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
    assert resp.headers["X-Request-Id"]


def test_full_stack_missing_token_is_401_with_envelope_and_request_id(
    full_app_client: TestClient,
) -> None:
    """RequestIdMiddleware (#1) must still have run even though
    AuthMiddleware (#3) short-circuited the request before it ever
    reached routing — proof the four layers actually nest in the
    documented order, not just that each works alone."""
    resp = full_app_client.get("/v1/wallet")

    body = resp.json()
    assert resp.status_code == 401
    assert body["error"]["code"] == "AUTH_MISSING_TOKEN"
    assert resp.headers["X-Request-Id"]
