"""OpenTelemetry wiring — one TracerProvider per process, one trace per
conversation turn (docs/04-backend-architecture.md §7.2, canon §4/§5).

Phase 2 has no Tempo collector running yet ([plan] Batch 1 task 1.4), so
`setup_observability` must degrade gracefully rather than assume a
collector is reachable:

- `settings.otel_exporter_otlp_endpoint` unset (the Phase-2 default) ->
  spans render to a `ConsoleSpanExporter` instead of failing to configure.
- Endpoint set but nothing listening -> `BatchSpanProcessor` buffers spans
  and drops them on export failure/overflow in a background thread; export
  never blocks the request path (docs/04 §7.5's documented failure mode).

Span *usage* — opening `turn`/`context.build`/`llm.*` spans and recording
their canon attributes — is `ConversationManager`/`ContextBuilder`/
`LLMRouter`'s job in later batches. This module only wires the provider,
hands out tracers, and pins the canon span names so later code never
hand-types a string that could drift from the dashboard's span-name
filters (docs/04 §7.3).

Two things deliberately NOT done here, flagged so later batches don't
assume otherwise:

- **httpx auto-instrumentation is not wired.** docs/04 §7.2 shows
  `HTTPXClientInstrumentor().instrument()` in the same code block as
  `setup_observability`, but `opentelemetry-instrumentation-httpx` isn't
  even a dependency yet (only `-fastapi` is). Whichever batch adds the
  first `httpx`-based provider call (OpenRouterLLM, task 2.3) should add
  the dependency + instrumentation call then — otherwise provider calls
  silently won't appear as child spans.
- **No attribute redaction/allowlist for span data.** `logging.py`'s
  `redact_pii` processor only scrubs structlog log lines — it has no
  reach into `span.set_attribute(...)` calls made elsewhere (ToolExecutor,
  LLMRouter, CostTracker in later batches), because logs and spans are
  structurally separate pipelines. Use `safe_set_attribute()` below for
  any span attribute a later batch adds — it's a narrow allowlist, not a
  scrubber, and it exists specifically so a raw wallet balance or account
  detail can't get attached to a span and exported verbatim (today, via
  `ConsoleSpanExporter`, straight to process stdout/stderr).
"""

from __future__ import annotations

import sys
from typing import Final

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter, SpanExporter
from opentelemetry.trace import Span
from opentelemetry.util.types import AttributeValue

from app.config import Settings

# Canon span names (docs/04 §7.2 / docs/05 §3.10). `tool.exec.<name>` is the
# one parametrized per-tool; use tool_span() for that instead of formatting
# the string by hand.
SPAN_TURN = "turn"
SPAN_CONTEXT_BUILD = "context.build"
SPAN_LLM_TTFT = "llm.ttft"
SPAN_LLM_TOTAL = "llm.total"
# Phase-3 voice-pipeline spans (docs/06 §4.1's frozen span table): the
# STT-tail finalization after the endpoint, and the first-sentence TTS
# time-to-first-byte. Added by the voice contract freeze (Phase-3 T1.1).
SPAN_STT_FINAL = "stt.final"
SPAN_TTS_FIRST_BYTE = "tts.first_byte"

# Attribute keys later batches are known to need (docs/04 §7.2's canon
# attribute list) — the allowlist `safe_set_attribute` enforces. Deliberately
# closed: a key not in this set is almost certainly either a raw business
# value (balance, amount, account/card detail) that doesn't belong on an
# exported span, or a new canon attribute that needs a one-line addition
# here, on purpose, rather than sailing through unreviewed.
_SAFE_SPAN_ATTRIBUTE_KEYS: Final[frozenset[str]] = frozenset(
    {
        "tier",
        "status",
        "latency_ms",
        "idempotency_key",
        "model",
        "cache_hit",
        "turn_no",
        "tool",
        "cost_estimated",
        # llm.total's canon token/cost attributes (docs/04 §7.2's frozen
        # span table) — added by task 4.3 for CostTracker.record_turn()
        # via exactly the deliberate-one-line-addition path this module's
        # docstring prescribes. Aggregate token counts and a computed USD
        # figure, never raw business values.
        "input_tokens",
        "output_tokens",
        "cost_usd",
        # Added by task 5.1 (ConversationManager's `turn` span): the id is
        # already on every structlog line for the same call, and the five
        # turn.* flags are booleans — nothing here can carry an account
        # fact or PII.
        "session_id",
        "turn.affirmed",
        "turn.failed",
        "turn.tool_loop_bound_hit",
        "turn.output_blocked",
        "turn.over_budget",
        # Phase-3 voice-pipeline attributes (docs/06 §4.1: `endpoint_ms`
        # rides the `turn` span because the endpoint decision is what
        # *opens* it; the stage timings ride their own spans). All are
        # millisecond durations, small counters, or booleans — nothing
        # here can carry transcript text, an account fact, or PII.
        "endpoint_ms",
        "stt_ms",
        "tts_ttfb_ms",
        "sentence_no",
        "interrupted",
        "is_endpoint",
        "turn_ms",
    }
)


def safe_set_attribute(span: Span, key: str, value: AttributeValue) -> None:
    """The only sanctioned way for later batches to attach an attribute to
    a span. Raises on any key outside `_SAFE_SPAN_ATTRIBUTE_KEYS` rather
    than silently dropping it — a raw wallet balance or account number
    attached here would be exported verbatim (Phase 2's default exporter
    is `ConsoleSpanExporter`, i.e. process stdout/stderr, with no
    redaction pass of its own; see module docstring).

    Adding a new attribute is a one-line addition to the allowlist above,
    done deliberately — not a call site silently working around this.
    """
    if key not in _SAFE_SPAN_ATTRIBUTE_KEYS:
        raise ValueError(
            f"span attribute {key!r} is not in the safe-attribute allowlist "
            "(app.obs.tracing._SAFE_SPAN_ATTRIBUTE_KEYS) — add it there "
            "deliberately if it's genuinely safe to export, never bypass "
            "this check at the call site"
        )
    span.set_attribute(key, value)


def setup_observability(settings: Settings, *, exporter: SpanExporter | None = None) -> None:
    """Configure the process-wide TracerProvider. Call once at startup
    (agent-api lifespan, a later batch). Never raises or blocks on a
    missing/unreachable collector — see module docstring.

    `exporter` is a testability seam only — production callers never pass
    it, letting the OTLP-vs-console selection below run as documented.
    Tests inject an `InMemorySpanExporter` here to assert on real exported
    spans instead of just "did this not raise" (`tests/obs/test_tracing.py`).
    """
    resolved_exporter = exporter if exporter is not None else (
        OTLPSpanExporter(endpoint=settings.otel_exporter_otlp_endpoint)
        if settings.otel_exporter_otlp_endpoint
        # out=sys.stderr, not the default stdout: structlog's JSON log
        # lines (logging.py) also go to stdout, and this exporter's
        # plain-text span dump would interleave with and corrupt that
        # one-JSON-object-per-line stream (e.g. under Loki's parser).
        else ConsoleSpanExporter(out=sys.stderr)
    )

    provider = TracerProvider(
        resource=Resource.create({"service.name": settings.otel_service_name})
    )
    provider.add_span_processor(BatchSpanProcessor(resolved_exporter))
    trace.set_tracer_provider(provider)


def get_tracer(name: str) -> trace.Tracer:
    """Everyday accessor: `tracer = get_tracer(__name__)`."""
    return trace.get_tracer(name)


def tool_span(name: str):
    """Context manager for a `tool.exec.<name>` span. Usage (ToolExecutor,
    a later batch):

        with tool_span("get_wallet_balance") as span:
            span.set_attribute("tier", "read")
            ...

    Attribute recording (`tier`, `status`, `latency_ms`, `idempotency_key`
    per docs/04 §7.2) is the caller's job — this only opens the
    correctly-named span.
    """
    return get_tracer(__name__).start_as_current_span(f"tool.exec.{name}")


__all__ = [
    "SPAN_CONTEXT_BUILD",
    "SPAN_LLM_TOTAL",
    "SPAN_LLM_TTFT",
    "SPAN_STT_FINAL",
    "SPAN_TTS_FIRST_BYTE",
    "SPAN_TURN",
    "get_tracer",
    "safe_set_attribute",
    "setup_observability",
    "tool_span",
]
