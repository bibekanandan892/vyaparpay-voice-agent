"""LLMRouter — tier routing, wire adaptation, streamed tool-call
reassembly, and the TTFT deadline/retry policy (docs/05-agent-
architecture.md §3.4).

This module owns the two interface pins the Phase-2 plan calls out:

1. **Wire adaptation.** `stream()` takes domain `list[Message]` and hands
   `LLMProvider.stream()` the OpenAI-compatible `list[dict]` it expects
   (`Message.to_wire()`, app/domain/types.py) — the provider layer does
   no domain-type handling of its own (`LLMProvider`'s docstring,
   app/domain/interfaces.py).
2. **Tool-call reassembly.** `OpenRouterLLM` forwards every streamed
   chunk's raw `delta` untouched (its module docstring explicitly defers
   reassembly here). Real OpenAI-compatible streams split one tool call's
   `function.arguments` JSON string across many chunks, keyed by
   `tool_calls[i].index`, with `id`/`function.name` arriving only on the
   first fragment for that index; parallel calls (the canonical
   two-reads turn, docs/05 §2) interleave distinct indices. `stream()`
   accumulates fragments per index and yields ONE `ToolCallsEvent` of
   whole `ToolCall`s (arguments JSON-parsed to a dict) when the
   tool-call section completes.

Judgment calls, flagged per house style:

- **The filler phrase is out of scope.** docs/05 §3.4's TTFT step 1 is
  "emit a filler phrase to TTS + retry once on the same tier" — Phase 2
  has no TTS/voice pipeline, so only the retry half is implemented here;
  the filler belongs to the Phase-3 voice worker.
- **Degrade/apology (docs/05 §3.4 steps 2-3) are not here either.** A
  retry that also stalls or errors propagates its exception — the
  shortened-prompt degrade and the `escalate_to_human` apology are
  ConversationManager's turn-level policy (docs/05 §3.2, a later task).
  Routing failures (primary-model 5xx/capacity) were already covered
  inside the single HTTP call by OpenRouter's `models: [...]` fallback
  array, invisibly to this layer.
- **The tool-call section's end is inferred, not observed.** The provider
  forwards only `delta` dicts, so `finish_reason: "tool_calls"` is
  invisible at this layer. Real streams place tool-call fragments after
  any content, followed by the stream-final usage frame — pending calls
  are flushed immediately before the `UsageEvent` (so the caller sees
  whole calls, then usage) or at end of stream, whichever comes first.
- **`llm.total`/`llm.ttft` are opened here, not by ConversationManager.**
  Earlier revisions of this docstring deferred span-opening to a later
  task; this is that task. `llm.total` wraps this whole `stream()` call
  (the async-generator body, closed via the existing try/finally so it
  covers normal completion, an error, and an early `aclose()` alike);
  `llm.ttft` nests inside it and wraps only `_open_with_ttft_retry(...)`
  — the deadline/retry policy's own span, distinct from the full-stream
  one. `model` is recorded on either span only once a `UsageEvent` (the
  only `LLMEvent` shape carrying `.model`, per app/domain/types.py) has
  actually been observed — `TokenEvent` doesn't carry it, so a stream
  whose first event is content-only genuinely doesn't know the served
  model yet at TTFT time. `cost_usd`/token counts stay off both spans:
  this module sees the raw provider `usage` dict but never prices it
  (CostTracker owns that, per the paragraph above), so setting them here
  would either duplicate `CostTracker.record_turn`'s parsing of that same
  dict (already recorded, on the enclosing `turn` span the caller passes
  it) or risk disagreeing with it.
- **Both spans disable OTel's default exception recording** (security
  review HIGH, fixed): `start_as_current_span(..., record_exception=False,
  set_status_on_exception=False)`. The SDK default would otherwise attach
  `str(exc)` and a full traceback to the span on any uncaught exception —
  `_ToolCallAssembler`'s own `ValueError`s (malformed tool-call fragments)
  embed the raw fragment/argument text verbatim, which for a
  money-moving agent can contain amounts or account numbers, exported
  unredacted via Phase 2's default `ConsoleSpanExporter(out=sys.stderr)`
  (`app/obs/tracing.py`'s own module docstring warns about exactly this).
  Each `with`-block instead catches its own exceptions narrowly and sets
  `Status(StatusCode.ERROR, description=type(exc).__name__)` — the
  exception's TYPE name only, never its message or traceback — before
  re-raising, so the span still shows *that* something failed without
  ever carrying *what* the failure said.
- **KNOWN OPEN GAP, not fixed in this revision** (code review HIGH,
  tracked, not silently missed): `llm.total`'s span correctly closes when
  the CONSUMER of `stream()` calls `.aclose()` in the same task/context
  it's iterating from — verified against the real opentelemetry-sdk. It
  does NOT correctly close when the consumer's loop body raises an
  exception WITHOUT calling `.aclose()` first (Python's async-generator
  finalizer then throws `GeneratorExit` from a different task with its
  own copied `contextvars.Context`, and the span's `context.detach()`
  fails against that mismatched Context — the span silently never closes
  / exports). `ConversationManager._run_tool_loop`'s `_MAX_REPLY_CHARS`
  guard does exactly this today (raises out of its `async for event in
  stream:` loop with no `try/finally`/`aclosing()`). The correct fix is
  on the CONSUMER side — wrap stream consumption in
  `contextlib.aclosing()` — not here; deferred because that call site
  (`conversation_manager.py`) is being touched by a separate, currently
  in-flight PR (the confirm-gate turn-anchored eligibility fix) and
  editing it now would create an unnecessary merge conflict. Tracked as
  a required follow-up, to land together with whatever next touches
  `ConversationManager._run_tool_loop`.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
from collections.abc import AsyncGenerator, AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Final, cast

import orjson
from opentelemetry.trace import Span, Status, StatusCode

from app.config import Settings
from app.domain.interfaces import LLMProvider
from app.domain.types import (
    LLMEvent,
    Message,
    ModelTier,
    TaskKind,
    TokenEvent,
    ToolCall,
    ToolCallsEvent,
    UsageEvent,
)
from app.obs.logging import get_logger
from app.obs.tracing import SPAN_LLM_TOTAL, SPAN_LLM_TTFT, get_tracer, safe_set_attribute

log = get_logger(__name__)
tracer = get_tracer(__name__)

# docs/05 §3.4's tier table: the merchant-facing dialogue turn gets the
# best model; invisible bookkeeping (summary folds, classification) gets
# the cheap one. One decision per task kind — deliberately a dumb mapping.
_TASK_TIER: Final[dict[TaskKind, ModelTier]] = {
    TaskKind.DIALOGUE: ModelTier.DIALOGUE,
    TaskKind.UTILITY: ModelTier.UTILITY,
}

# Security-review HIGH, fixed: nothing bounded how many distinct
# `tool_calls[i].index` values or how many argument bytes per index this
# accumulator would hold — a misbehaving or compromised upstream could
# grow it unboundedly for as long as the connection stayed open (no
# per-call wall-clock cap exists past the TTFT deadline). The 16-tool
# catalog (docs/10-tool-calling.md §3) bounds legitimate parallelism; a
# generous multiple covers any real batch with headroom, and per-index
# argument bytes are bounded well above the largest real tool schema
# (docs/10 §3's `update_business_address`, still under 1 KB serialized).
# Both fail loudly (house rule) rather than silently truncating.
_MAX_TOOL_CALL_INDICES: Final = 32
_MAX_ARGUMENT_BYTES_PER_INDEX: Final = 16_384


@dataclass
class _PartialToolCall:
    """Mutable accumulator for one in-flight streamed tool call.

    Deliberately not frozen: it exists to accumulate fragments and is
    internal working state, never shared or returned — the immutability
    rule protects the domain value objects this module *emits*
    (`ToolCall`/`ToolCallsEvent`), which stay frozen.
    """

    id: str | None = None
    name: str | None = None
    argument_parts: list[str] = field(default_factory=list)


class _ToolCallAssembler:
    """Stitches OpenAI-compatible streamed `tool_calls` fragments back
    into whole `ToolCall`s (the docs/05 §3.4 interface pin).

    The streaming shape this decodes (pinned raw, fragment by fragment,
    in tests/providers/test_openrouter.py): each chunk's
    `delta.tool_calls` is a list of fragments each carrying an `index`;
    the first fragment for an index carries `id` and `function.name`;
    every fragment may carry a partial `function.arguments` string that
    only parses as JSON once concatenated in arrival order.
    """

    def __init__(self) -> None:
        self._partials: dict[int, _PartialToolCall] = {}

    @property
    def has_pending(self) -> bool:
        return bool(self._partials)

    def process(self, event: LLMEvent) -> list[LLMEvent]:
        """Map one provider event to the events the router should yield.

        - `TokenEvent` without `tool_calls`: passed through untouched —
          content/role/empty deltas are the caller's to interpret.
        - `TokenEvent` with `tool_calls`: fragments are consumed into the
          accumulator and NOT re-yielded (the caller must never see a raw
          fragment). A hybrid delta carrying `content` alongside
          fragments re-emits just the content part as a fresh TokenEvent.
        - `UsageEvent`: flushes pending calls first, so the reassembled
          `ToolCallsEvent` always precedes the stream-final usage frame.
        - `ToolCallsEvent`: already whole (not something `OpenRouterLLM`
          emits today) — passed through untouched.
        """
        if isinstance(event, UsageEvent):
            out: list[LLMEvent] = []
            if self.has_pending:
                out.append(ToolCallsEvent(tool_calls=self.flush()))
            out.append(event)
            return out
        if isinstance(event, ToolCallsEvent):
            # Security-review LOW, hardened: an already-whole event (not
            # something OpenRouterLLM emits today, but the Protocol
            # admits a future provider that does) must not silently
            # strand any partials this instance already accumulated —
            # flush them first so nothing is dropped.
            out = [ToolCallsEvent(tool_calls=self.flush())] if self.has_pending else []
            out.append(event)
            return out
        fragments = event.delta.get("tool_calls")
        if not fragments:
            return [event]
        self._feed(fragments)
        content = event.delta.get("content")
        if content:
            return [TokenEvent(delta={"content": content})]
        return []

    def flush(self) -> tuple[ToolCall, ...]:
        """Finalize every accumulated call in index order, resetting the
        accumulator. Malformed accumulation fails loudly with full
        context attached (house rule: never silently swallow — mirrors
        `OpenRouterLLM`'s malformed-chunk handling one layer down)."""
        calls: list[ToolCall] = []
        seen_ids: set[str] = set()
        for index in sorted(self._partials):
            partial = self._partials[index]
            raw_arguments = "".join(partial.argument_parts)
            if partial.id is None or partial.name is None:
                log.error(
                    "llm_router.tool_call_reassembly_incomplete",
                    index=index,
                    tool_call_id=partial.id,
                    tool_name=partial.name,
                    raw_arguments=raw_arguments,
                )
                raise ValueError(
                    f"streamed tool_call at index {index} ended without an id/name "
                    f"(id={partial.id!r}, name={partial.name!r}) — first-fragment "
                    "fields never arrived"
                )
            if partial.id in seen_ids:
                # Security-review M-1: a provider that violates its own
                # documented "id arrives only on an index's first
                # fragment" invariant by reusing an id across indices
                # would otherwise silently produce two ToolCalls sharing
                # one id — corrupting tool-result correlation downstream
                # (ToolExecutor, the follow-up LLM message). Fail loudly
                # instead of guessing which one is real.
                log.error(
                    "llm_router.tool_call_id_reused_across_indices",
                    index=index,
                    tool_call_id=partial.id,
                )
                raise ValueError(
                    f"streamed tool_call id {partial.id!r} reused across multiple "
                    "indices — expected one id per index"
                )
            seen_ids.add(partial.id)
            arguments = self._parse_arguments(partial, index, raw_arguments)
            calls.append(ToolCall(id=partial.id, name=partial.name, arguments=arguments))
        self._partials = {}
        return tuple(calls)

    def _feed(self, fragments: list[dict[str, Any]]) -> None:
        for fragment in fragments:
            index = fragment.get("index")
            if not isinstance(index, int):
                # Without an integer index there is nothing to key the
                # accumulation on — fail loudly with the fragment attached
                # rather than guessing (raw fragment is LLM output, not a
                # secret, same logging rationale as openrouter.py).
                log.error("llm_router.tool_call_fragment_missing_index", fragment=fragment)
                raise ValueError(
                    f"streamed tool_call fragment has no integer 'index': {fragment!r}"
                )
            if index not in self._partials and len(self._partials) >= _MAX_TOOL_CALL_INDICES:
                log.error(
                    "llm_router.tool_call_index_limit_exceeded",
                    index=index,
                    limit=_MAX_TOOL_CALL_INDICES,
                )
                raise ValueError(
                    f"streamed tool_calls exceeded {_MAX_TOOL_CALL_INDICES} distinct "
                    "indices — refusing to accumulate further (docs/10 §3's 16-tool "
                    "catalog bounds any legitimate batch well under this)"
                )
            partial = self._partials.setdefault(index, _PartialToolCall())
            if partial.id is None and fragment.get("id"):
                partial.id = fragment["id"]
            function = fragment.get("function") or {}
            if partial.name is None and function.get("name"):
                partial.name = function["name"]
            arguments_part = function.get("arguments")
            if arguments_part:
                accumulated = sum(len(p) for p in partial.argument_parts) + len(arguments_part)
                if accumulated > _MAX_ARGUMENT_BYTES_PER_INDEX:
                    log.error(
                        "llm_router.tool_call_argument_bytes_exceeded",
                        index=index,
                        tool_call_id=partial.id,
                        accumulated=accumulated,
                        limit=_MAX_ARGUMENT_BYTES_PER_INDEX,
                    )
                    raise ValueError(
                        f"streamed tool_call at index {index} exceeded "
                        f"{_MAX_ARGUMENT_BYTES_PER_INDEX} accumulated argument bytes"
                    )
                partial.argument_parts.append(arguments_part)

    @staticmethod
    def _parse_arguments(
        partial: _PartialToolCall, index: int, raw_arguments: str
    ) -> dict[str, Any]:
        if not raw_arguments:
            # A zero-argument call streams `arguments: ""` (or nothing at
            # all) — an empty accumulation means "no arguments", which is
            # a defined wire shape, not malformed JSON to fail on.
            return {}
        try:
            arguments = orjson.loads(raw_arguments)
        except orjson.JSONDecodeError:
            log.error(
                "llm_router.tool_call_arguments_malformed",
                index=index,
                tool_call_id=partial.id,
                tool_name=partial.name,
                raw_arguments=raw_arguments,
            )
            raise
        if not isinstance(arguments, dict):
            log.error(
                "llm_router.tool_call_arguments_not_object",
                index=index,
                tool_call_id=partial.id,
                tool_name=partial.name,
                raw_arguments=raw_arguments,
            )
            raise ValueError(
                f"tool_call {partial.name!r} arguments parsed to "
                f"{type(arguments).__name__}, expected a JSON object: {raw_arguments!r}"
            )
        return arguments


class LLMRouter:
    """Implements `app.domain.interfaces.LLMRouterProto` (docs/05 §3.4)
    on top of an injected `LLMProvider` — model IDs and fallback slugs
    come from `Settings`, never constants (canon §5)."""

    def __init__(self, provider: LLMProvider, settings: Settings) -> None:
        self._provider = provider
        self._settings = settings

    def route(self, task: TaskKind) -> ModelTier:
        """docs/05 §3.4's tier table — see `_TASK_TIER`."""
        return _TASK_TIER[task]

    async def stream(
        self,
        messages: list[Message],
        *,
        tier: ModelTier,
        tools: list[dict[str, Any]] | None = None,
        ttft_deadline_s: float = 1.5,
    ) -> AsyncIterator[LLMEvent]:
        """Stream one completion: adapt to wire shape, apply the TTFT
        deadline/one-retry policy, pass content `TokenEvent`s and the
        final `UsageEvent` through (the caller hands usage to
        CostTracker), and yield reassembled `ToolCallsEvent`s in place of
        raw tool-call fragments. See the module docstring for the policy
        scope, judgment calls, and the `llm.total`/`llm.ttft` span
        boundaries (module docstring)."""
        wire_messages = [message.to_wire() for message in messages]
        models = self._models_for(tier)
        # llm.total wraps the ENTIRE call — opened here, closed on every
        # exit path via the try/except below plus _forward_events' own
        # try/finally (normal completion, an error, or the consumer
        # aclose()-ing this generator early). A `with`-block's __exit__
        # fires correctly across an async generator's yield points as
        # long as the generator eventually resumes to one of those three
        # outcomes (verified against the real opentelemetry-sdk with a
        # standalone repro — see the module docstring's KNOWN OPEN GAP
        # note for the one exit path that's NOT yet safe).
        with tracer.start_as_current_span(
            SPAN_LLM_TOTAL, record_exception=False, set_status_on_exception=False
        ) as total_span:
            safe_set_attribute(total_span, "tier", tier.value)
            try:
                upstream, first = await self._open_ttft_span(
                    wire_messages,
                    models=models,
                    tools=tools,
                    ttft_deadline_s=ttft_deadline_s,
                    tier=tier,
                )
                # Security review HIGH, fixed: `async for event in
                # self._forward_events(...): yield event` does NOT
                # propagate this generator's own `.aclose()`/GeneratorExit
                # into `_forward_events`' generator synchronously — bare
                # `async for` only drops the reference, leaving asyncio's
                # async-generator finalizer to schedule its `.aclose()`
                # on a LATER event-loop tick. That deferred close ran
                # AFTER total_span already closed (in this same `with`
                # block, synchronously), so `_forward_events`' own
                # `finally` (the `model` attribute write + closing
                # `upstream`) either landed on an already-ended span
                # (silently dropped — the SDK logs "Setting attribute on
                # ended span") or closed the upstream HTTP response one
                # tick late — both verified empirically with a standalone
                # repro before relying on this. `async with
                # contextlib.aclosing(...)` compiles to an in-frame
                # try/finally, so its `__aexit__` (and therefore
                # `_forward_events`' own `finally`) runs synchronously as
                # part of THIS generator's own close, not deferred.
                async with contextlib.aclosing(
                    self._forward_events(upstream, first, total_span)
                ) as events:
                    async for event in events:
                        yield event
            except Exception as exc:
                # Security review HIGH, fixed: never let str(exc)/the
                # traceback reach the span (module docstring) — the type
                # name alone is enough to see that this call failed.
                total_span.set_status(Status(StatusCode.ERROR, description=type(exc).__name__))
                raise

    async def _open_ttft_span(
        self,
        wire_messages: list[dict[str, Any]],
        *,
        models: list[str],
        tools: list[dict[str, Any]] | None,
        ttft_deadline_s: float,
        tier: ModelTier,
    ) -> tuple[AsyncIterator[LLMEvent], LLMEvent | None]:
        """llm.ttft: wraps only the TTFT deadline/retry policy's own
        call, not the rest of the stream — extracted from `stream()` to
        keep that method's nesting within house style (code-review
        HIGH)."""
        with tracer.start_as_current_span(
            SPAN_LLM_TTFT, record_exception=False, set_status_on_exception=False
        ) as ttft_span:
            safe_set_attribute(ttft_span, "tier", tier.value)
            try:
                upstream, first = await self._open_with_ttft_retry(
                    wire_messages, models=models, tools=tools, ttft_deadline_s=ttft_deadline_s
                )
            except Exception as exc:
                ttft_span.set_status(Status(StatusCode.ERROR, description=type(exc).__name__))
                raise
            # `model` (which model in the fallback array actually served
            # the request) is only knowable once a `UsageEvent` has been
            # seen — `TokenEvent` carries no `model` field
            # (app/domain/types.py). The common case (first event is
            # streamed content) genuinely doesn't know it yet; omit
            # rather than guess.
            if isinstance(first, UsageEvent):
                safe_set_attribute(ttft_span, "model", first.model)
            return upstream, first

    async def _forward_events(
        self,
        upstream: AsyncIterator[LLMEvent],
        first: LLMEvent | None,
        total_span: Span,
    ) -> AsyncGenerator[LLMEvent, None]:
        """Reassembles tool-call fragments and forwards events; records
        the served model on `total_span` and closes `upstream` on every
        exit path. Extracted from `stream()` to keep that method's
        nesting within house style (code-review HIGH) — this generator
        opens no span of its own (no `context.attach()`/`detach()`
        pairing happens here), so its cleanup is safe to run from any
        task/context, unlike `total_span`'s own `with`-block in the
        caller."""
        assembler = _ToolCallAssembler()
        served_model: str | None = None
        try:
            if first is not None:
                if isinstance(first, UsageEvent):
                    served_model = first.model
                for out_event in assembler.process(first):
                    yield out_event
                async for event in upstream:
                    if isinstance(event, UsageEvent):
                        served_model = event.model
                    for out_event in assembler.process(event):
                        yield out_event
            if assembler.has_pending:
                # Stream ended without a usage frame — flush at end of
                # stream so accumulated calls are never dropped.
                yield ToolCallsEvent(tool_calls=assembler.flush())
        finally:
            # Record before closing the span — set_attribute on an
            # already-ended span is a silent no-op in the SDK.
            if served_model is not None:
                safe_set_attribute(total_span, "model", served_model)
            # The consumer may abandon this generator mid-stream
            # (aclose()); closing the provider stream here keeps the
            # underlying HTTP response from leaking. No-op when the
            # stream is already exhausted.
            await _close_quietly(upstream)

    def _models_for(self, tier: ModelTier) -> list[str]:
        """docs/05 §3.4: dialogue rides the fallback array (primary +
        configured fallbacks, `Settings.dialogue_models`); utility is the
        single cheap model with no fallback array of its own — every
        utility task is off the merchant-audible path."""
        if tier is ModelTier.DIALOGUE:
            return self._settings.dialogue_models
        return [self._settings.openrouter_utility_model]

    async def _open_with_ttft_retry(
        self,
        wire_messages: list[dict[str, Any]],
        *,
        models: list[str],
        tools: list[dict[str, Any]] | None,
        ttft_deadline_s: float,
    ) -> tuple[AsyncIterator[LLMEvent], LLMEvent | None]:
        """Open the provider stream and await its first event under the
        TTFT deadline (docs/05 §3.4: no first token by 1.5 s -> retry
        once, same tier). Returns the live iterator plus its first event
        (`None` = the stream legitimately ended with no events at all).

        A stalled first attempt is closed and retried exactly once; a
        retry that stalls (TimeoutError) or errors propagates after its
        stream is closed — see the module docstring for why degrade
        lives with the caller."""
        upstream = await self._open_stream(wire_messages, models=models, tools=tools)
        try:
            return upstream, await _first_event(upstream, ttft_deadline_s)
        except TimeoutError:
            log.warning(
                "llm_router.ttft_deadline_exceeded",
                ttft_deadline_s=ttft_deadline_s,
                models=models,
                attempt=1,
            )
            await _close_quietly(upstream)
        except BaseException:
            # Code-review MEDIUM, fixed: only TimeoutError closed the
            # first attempt's stream; a CancelledError from the router's
            # own generator being aclose()'d while still awaiting the
            # first token (a realistic barge-in/hangup-during-TTFT
            # scenario) propagated straight out, before stream()'s own
            # try/finally is even entered. Now symmetric with the retry
            # branch below.
            await _close_quietly(upstream)
            raise
        retry_upstream = await self._open_stream(wire_messages, models=models, tools=tools)
        try:
            return retry_upstream, await _first_event(retry_upstream, ttft_deadline_s)
        except BaseException:
            await _close_quietly(retry_upstream)
            raise

    async def _open_stream(
        self,
        wire_messages: list[dict[str, Any]],
        *,
        models: list[str],
        tools: list[dict[str, Any]] | None,
    ) -> AsyncIterator[LLMEvent]:
        """Open one provider stream. `LLMProvider.stream`'s
        `async def ... -> AsyncIterator` Protocol spelling admits two
        legal implementations: an async *generator* (OpenRouterLLM,
        FakeLLM — calling it returns the iterator directly, un-awaited)
        or a plain coroutine that must be awaited to obtain the iterator.
        Accept both rather than baking in the generator form — the cast
        reconciles mypy's coroutine reading of the Protocol with the
        generator reality of every current implementation."""
        result: Any = self._provider.stream(wire_messages, models=models, tools=tools)
        if inspect.iscoroutine(result):
            result = await result
        return aiter(cast("AsyncIterator[LLMEvent]", result))


async def _first_event(
    upstream: AsyncIterator[LLMEvent], deadline_s: float
) -> LLMEvent | None:
    """First event under the TTFT deadline. Raises `TimeoutError` on a
    stall (`asyncio.wait_for` also cancels the pending `anext`, which
    unwinds the provider generator at its await point); returns `None`
    when the stream ends before yielding anything."""
    try:
        return await asyncio.wait_for(anext(upstream), timeout=deadline_s)
    except StopAsyncIteration:
        return None


async def _close_quietly(upstream: AsyncIterator[LLMEvent]) -> None:
    """Best-effort `aclose()` of a provider stream being walked away from
    (a stalled first attempt, an errored retry, or a consumer that closed
    the router's own generator mid-stream).

    Deliberate, narrow exception to the never-swallow rule: a failure to
    close an already-abandoned stream is logged and dropped — raising
    here would mask the TTFT retry (or the original error) the caller
    actually cares about."""
    aclose = getattr(upstream, "aclose", None)
    if aclose is None:
        return
    try:
        await aclose()
    except Exception as exc:
        log.warning("llm_router.abandoned_stream_close_failed", error=str(exc))


__all__ = ["LLMRouter"]
