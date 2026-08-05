"""Unit tests for app.obs.tracing — pure in-process, no collector needed.

Two things this suite must get right (both fixed after PR review — see
git history — from an earlier version that only asserted "does not raise"):

1. `opentelemetry.trace.set_tracer_provider` is a set-*once* global — a
   second call is a silent no-op (only a logged warning), guarded by a
   private `Once()` object (`opentelemetry.trace._TRACER_PROVIDER_SET_ONCE`),
   not just the `_TRACER_PROVIDER` value itself. Resetting only
   `_TRACER_PROVIDER = None` between tests is NOT enough — the `Once()`
   guard must be replaced too, or every test after the first one silently
   configures nothing. `_reset_tracer_provider` below does both, and this
   was verified against the real opentelemetry-api behavior (not assumed)
   before being relied on here.
2. Real span assertions, not just "the context manager didn't raise" —
   `setup_observability(..., exporter=...)` (a testability seam, see its
   docstring) lets these tests inject `InMemorySpanExporter` and assert on
   actual exported span names, parent/child nesting, and attributes.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from opentelemetry import trace as otel_trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.util._once import Once

import app.obs.tracing as tracing_module
from app.config import Settings
from app.obs.tracing import (
    SPAN_CONTEXT_BUILD,
    SPAN_STT_FINAL,
    SPAN_TTS_FIRST_BYTE,
    SPAN_TURN,
    get_tracer,
    safe_set_attribute,
    setup_observability,
    tool_span,
)


def _settings(**overrides: object) -> Settings:
    """Minimal valid Settings for a test — only the three fields with no
    default (docs/04's fail-fast-on-missing-secrets rule) need supplying;
    kwargs win over any real .env, so this is hermetic."""
    defaults: dict[str, object] = {
        "jwt_secret": "test-secret",
        "database_url": "postgresql+asyncpg://user:pass@localhost/test",
        "openrouter_api_key": "test-key",
    }
    defaults.update(overrides)
    return Settings(**defaults)  # type: ignore[arg-type]


@pytest.fixture(autouse=True)
def _reset_tracer_provider(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Reset BOTH the cached provider and its `Once()` guard before and
    after every test — see module docstring point 1. Without resetting
    `_TRACER_PROVIDER_SET_ONCE`, `setup_observability` in every test after
    the first would silently configure nothing.

    **And shut the provider down, which resetting the globals does not
    do.** `setup_observability` with no OTLP endpoint builds a
    `ConsoleSpanExporter(out=sys.stderr)` behind a `BatchSpanProcessor`,
    and a `BatchSpanProcessor` runs a background worker thread. Dropping
    the global reference orphans that thread rather than stopping it: it
    stays alive holding the `sys.stderr` pytest had swapped in at the
    moment of capture, and keeps waking on its schedule for the rest of
    the session. Once pytest closes that captured stream, every flush
    raises `ValueError: I/O operation on closed file` from a thread no
    test owns.

    That was not theoretical. Before this shutdown, running `tests/obs`
    ahead of `tests/voice/test_peer_session.py` **hung the suite** — the
    aiortc loopback test never completed, and the full run stalled at
    ~91% indefinitely rather than failing. Because it is an ordering
    interaction, `tests/voice` alone passed in 3.5s and the bug stayed
    invisible until enough tests existed to reach that order by default.

    `TracerProvider.shutdown()` flushes and joins every span processor,
    so the thread is gone before pytest touches the stream.

    **Every provider the test builds is shut down, not just whichever one
    reached the global.** Shutting down `otel_trace._TRACER_PROVIDER` is
    not enough and that was measured, not guessed: a probe at
    `pytest_sessionfinish` still found two live
    `OtelBatchSpanRecordProcessor` threads after `tests/obs`, exactly one
    per `setup_observability(settings)` call that takes the console path.
    `set_tracer_provider` is set-once, so a second call in a session where
    the guard is already spent constructs the provider — spawning its
    worker thread — and is then a silent no-op, leaving the object
    unreachable from the global while its thread runs on. Recording every
    construction is the only way to reach those.
    """
    created: list[TracerProvider] = []
    real_provider_cls = tracing_module.TracerProvider

    class _RecordingTracerProvider(real_provider_cls):  # type: ignore[valid-type,misc]
        def __init__(self, *args: object, **kwargs: object) -> None:
            super().__init__(*args, **kwargs)
            created.append(self)

    monkeypatch.setattr(tracing_module, "TracerProvider", _RecordingTracerProvider)

    otel_trace._TRACER_PROVIDER = None
    otel_trace._TRACER_PROVIDER_SET_ONCE = Once()
    yield
    for provider in created:
        provider.shutdown()
    otel_trace._TRACER_PROVIDER = None
    otel_trace._TRACER_PROVIDER_SET_ONCE = Once()


def test_setup_observability_no_endpoint_does_not_raise() -> None:
    """No OTLP endpoint configured (the Phase-2 default) -> falls back to a
    ConsoleSpanExporter instead of crashing at startup."""
    settings = _settings(otel_exporter_otlp_endpoint=None)

    setup_observability(settings)


def test_setup_observability_unreachable_endpoint_does_not_raise() -> None:
    """An endpoint that's configured but has nothing listening (no Tempo in
    Phase 2) must not crash or hang setup — BatchSpanProcessor buffers and
    drops export failures in a background thread, off the request path."""
    settings = _settings(otel_exporter_otlp_endpoint="localhost:4317")

    setup_observability(settings)


def test_turn_and_tool_spans_export_with_correct_names_and_nesting() -> None:
    """The behavior later batches (ConversationManager, ToolExecutor)
    actually depend on: opening a `turn` span and a nested `tool.exec.*`
    span inside it produces two real exported spans, correctly parented —
    not just "the context manager didn't raise"."""
    exporter = InMemorySpanExporter()
    setup_observability(_settings(otel_exporter_otlp_endpoint=None), exporter=exporter)

    tracer = get_tracer(__name__)
    with tracer.start_as_current_span(SPAN_TURN):
        with tool_span("get_wallet_balance") as span:
            safe_set_attribute(span, "tier", "read")

    otel_trace.get_tracer_provider().force_flush()
    spans = exporter.get_finished_spans()

    assert {s.name for s in spans} == {"turn", "tool.exec.get_wallet_balance"}

    turn_span = next(s for s in spans if s.name == "turn")
    tool_span_obj = next(s for s in spans if s.name == "tool.exec.get_wallet_balance")
    assert tool_span_obj.parent is not None
    assert tool_span_obj.parent.span_id == turn_span.context.span_id
    assert tool_span_obj.attributes is not None
    assert tool_span_obj.attributes.get("tier") == "read"


def test_context_build_span_exports_independently_of_a_turn_span() -> None:
    """`context.build` (docs/06's 15/40ms budget line) is opened without
    necessarily being nested — confirm the span name is exactly what
    ContextBuilder (a later batch) will filter dashboards on."""
    exporter = InMemorySpanExporter()
    setup_observability(_settings(otel_exporter_otlp_endpoint=None), exporter=exporter)

    tracer = get_tracer(__name__)
    with tracer.start_as_current_span(SPAN_CONTEXT_BUILD):
        pass

    otel_trace.get_tracer_provider().force_flush()
    spans = exporter.get_finished_spans()

    assert len(spans) == 1
    assert spans[0].name == "context.build"


def test_safe_set_attribute_allows_a_known_key() -> None:
    exporter = InMemorySpanExporter()
    setup_observability(_settings(otel_exporter_otlp_endpoint=None), exporter=exporter)

    with tool_span("get_payment_status") as span:
        safe_set_attribute(span, "latency_ms", 12)

    otel_trace.get_tracer_provider().force_flush()
    recorded = exporter.get_finished_spans()[0]
    assert recorded.attributes is not None
    assert recorded.attributes.get("latency_ms") == 12


def test_safe_set_attribute_rejects_a_key_outside_the_allowlist() -> None:
    """The whole point of this helper (docs docstring, tracing.py): a raw
    business value must not be attachable to a span by accident."""
    exporter = InMemorySpanExporter()
    setup_observability(_settings(otel_exporter_otlp_endpoint=None), exporter=exporter)

    with tool_span("get_wallet_balance") as span:
        with pytest.raises(ValueError, match="not in the safe-attribute allowlist"):
            safe_set_attribute(span, "wallet_balance_paise", 1_845_000)


def test_phase3_voice_span_constants_pin_the_docs_06_span_names() -> None:
    """docs/06 §4.1's frozen span table — the names dashboards filter on;
    added by the Phase-3 voice contract freeze."""
    assert SPAN_STT_FINAL == "stt.final"
    assert SPAN_TTS_FIRST_BYTE == "tts.first_byte"


def test_safe_set_attribute_allows_the_phase3_voice_keys() -> None:
    """The eight Phase-3 additions to the allowlist (endpoint/stage
    timings, a transcript-length counter, sentence counter, endpoint/
    barge-in flags) must pass safe_set_attribute and land on the
    exported span."""
    exporter = InMemorySpanExporter()
    setup_observability(_settings(otel_exporter_otlp_endpoint=None), exporter=exporter)

    voice_attributes: dict[str, int | bool] = {
        "endpoint_ms": 250,
        "stt_ms": 80,
        "text_len": 42,
        "tts_ttfb_ms": 120,
        "sentence_no": 0,
        "interrupted": True,
        "is_endpoint": True,
        "turn_ms": 1_000,
    }
    tracer = get_tracer(__name__)
    with tracer.start_as_current_span(SPAN_TURN) as span:
        for key, value in voice_attributes.items():
            safe_set_attribute(span, key, value)

    otel_trace.get_tracer_provider().force_flush()
    recorded = exporter.get_finished_spans()[0]
    assert recorded.attributes is not None
    for key, value in voice_attributes.items():
        assert recorded.attributes.get(key) == value
