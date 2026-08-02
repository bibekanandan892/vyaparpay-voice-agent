"""Unit tests for `LLMRouter` (app/agent/llm_router.py) — pure in-process
via `FakeLLM` (tests/fakes.py), no HTTP mocking (docs/04 §9.4's fake-vs-
respx split; the provider's own wire behavior is tests/providers/
test_openrouter.py's job).

Fragment sequences below mirror real OpenAI-compatible streaming traces:
`tool_calls[i].index` keys the fragments, `id`/`function.name` arrive only
on an index's first fragment, and `function.arguments` is a JSON string
split across chunks — the exact raw shape test_openrouter.py pins as
passing through the provider un-stitched.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any, cast

import orjson
import pytest
from opentelemetry import trace as otel_trace
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from app.agent.llm_router import LLMRouter
from app.config import Settings
from app.domain.interfaces import LLMProvider
from app.domain.types import (
    LLMEvent,
    Message,
    ModelTier,
    Role,
    TaskKind,
    TokenEvent,
    ToolCall,
    ToolCallsEvent,
    UsageEvent,
)
from tests.conftest import SettingsFactory
from tests.fakes import FakeLLM

_USER_MESSAGE = [Message(role=Role.USER, content="check my balance")]


def _fragment_event(
    index: int,
    *,
    call_id: str | None = None,
    name: str | None = None,
    arguments: str | None = None,
    content: str | None = None,
) -> TokenEvent:
    """One provider TokenEvent carrying a single tool_calls fragment, in
    the real OpenAI-compatible delta shape."""
    fragment: dict[str, Any] = {"index": index}
    if call_id is not None:
        fragment["id"] = call_id
        fragment["type"] = "function"
    function: dict[str, Any] = {}
    if name is not None:
        function["name"] = name
    if arguments is not None:
        function["arguments"] = arguments
    if function:
        fragment["function"] = function
    delta: dict[str, Any] = {"tool_calls": [fragment]}
    if content is not None:
        delta["content"] = content
    return TokenEvent(delta=delta)


def _usage_event(model: str = "anthropic/claude-sonnet-5") -> UsageEvent:
    return UsageEvent(
        model=model,
        usage={"prompt_tokens": 120, "completion_tokens": 8, "cached_tokens": 0},
    )


async def _collect(stream: AsyncIterator[LLMEvent]) -> list[LLMEvent]:
    return [event async for event in stream]


def _router(provider: object, settings: Settings) -> LLMRouter:
    """Construct the router around an async-generator provider double.

    mypy reads `LLMProvider.stream`'s `async def ... -> AsyncIterator`
    Protocol spelling as coroutine-returning, so async-generator
    implementations (FakeLLM, `_StallingLLM` — and `OpenRouterLLM`
    itself) don't structurally match under mypy even though they are
    exactly what the codebase ships; `LLMRouter._open_stream` accepts
    both shapes at runtime. One documented cast here beats one per call
    site."""
    return LLMRouter(cast(LLMProvider, provider), settings)


class _StallingLLM:
    """`LLMProvider` double whose first N `stream()` calls stall forever
    (until the router's TTFT deadline cancels them), then replay scripted
    events — drives the deadline/one-retry path. `cancelled_streams`
    counts stalled attempts the router actually tore down."""

    def __init__(self, events: list[LLMEvent], *, stall_attempts: int) -> None:
        self._events = events
        self._stall_attempts = stall_attempts
        self.call_count = 0
        self.cancelled_streams = 0

    async def stream(
        self,
        messages: list[dict[str, Any]],
        *,
        models: list[str],
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[LLMEvent]:
        self.call_count += 1
        if self.call_count <= self._stall_attempts:
            try:
                await asyncio.sleep(3600)
            finally:
                self.cancelled_streams += 1
        else:
            for event in self._events:
                yield event


# ----------------------------------------------------------------------
# route()
# ----------------------------------------------------------------------


def test_route_maps_dialogue_task_to_dialogue_tier(
    fake_llm: FakeLLM, settings: Settings
) -> None:
    router = _router(fake_llm, settings)

    assert router.route(TaskKind.DIALOGUE) is ModelTier.DIALOGUE


def test_route_maps_utility_task_to_utility_tier(
    fake_llm: FakeLLM, settings: Settings
) -> None:
    router = _router(fake_llm, settings)

    assert router.route(TaskKind.UTILITY) is ModelTier.UTILITY


# ----------------------------------------------------------------------
# Wire adaptation (interface pin #1) + tier -> models array selection
# ----------------------------------------------------------------------


async def test_stream_adapts_domain_messages_to_wire_dicts(
    fake_llm: FakeLLM, settings: Settings
) -> None:
    # Arrange — a full round: system, user, assistant proposing a call,
    # and the tool result, exercising every to_wire() branch.
    messages = [
        Message(role=Role.SYSTEM, content="You are Asha."),
        Message(role=Role.USER, content="check my balance"),
        Message(
            role=Role.ASSISTANT,
            tool_calls=(
                ToolCall(id="call_abc123", name="get_wallet_balance", arguments={}),
            ),
        ),
        Message(
            role=Role.TOOL,
            content='{"ok":true,"data":{"balance_paise":1845000}}',
            tool_call_id="call_abc123",
            name="get_wallet_balance",
        ),
    ]
    fake_llm.script_turn(TokenEvent(delta={"content": "ok"}))
    router = _router(fake_llm, settings)

    # Act
    await _collect(router.stream(messages, tier=ModelTier.DIALOGUE))

    # Assert — exact OpenAI-compatible wire dicts, arguments as a JSON
    # *string* (Message.to_wire()'s documented serialization point).
    assert fake_llm.calls[0].messages == [
        {"role": "system", "content": "You are Asha."},
        {"role": "user", "content": "check my balance"},
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": "call_abc123",
                    "type": "function",
                    "function": {"name": "get_wallet_balance", "arguments": "{}"},
                }
            ],
        },
        {
            "role": "tool",
            "content": '{"ok":true,"data":{"balance_paise":1845000}}',
            "tool_call_id": "call_abc123",
            "name": "get_wallet_balance",
        },
    ]


async def test_dialogue_tier_sends_primary_plus_fallback_models_array(
    fake_llm: FakeLLM, settings_factory: SettingsFactory
) -> None:
    settings = settings_factory(
        openrouter_dialogue_fallbacks="openai/gpt-5.1, google/gemini-3-pro"
    )
    fake_llm.script_turn(TokenEvent(delta={"content": "hi"}))
    router = _router(fake_llm, settings)

    await _collect(router.stream(_USER_MESSAGE, tier=ModelTier.DIALOGUE))

    assert fake_llm.calls[0].models == [
        settings.openrouter_dialogue_model,
        "openai/gpt-5.1",
        "google/gemini-3-pro",
    ]


async def test_utility_tier_sends_single_utility_model(
    fake_llm: FakeLLM, settings: Settings
) -> None:
    fake_llm.script_turn(TokenEvent(delta={"content": "hi"}))
    router = _router(fake_llm, settings)

    await _collect(router.stream(_USER_MESSAGE, tier=ModelTier.UTILITY))

    assert fake_llm.calls[0].models == [settings.openrouter_utility_model]


async def test_stream_passes_tools_array_through_unmodified(
    fake_llm: FakeLLM, settings: Settings
) -> None:
    tools = [{"type": "function", "function": {"name": "get_wallet_balance"}}]
    fake_llm.script_turn(TokenEvent(delta={"content": "hi"}))
    router = _router(fake_llm, settings)

    await _collect(router.stream(_USER_MESSAGE, tier=ModelTier.DIALOGUE, tools=tools))

    assert fake_llm.calls[0].tools == tools


# ----------------------------------------------------------------------
# Passthrough of content deltas and the usage frame
# ----------------------------------------------------------------------


async def test_content_only_stream_passes_events_through_in_order(
    fake_llm: FakeLLM, settings: Settings
) -> None:
    scripted_usage = _usage_event()
    fake_llm.script_turn(
        TokenEvent(delta={"role": "assistant", "content": "Hello"}),
        TokenEvent(delta={"content": " Rajesh"}),
        scripted_usage,
    )
    router = _router(fake_llm, settings)

    events = await _collect(router.stream(_USER_MESSAGE, tier=ModelTier.DIALOGUE))

    assert events == [
        TokenEvent(delta={"role": "assistant", "content": "Hello"}),
        TokenEvent(delta={"content": " Rajesh"}),
        scripted_usage,
    ]
    # The usage frame is the very same object, untouched — the caller
    # hands it straight to CostTracker (docs/05 §3.8).
    assert events[-1] is scripted_usage
    # First event arrived within the deadline: exactly one provider call.
    assert len(fake_llm.calls) == 1


# ----------------------------------------------------------------------
# Tool-call reassembly (interface pin #2)
# ----------------------------------------------------------------------


async def test_reassembles_single_call_split_across_three_argument_fragments(
    fake_llm: FakeLLM, settings: Settings
) -> None:
    scripted_usage = _usage_event()
    fake_llm.script_turn(
        _fragment_event(
            0, call_id="call_abc123", name="get_payment_status", arguments='{"pay'
        ),
        _fragment_event(0, arguments='ment_id":"pay_'),
        _fragment_event(0, arguments='9f2c11"}'),
        scripted_usage,
    )
    router = _router(fake_llm, settings)

    events = await _collect(router.stream(_USER_MESSAGE, tier=ModelTier.DIALOGUE))

    # One whole ToolCallsEvent — before the usage frame, never raw fragments.
    assert events == [
        ToolCallsEvent(
            tool_calls=(
                ToolCall(
                    id="call_abc123",
                    name="get_payment_status",
                    arguments={"payment_id": "pay_9f2c11"},
                ),
            )
        ),
        scripted_usage,
    ]


async def test_reassembles_two_parallel_calls_interleaved_by_index(
    fake_llm: FakeLLM, settings: Settings
) -> None:
    # The canonical turn (docs/05 §2): two read tools proposed in one
    # completion, fragments interleaved across indices 0 and 1.
    fake_llm.script_turn(
        _fragment_event(0, call_id="call_bal", name="get_wallet_balance", arguments=""),
        _fragment_event(
            1, call_id="call_pay", name="get_payment_status", arguments='{"payment'
        ),
        _fragment_event(0, arguments="{}"),
        _fragment_event(1, arguments='_id":"pay_42"}'),
        _usage_event(),
    )
    router = _router(fake_llm, settings)

    events = await _collect(router.stream(_USER_MESSAGE, tier=ModelTier.DIALOGUE))

    tool_calls_events = [e for e in events if isinstance(e, ToolCallsEvent)]
    assert len(tool_calls_events) == 1
    assert tool_calls_events[0].tool_calls == (
        ToolCall(id="call_bal", name="get_wallet_balance", arguments={}),
        ToolCall(id="call_pay", name="get_payment_status", arguments={"payment_id": "pay_42"}),
    )


async def test_flushes_pending_calls_at_end_of_stream_without_usage_frame(
    fake_llm: FakeLLM, settings: Settings
) -> None:
    fake_llm.script_turn(
        _fragment_event(0, call_id="call_abc", name="get_wallet_balance", arguments="{}"),
    )
    router = _router(fake_llm, settings)

    events = await _collect(router.stream(_USER_MESSAGE, tier=ModelTier.DIALOGUE))

    assert events == [
        ToolCallsEvent(
            tool_calls=(ToolCall(id="call_abc", name="get_wallet_balance", arguments={}),)
        )
    ]


async def test_zero_argument_call_with_empty_arguments_parses_to_empty_dict(
    fake_llm: FakeLLM, settings: Settings
) -> None:
    fake_llm.script_turn(
        _fragment_event(0, call_id="call_abc", name="get_wallet_balance", arguments=""),
        _usage_event(),
    )
    router = _router(fake_llm, settings)

    events = await _collect(router.stream(_USER_MESSAGE, tier=ModelTier.DIALOGUE))

    assert isinstance(events[0], ToolCallsEvent)
    assert events[0].tool_calls[0].arguments == {}


async def test_hybrid_delta_re_emits_content_but_never_raw_fragments(
    fake_llm: FakeLLM, settings: Settings
) -> None:
    fake_llm.script_turn(
        _fragment_event(
            0,
            call_id="call_abc",
            name="get_wallet_balance",
            arguments="{}",
            content="Let me check",
        ),
        _usage_event(),
    )
    router = _router(fake_llm, settings)

    events = await _collect(router.stream(_USER_MESSAGE, tier=ModelTier.DIALOGUE))

    assert events[0] == TokenEvent(delta={"content": "Let me check"})
    assert isinstance(events[1], ToolCallsEvent)
    # No event anywhere still carries a raw fragment.
    for event in events:
        if isinstance(event, TokenEvent):
            assert "tool_calls" not in event.delta


async def test_malformed_accumulated_arguments_fail_loudly(
    fake_llm: FakeLLM, settings: Settings
) -> None:
    fake_llm.script_turn(
        _fragment_event(0, call_id="call_abc", name="get_wallet_balance", arguments="{not"),
        _fragment_event(0, arguments=" valid json"),
        _usage_event(),
    )
    router = _router(fake_llm, settings)

    with pytest.raises(orjson.JSONDecodeError):
        await _collect(router.stream(_USER_MESSAGE, tier=ModelTier.DIALOGUE))


async def test_fragment_without_index_fails_loudly(
    fake_llm: FakeLLM, settings: Settings
) -> None:
    fake_llm.script_turn(
        TokenEvent(delta={"tool_calls": [{"id": "call_x", "function": {"name": "f"}}]}),
    )
    router = _router(fake_llm, settings)

    with pytest.raises(ValueError, match="no integer 'index'"):
        await _collect(router.stream(_USER_MESSAGE, tier=ModelTier.DIALOGUE))


async def test_call_whose_id_and_name_never_arrive_fails_loudly(
    fake_llm: FakeLLM, settings: Settings
) -> None:
    fake_llm.script_turn(
        _fragment_event(0, arguments='{"a":1}'),
        _usage_event(),
    )
    router = _router(fake_llm, settings)

    with pytest.raises(ValueError, match="without an id/name"):
        await _collect(router.stream(_USER_MESSAGE, tier=ModelTier.DIALOGUE))


# ----------------------------------------------------------------------
# TTFT deadline policy (docs/05 §3.4 retry/timeout)
# ----------------------------------------------------------------------


async def test_ttft_stall_retries_exactly_once_on_same_tier_then_succeeds(
    settings: Settings,
) -> None:
    scripted_usage = _usage_event()
    provider = _StallingLLM(
        [TokenEvent(delta={"content": "Hello"}), scripted_usage], stall_attempts=1
    )
    router = _router(provider, settings)

    events = await _collect(
        router.stream(_USER_MESSAGE, tier=ModelTier.DIALOGUE, ttft_deadline_s=0.05)
    )

    assert events == [TokenEvent(delta={"content": "Hello"}), scripted_usage]
    assert provider.call_count == 2  # original + exactly one retry
    assert provider.cancelled_streams == 1  # the stalled attempt was torn down


async def test_ttft_stall_on_retry_propagates_after_exactly_two_attempts(
    settings: Settings,
) -> None:
    provider = _StallingLLM([], stall_attempts=2)
    router = _router(provider, settings)

    with pytest.raises(TimeoutError):
        await _collect(
            router.stream(_USER_MESSAGE, tier=ModelTier.DIALOGUE, ttft_deadline_s=0.05)
        )

    assert provider.call_count == 2  # never a third attempt
    assert provider.cancelled_streams == 2


# --------------------------------------------------------------------------
# Review hardening — security HIGH (unbounded memory growth), M-1 (duplicate
# tool-call ids), code-review MEDIUM (first-attempt stream-close symmetry)
# --------------------------------------------------------------------------


async def test_index_count_beyond_limit_fails_loudly(
    fake_llm: FakeLLM, settings: Settings
) -> None:
    from app.agent.llm_router import _MAX_TOOL_CALL_INDICES

    fragments = [
        _fragment_event(i, call_id=f"c{i}", name="get_wallet_balance", arguments="{}")
        for i in range(_MAX_TOOL_CALL_INDICES + 1)
    ]
    fake_llm.script_turn(*fragments, _usage_event())
    router = _router(fake_llm, settings)

    with pytest.raises(ValueError, match="distinct"):
        await _collect(router.stream(_USER_MESSAGE, tier=ModelTier.DIALOGUE))


async def test_argument_bytes_beyond_limit_fails_loudly(
    fake_llm: FakeLLM, settings: Settings
) -> None:
    from app.agent.llm_router import _MAX_ARGUMENT_BYTES_PER_INDEX

    oversized = "1" * (_MAX_ARGUMENT_BYTES_PER_INDEX + 1)
    fake_llm.script_turn(
        _fragment_event(0, call_id="c1", name="request_limit_increase", arguments=oversized),
        _usage_event(),
    )
    router = _router(fake_llm, settings)

    with pytest.raises(ValueError, match="accumulated argument bytes"):
        await _collect(router.stream(_USER_MESSAGE, tier=ModelTier.DIALOGUE))


async def test_duplicate_tool_call_id_across_indices_fails_loudly(
    fake_llm: FakeLLM, settings: Settings
) -> None:
    fake_llm.script_turn(
        _fragment_event(0, call_id="dup", name="get_wallet_balance", arguments="{}"),
        _fragment_event(
            1, call_id="dup", name="get_payment_status", arguments='{"payment_id":"txn_1"}'
        ),
        _usage_event(),
    )
    router = _router(fake_llm, settings)

    with pytest.raises(ValueError, match="reused across multiple indices"):
        await _collect(router.stream(_USER_MESSAGE, tier=ModelTier.DIALOGUE))


async def test_ttft_first_attempt_cancellation_still_closes_its_stream(
    settings: Settings,
) -> None:
    """Code-review MEDIUM: a non-TimeoutError failure (e.g. the router's
    own generator being aclose()'d mid-TTFT-wait) on the FIRST attempt
    must still tear down that attempt's stream, not just the retry's."""

    class _CancelsOnFirstEvent:
        def __init__(self) -> None:
            self.closed = False

        def stream(self, *args: Any, **kwargs: Any) -> AsyncIterator[LLMEvent]:
            return self._gen()

        async def _gen(self) -> AsyncIterator[LLMEvent]:
            try:
                await asyncio.sleep(10)
                yield _usage_event()  # pragma: no cover — never reached
            finally:
                self.closed = True

    provider = _CancelsOnFirstEvent()
    router = _router(provider, settings)
    agen = router.stream(_USER_MESSAGE, tier=ModelTier.DIALOGUE, ttft_deadline_s=10)

    task = asyncio.ensure_future(anext(agen))
    await asyncio.sleep(0)  # let the router open the stream and start waiting
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert provider.closed is True


# --------------------------------------------------------------------------
# llm.ttft / llm.total spans (docs/04 §7.2 — previously never opened, see
# git history). Real exported spans via `span_exporter` (tests/conftest.py),
# not just "the context manager didn't raise" — same bar
# tests/obs/test_tracing.py sets, adapted for an async-generator caller
# (tests/conftest.py's `_session_span_exporter` docstring explains why this
# needs a session-scoped provider rather than a per-test one).
# --------------------------------------------------------------------------


async def test_stream_opens_llm_total_and_ttft_spans_correctly_nested(
    fake_llm: FakeLLM, settings: Settings, span_exporter: InMemorySpanExporter
) -> None:
    fake_llm.script_turn(TokenEvent(delta={"content": "hi"}), _usage_event())
    router = _router(fake_llm, settings)

    await _collect(router.stream(_USER_MESSAGE, tier=ModelTier.DIALOGUE))

    otel_trace.get_tracer_provider().force_flush()
    spans = span_exporter.get_finished_spans()
    assert {s.name for s in spans} == {"llm.total", "llm.ttft"}

    total = next(s for s in spans if s.name == "llm.total")
    ttft = next(s for s in spans if s.name == "llm.ttft")
    assert ttft.parent is not None
    assert ttft.parent.span_id == total.context.span_id
    assert total.end_time is not None
    assert ttft.end_time is not None
    # ttft closes at first-event time, strictly before the full stream does.
    assert ttft.end_time <= total.end_time


async def test_both_spans_record_the_requested_tier(
    fake_llm: FakeLLM, settings: Settings, span_exporter: InMemorySpanExporter
) -> None:
    fake_llm.script_turn(TokenEvent(delta={"content": "hi"}), _usage_event())
    router = _router(fake_llm, settings)

    await _collect(router.stream(_USER_MESSAGE, tier=ModelTier.UTILITY))

    otel_trace.get_tracer_provider().force_flush()
    spans = span_exporter.get_finished_spans()
    assert len(spans) == 2
    for span in spans:
        assert span.attributes is not None
        assert span.attributes.get("tier") == "utility"


async def test_ttft_span_has_no_model_when_first_event_is_content(
    fake_llm: FakeLLM, settings: Settings, span_exporter: InMemorySpanExporter
) -> None:
    """`TokenEvent` carries no `.model` field (app/domain/types.py) — the
    common case (content streamed first) genuinely doesn't know which
    model served the request yet at TTFT time, so `llm.ttft` must not
    guess one."""
    fake_llm.script_turn(TokenEvent(delta={"content": "hi"}), _usage_event())
    router = _router(fake_llm, settings)

    await _collect(router.stream(_USER_MESSAGE, tier=ModelTier.DIALOGUE))

    otel_trace.get_tracer_provider().force_flush()
    ttft = next(s for s in span_exporter.get_finished_spans() if s.name == "llm.ttft")
    assert ttft.attributes is not None
    assert "model" not in ttft.attributes


async def test_ttft_span_records_model_when_the_first_event_is_usage(
    fake_llm: FakeLLM, settings: Settings, span_exporter: InMemorySpanExporter
) -> None:
    """The one case `model` IS knowable at TTFT time: the stream's very
    first event is itself the `UsageEvent`."""
    scripted_usage = _usage_event(model="anthropic/claude-sonnet-5")
    fake_llm.script_turn(scripted_usage)
    router = _router(fake_llm, settings)

    await _collect(router.stream(_USER_MESSAGE, tier=ModelTier.DIALOGUE))

    otel_trace.get_tracer_provider().force_flush()
    ttft = next(s for s in span_exporter.get_finished_spans() if s.name == "llm.ttft")
    assert ttft.attributes is not None
    assert ttft.attributes.get("model") == "anthropic/claude-sonnet-5"


async def test_total_span_records_model_once_the_usage_event_is_seen(
    fake_llm: FakeLLM, settings: Settings, span_exporter: InMemorySpanExporter
) -> None:
    """Unlike `llm.ttft`, `llm.total` spans the whole call — it sees the
    `UsageEvent` even when it arrives after content, so it can record
    `model` in the common case ttft genuinely cannot."""
    scripted_usage = _usage_event(model="anthropic/claude-sonnet-5")
    fake_llm.script_turn(TokenEvent(delta={"content": "hi"}), scripted_usage)
    router = _router(fake_llm, settings)

    await _collect(router.stream(_USER_MESSAGE, tier=ModelTier.DIALOGUE))

    otel_trace.get_tracer_provider().force_flush()
    total = next(s for s in span_exporter.get_finished_spans() if s.name == "llm.total")
    assert total.attributes is not None
    assert total.attributes.get("model") == "anthropic/claude-sonnet-5"


async def test_total_span_has_no_model_when_no_usage_event_ever_arrives(
    fake_llm: FakeLLM, settings: Settings, span_exporter: InMemorySpanExporter
) -> None:
    fake_llm.script_turn(TokenEvent(delta={"content": "hi"}))
    router = _router(fake_llm, settings)

    await _collect(router.stream(_USER_MESSAGE, tier=ModelTier.DIALOGUE))

    otel_trace.get_tracer_provider().force_flush()
    total = next(s for s in span_exporter.get_finished_spans() if s.name == "llm.total")
    assert total.attributes is not None
    assert "model" not in total.attributes


async def test_neither_span_ever_carries_cost_or_token_attributes(
    fake_llm: FakeLLM, settings: Settings, span_exporter: InMemorySpanExporter
) -> None:
    """LLMRouter never prices anything (`CostTracker` owns `cost_usd`) and
    doesn't duplicate `CostTracker`'s usage-dict parsing for
    `input_tokens`/`output_tokens` either — see the module docstring's
    "who owns this data" note."""
    fake_llm.script_turn(TokenEvent(delta={"content": "hi"}), _usage_event())
    router = _router(fake_llm, settings)

    await _collect(router.stream(_USER_MESSAGE, tier=ModelTier.DIALOGUE))

    otel_trace.get_tracer_provider().force_flush()
    spans = span_exporter.get_finished_spans()
    assert len(spans) == 2
    for span in spans:
        assert span.attributes is not None
        assert "cost_usd" not in span.attributes
        assert "input_tokens" not in span.attributes
        assert "output_tokens" not in span.attributes


async def test_both_spans_close_with_error_status_when_ttft_deadline_exhausts_both_attempts(
    settings: Settings, span_exporter: InMemorySpanExporter
) -> None:
    """A `TimeoutError` propagating out of `stream()` must not leave
    either span open/unexported — the nested `with`-blocks' `__exit__`
    fires on the exception path too, all the way out through
    `llm.total`."""
    provider = _StallingLLM([], stall_attempts=2)
    router = _router(provider, settings)

    with pytest.raises(TimeoutError):
        await _collect(
            router.stream(_USER_MESSAGE, tier=ModelTier.DIALOGUE, ttft_deadline_s=0.05)
        )

    otel_trace.get_tracer_provider().force_flush()
    spans = span_exporter.get_finished_spans()
    assert {s.name for s in spans} == {"llm.total", "llm.ttft"}
    for span in spans:
        assert span.end_time is not None
        assert span.status.status_code is otel_trace.StatusCode.ERROR


async def test_both_spans_close_when_the_consumer_abandons_the_stream_early(
    fake_llm: FakeLLM, settings: Settings, span_exporter: InMemorySpanExporter
) -> None:
    """A consumer that `aclose()`s the generator after the first event
    (a barge-in, ConversationManager's `_MAX_REPLY_CHARS` guard) must
    still close both spans — the `try`/`finally` wrapping the yields
    handles `GeneratorExit` the same as any other exit path. Verified
    against the real SDK with a standalone async-generator+span repro
    before relying on this in production code (see PR description);
    `GeneratorExit` is a `BaseException`, not an `Exception`, so
    `opentelemetry.trace.use_span` deliberately does NOT mark these
    spans as errored (its own source: "classes that directly inherit
    BaseException are not technically errors") — only that they close."""
    fake_llm.script_turn(
        TokenEvent(delta={"content": "one"}),
        TokenEvent(delta={"content": "two"}),
        _usage_event(),
    )
    router = _router(fake_llm, settings)
    agen = router.stream(_USER_MESSAGE, tier=ModelTier.DIALOGUE)

    first = await agen.__anext__()
    assert first == TokenEvent(delta={"content": "one"})
    await agen.aclose()

    otel_trace.get_tracer_provider().force_flush()
    spans = span_exporter.get_finished_spans()
    assert {s.name for s in spans} == {"llm.total", "llm.ttft"}
    for span in spans:
        assert span.end_time is not None
        assert span.status.status_code is not otel_trace.StatusCode.ERROR


async def test_model_attribute_survives_an_early_abandon_right_after_a_usage_event(
    fake_llm: FakeLLM, settings: Settings, span_exporter: InMemorySpanExporter
) -> None:
    """Code-review HIGH, fixed: an earlier version of `stream()` consumed
    `_forward_events(...)` via a bare `async for`, which does NOT
    propagate this generator's own `.aclose()` into `_forward_events`'
    generator synchronously — its `finally` (which records `model` on
    `total_span` and closes `upstream`) was deferred to a LATER
    event-loop tick, by which point `total_span` had already closed
    (in `stream()`'s own frame, synchronously), silently dropping the
    attribute (the SDK logs "Setting attribute on ended span"). Fixed by
    consuming `_forward_events(...)` through `contextlib.aclosing()`,
    whose `__aexit__` runs synchronously as part of THIS generator's own
    close. This test aborts immediately after the FIRST event, which is
    itself the `UsageEvent` — the one scenario where `served_model` is
    knowable right away, making a dropped attribute directly observable
    rather than looking identical to the "never learned it" case."""
    fake_llm.script_turn(_usage_event(model="anthropic/claude-sonnet-5"))
    router = _router(fake_llm, settings)
    agen = router.stream(_USER_MESSAGE, tier=ModelTier.DIALOGUE)

    first = await agen.__anext__()
    assert first == _usage_event(model="anthropic/claude-sonnet-5")
    await agen.aclose()

    otel_trace.get_tracer_provider().force_flush()
    spans = span_exporter.get_finished_spans()
    total_span = next(s for s in spans if s.name == "llm.total")
    assert total_span.attributes is not None
    assert total_span.attributes.get("model") == "anthropic/claude-sonnet-5"


async def test_error_status_never_carries_the_exception_message_or_a_recorded_event(
    fake_llm: FakeLLM, settings: Settings, span_exporter: InMemorySpanExporter
) -> None:
    """Security review HIGH, fixed: `_ToolCallAssembler`'s malformed-
    fragment `ValueError`s embed the raw fragment/argument text verbatim
    (potentially amounts/account numbers, for a money-moving agent) —
    OTel's default `record_exception=True`/`set_status_on_exception=True`
    would attach that text to the span (an `exception` event plus a
    `str(exc)`-bearing status description), exported unredacted via
    Phase 2's default ConsoleSpanExporter. `llm.total`'s span must carry
    only the exception's TYPE name, never its message, and must record
    no `exception` event at all."""
    sensitive_marker = "acct-9988776655-amount-450000"
    fake_llm.script_turn(
        TokenEvent(delta={"tool_calls": [{"function": {"name": sensitive_marker}}]}),
    )
    router = _router(fake_llm, settings)

    with pytest.raises(ValueError, match="no integer 'index'") as exc_info:
        await _collect(router.stream(_USER_MESSAGE, tier=ModelTier.DIALOGUE))
    assert sensitive_marker in str(exc_info.value)  # confirms the raw content really is in there

    otel_trace.get_tracer_provider().force_flush()
    spans = span_exporter.get_finished_spans()
    total_span = next(s for s in spans if s.name == "llm.total")
    assert total_span.status.status_code is otel_trace.StatusCode.ERROR
    assert total_span.status.description == "ValueError"
    assert sensitive_marker not in (total_span.status.description or "")
    assert total_span.events == ()  # record_exception=False: no exception event at all
    for span in spans:
        rendered = repr(span.to_json())
        assert sensitive_marker not in rendered
