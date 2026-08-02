"""Root-level shared pytest fixtures (Phase-2 plan Batch 2, task 2.3).

Every later batch's test module is expected to pull from here instead of
re-inventing its own `Settings` boilerplate, respx route, or scripted LLM
double:

- `settings_factory` / `settings` — a minimal valid `Settings`
  (app/config.py fails fast on missing secrets at import time, so every
  test needs *some* concrete values for the three fields with no default)
  built from fixed test values, never a real `.env` — hermetic. Mirrors
  the `_settings(**overrides)` helper already used locally in
  tests/obs/test_logging.py; hoisted here so it isn't reinvented a third
  time.
- `openrouter_route` — a respx route pre-matched to `settings`'s
  `{openrouter_base_url}/chat/completions`, the one HTTP call
  `OpenRouterLLM.stream()` makes (docs/04 §4) and the one later
  `LLMRouter` tests will also need to stub. Built on top of respx's own
  `respx_mock` fixture (registered automatically by respx's `pytest11`
  plugin, see the installed `respx.plugin` module — this file does not
  redefine `respx_mock` itself, only adds the OpenRouter-specific route on
  top of it, since respx already fully supplies the base mock router).
- `fake_llm` — tests/fakes.py's `FakeLLM`, re-exported here so test
  modules can pull it straight from conftest instead of importing
  `tests.fakes` directly.
- `span_exporter` — a real `TracerProvider` wired to an
  `InMemorySpanExporter` via `setup_observability(settings, exporter=...)`
  (the testability seam its own docstring documents), for any test that
  needs to assert on actually-exported spans rather than just "the
  context manager didn't raise" — the pattern `tests/obs/test_tracing.py`
  established first. Configured session-wide, once, via
  `_session_span_exporter` (see that fixture's own docstring for why —
  module-level `tracer = get_tracer(__name__)` singletons, as
  `ContextBuilder`/`LLMRouter`/`ConversationManager` all use, cache the
  first real provider they resolve against and never re-check it, so a
  per-test provider would silently orphan spans after the first test);
  `span_exporter` itself just clears accumulated spans around each test.
  `test_tracing.py` does not use this — it tests `setup_observability`
  itself and needs the set-once global genuinely reset per test, always
  fetching `get_tracer(__name__)` fresh inside each test body.

Docs: docs/04-backend-architecture.md §4 (provider DI / lifespan pattern),
§9.4 (respx as the provider-mocking strategy).
"""

from __future__ import annotations

from collections.abc import Callable, Iterator

import pytest
import respx
from opentelemetry import trace as otel_trace
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from app.config import Settings
from app.obs.tracing import setup_observability
from tests.fakes import FakeLLM

SettingsFactory = Callable[..., Settings]


@pytest.fixture
def settings_factory() -> SettingsFactory:
    """Factory, not a bare instance, so a test that needs one field
    different (e.g. `openrouter_dialogue_fallbacks`, `env`) doesn't have to
    reconstruct all the required-field boilerplate — just
    `settings_factory(openrouter_dialogue_fallbacks="openai/gpt-5.1")`."""

    def _make(**overrides: object) -> Settings:
        defaults: dict[str, object] = {
            "jwt_secret": "test-jwt-secret",
            "database_url": "postgresql+asyncpg://test:test@localhost/test",
            "openrouter_api_key": "test-openrouter-key",
            "openrouter_base_url": "https://openrouter.test/api/v1",
        }
        defaults.update(overrides)
        return Settings(**defaults)  # type: ignore[arg-type]

    return _make


@pytest.fixture
def settings(settings_factory: SettingsFactory) -> Settings:
    """The common case — no field overrides needed."""
    return settings_factory()


@pytest.fixture(scope="session")
def _session_span_exporter() -> InMemorySpanExporter:
    """Configure the real process-wide `TracerProvider` exactly ONCE for
    the whole test session, wired to one `InMemorySpanExporter` —
    deliberately session-scoped, not per-test. Reasoning, verified by
    reading `opentelemetry.trace.ProxyTracer._tracer`'s source rather
    than assumed:

    Every module under test that opens spans (`ContextBuilder`,
    `LLMRouter`, `ConversationManager`) fetches its tracer ONCE at
    import time via a module-level `tracer = get_tracer(__name__)`. That
    returns a `ProxyTracer`, which resolves against whatever
    `_TRACER_PROVIDER` is set the FIRST time a span is actually opened
    through it — and then permanently caches that resolved `Tracer`
    (`self._real_tracer`), never re-checking `_TRACER_PROVIDER` again.
    Re-creating the provider on every test (an earlier version of this
    fixture did exactly that, modeled naively on
    `tests/obs/test_tracing.py`'s per-test reset) silently orphans every
    span opened by one of those cached module tracers after the first
    test that exercises it — the spans are real and correctly closed,
    they just export to the FIRST test's now-discarded exporter, so
    every later test sees an empty list and looks like spans were never
    opened at all. Setting the provider up once, session-wide, matches
    how `setup_observability` actually runs in production (once, at
    app-lifespan startup, before any module's tracer is first used) and
    sidesteps the caching entirely.

    `tests/obs/test_tracing.py` does not use this fixture: it tests
    `setup_observability` itself (including calling it more than once
    under different settings), so it needs the set-once global truly
    reset per test and always fetches `get_tracer(__name__)` fresh,
    inside each test body, after its own `setup_observability` call —
    never through a cached module-level tracer.
    """
    exporter = InMemorySpanExporter()
    settings = Settings(
        jwt_secret="test-jwt-secret",
        database_url="postgresql+asyncpg://test:test@localhost/test",
        openrouter_api_key="test-openrouter-key",
    )
    setup_observability(settings, exporter=exporter)
    return exporter


@pytest.fixture
def span_exporter(
    _session_span_exporter: InMemorySpanExporter,
) -> Iterator[InMemorySpanExporter]:
    """Per-test view onto the session-wide tracer (see
    `_session_span_exporter`): drains and clears accumulated spans before
    AND after, so one test never sees another's spans, without
    re-creating the provider (which would orphan cached module tracers).

    `force_flush()` before each `.clear()`, not just `.clear()` alone,
    matters because the real `BatchSpanProcessor` `setup_observability`
    always installs (module docstring) batches spans in a background
    thread — a span opened by some OTHER test that shares this same
    session-wide provider (e.g. `ConversationManager`'s `turn` span,
    opened by `tests/agent/test_conversation_manager.py`, which doesn't
    reference this fixture at all) can sit queued, unexported, until
    the next `force_flush()` anywhere flushes it — which would otherwise
    land inside THIS test's span list instead of its own. Found by
    running the full suite, not just this file in isolation.
    """
    otel_trace.get_tracer_provider().force_flush()
    _session_span_exporter.clear()
    yield _session_span_exporter
    otel_trace.get_tracer_provider().force_flush()
    _session_span_exporter.clear()


@pytest.fixture
def openrouter_route(respx_mock: respx.MockRouter, settings: Settings) -> respx.Route:
    """Pre-registered POST route for OpenRouterLLM's one HTTP call
    (`{openrouter_base_url}/chat/completions`). Nothing is stubbed by
    default — each test configures the response via
    `openrouter_route.mock(return_value=...)` or `.side_effect(...)`; an
    un-configured call still fails loudly via respx's `assert_all_mocked`
    default rather than silently reaching the real network.
    """
    return respx_mock.post(f"{settings.openrouter_base_url}/chat/completions")


@pytest.fixture
def fake_llm() -> FakeLLM:
    """A fresh, unscripted `FakeLLM` per test — see tests/fakes.py."""
    return FakeLLM()


__all__ = ["SettingsFactory"]
