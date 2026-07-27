"""FastAPI app factory + lifespan (docs/04-backend-architecture.md §4).

This is the real app-factory module per the Phase-2 plan's Batch-3.3 file
ownership (`app/api/{deps,middleware,main}.py`). The Dockerfile's `CMD`
(already committed in Batch 1.2, frozen, not part of this task's file
list) hard-codes `uvicorn app.main:app` — not `app.api.main:app` — so
`app/main.py` (one directory up) re-exports `app`/`create_app` from here
as a thin shim; see that file's own docstring. Before either file
existed, `uvicorn app.main:app` failed with `ModuleNotFoundError`; that
gap is this task's whole reason for being.

Process singletons (settings, the pooled `httpx.AsyncClient`, the
SQLAlchemy engine/sessionmaker, the Redis client, the `OpenRouterLLM`
provider) are built once here and handed out via `app.state` — docs/04
§4's "Process/request scope" split. Nothing here opens a real socket
eagerly: `create_engine_and_sessionmaker` only builds a connection-pool
descriptor (app/data/engine.py's own docstring), and
`RedisClient.from_settings` similarly only builds a lazy client — both
are safe to construct in a test lifespan with no Postgres/Redis actually
running (tests/api/test_routes.py relies on exactly this).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from opentelemetry import trace

from app.api.middleware import (
    AuthMiddleware,
    ErrorEnvelopeMiddleware,
    RequestIdMiddleware,
    TracingMiddleware,
    validation_exception_handler,
)
from app.api.routes import api_router
from app.config import get_settings
from app.data.engine import create_engine_and_sessionmaker
from app.data.redis_client import RedisClient
from app.obs import configure_logging, setup_observability
from app.providers.openrouter import OpenRouterLLM


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """docs/04 §4's inline sketch, adapted to this repo's actual
    factories (`create_engine_and_sessionmaker`, `RedisClient.from_settings`)
    rather than the sketch's raw `async_sessionmaker(create_async_engine(...))`
    / `redis.from_url(...)` calls — those factories are what Batch 2 built
    and own the DSN-unwrapping (`SecretStr.get_secret_value()`) and
    engine-disposal concerns the inline sketch doesn't.
    """
    settings = get_settings()
    configure_logging(settings)
    setup_observability(settings)

    http = httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=2.0))
    engine, sessionmaker = create_engine_and_sessionmaker(settings)
    redis_client = RedisClient.from_settings(settings)

    app.state.settings = settings
    app.state.http = http
    app.state.engine = engine
    app.state.sessionmaker = sessionmaker
    app.state.redis = redis_client
    app.state.llm = OpenRouterLLM(http, settings)

    # TODO(known gap, flagged in review): once app/tools/registry.py
    # lands (task 3.2, a sibling PR not yet merged as of this PR — it
    # doesn't exist on `main` yet, so it can't be imported here without
    # coupling this PR's merge order to that one), add:
    #     from app.tools.registry import configure as configure_tools
    #     configure_tools(sessionmaker)
    # here, wiring the tool-invocation bridge to this SAME sessionmaker
    # rather than a second one. Until this line is added, calling any
    # registered tool raises a clear RuntimeError (by that module's own
    # design) rather than silently building an unmanaged connection pool.

    try:
        yield
    finally:
        await http.aclose()
        await redis_client.close()
        await engine.dispose()
        # Added after review (MEDIUM): without this, BatchSpanProcessor's
        # background thread can lose buffered spans on process exit, and
        # its exporter (ConsoleSpanExporter's stderr handle in Phase 2's
        # no-collector default, app/obs/tracing.py) is never cleanly
        # closed — directly observable as an "I/O operation on closed
        # file" export error after every test run. `setup_observability`
        # above always registers a real SDK TracerProvider before this
        # point in this lifespan, so `.shutdown()` here is always the
        # real thing, never the API's no-op ProxyTracerProvider.
        trace.get_tracer_provider().shutdown()  # type: ignore[attr-defined]


def create_app() -> FastAPI:
    app = FastAPI(
        title="Vyapar Agent API",
        version="0.2.0",
        lifespan=lifespan,
    )

    # Registered outermost-last on purpose: Starlette's `add_middleware()`
    # *prepends* to its internal stack (`self.user_middleware.insert(0,
    # ...)`), so whichever call happens last here ends up wrapping
    # everything else — the first layer an inbound request actually hits.
    # To reproduce docs/04 §4.1's documented request-flow order
    # (RequestId sees the request first, then Tracing, then Auth, then
    # ErrorEnvelope closest to the router), the calls below run in the
    # *reverse* of that order. Get this backwards and RequestIdMiddleware
    # silently ends up innermost instead of outermost — mirror
    # tests/api/test_middleware.py's stack-order test after touching this.
    app.add_middleware(ErrorEnvelopeMiddleware)
    app.add_middleware(AuthMiddleware)
    app.add_middleware(TracingMiddleware)
    app.add_middleware(RequestIdMiddleware)

    # Starlette's `add_exception_handler` typeshed wants a handler typed
    # to accept the base `Exception` (contravariance); `validation_
    # exception_handler`'s narrower `RequestValidationError` parameter is
    # the correct/safe type for what's actually registered against —
    # mirrors the existing `# type: ignore[arg-type]` precedent elsewhere
    # in this codebase (e.g. tests/conftest.py) for the same kind of
    # typeshed-vs-actual-usage mismatch.
    app.add_exception_handler(
        RequestValidationError, validation_exception_handler  # type: ignore[arg-type]
    )

    app.include_router(api_router)
    return app


app = create_app()

__all__ = ["app", "create_app", "lifespan"]
