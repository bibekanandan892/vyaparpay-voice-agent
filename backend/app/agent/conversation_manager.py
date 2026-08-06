"""`ConversationManager` — the per-turn orchestrator (docs/05-agent-
architecture.md §3.2), implementing the frozen `ConversationManagerProto`
(app/domain/interfaces.py): `on_stt_final` opens a turn, runs the
critical path (context → prompt → LLM → tools → safety → cost, docs/05
§2's numbered solid arrows), and returns Asha's finalized reply text for
the CLI harness to print. Every other Batch-4 component is injected via
its Protocol — this class holds the turn state machine and the tool
loop, and does no I/O of its own beyond the transcript writes it owns.

Judgment calls, flagged per house style rather than silently guessed at:

1. **Tool-call events are detected structurally, not by isinstance.**
   Task 4.3's `LLMRouter` yields reassembled whole tool calls in an
   event type (`ToolCallsEvent`) added to `app/domain/types.py` on a
   sibling branch (PR #15) not yet merged to `main` as of this branch.
   Importing it here would couple this PR's merge order to that one —
   the exact hazard PR #11 documented against PR #10. Instead the loop
   checks `getattr(event, "tool_calls", None)`: any event carrying a
   non-None `tool_calls` sequence of `ToolCall`s is the router's
   reassembled batch. Tighten to an `isinstance` check in a follow-up
   once both branches are on `main`.
2. **The tool loop is bounded at 2 execution rounds** (docs/05 §2: "the
   loop bound is small (typically 0–2 iterations)"; the Phase-2 plan
   pins "0–2"). If the model emits tool calls a third time, the loop
   stops executing AND discards that round's narration (security review
   HIGH, fixed — an earlier revision kept it): text streamed alongside
   a dropped batch describes calls that never ran, in the worst case
   announcing a confirm-gated action that was never gated. The turn
   degrades to `_screen_output`'s `_BLOCKED_OUTPUT_FALLBACK` hedge
   instead — see the bound-hit comment in `_run_tool_loop`.
3. **Turn records for `session:{id}:turns` are NOT appended here.**
   docs/12 §7 and `RedisClient.append_turn`'s own comment name
   CostTracker (task 4.3, sibling branch) as the appender; writing them
   here too would double-append the drain source `SessionManager.end`
   reads. Reconcile once 4.3 lands — if its CostTracker turns out not to
   append, the follow-up belongs here, with `turn_no` this class owns.
4. **`screen_output` failure fails closed** (docs/05 §3.6): a blocked
   verdict falls back to `safe_text` when the layer provides one,
   otherwise to a fixed hedge that voices no account facts. The hedge
   deliberately offers a re-check rather than apologizing emptily.
5. **A critical-path exception degrades to a spoken apology + human
   offer** (docs/05 §3.2's failure behavior: "a spoken apology plus an
   `escalate_to_human` offer rather than dropping silence on the
   call") — logged server-side with the real exception, never voiced.
6. **Affirmation is classified once, at turn open**, from the utterance
   against the pending confirm present at that moment, and the same
   flag is passed to every `execute()` this turn — the gate semantics
   (docs/10 §4) anchor a "yes" to the utterance that voiced it, not to
   whatever the pending state morphs into mid-turn.
7. **`on_barge_in` is a Phase-2 no-op** — kept in the contract for
   shape-stability into Phase 3 (`ConversationManagerProto`'s own
   docstring); there is no audio to cancel yet.
8. **Transcript writes are split into an early user-turn append and a
   late assistant-turn append, each independently best-effort** (fixed
   after review — MEDIUM). The earlier single `_append_transcript(user,
   assistant)` call at turn-end meant a Redis hiccup on the *second*
   (assistant) write inside that call raised past the point where a
   correctly-computed `reply_text` already existed, discarding it in
   favor of the generic apology — and the top-level except-handler's
   retry then re-appended the user's utterance a SECOND time (the first
   append had already succeeded). Now: the user turn is appended once,
   immediately, before any of the rest of the turn runs; the assistant
   turn is appended once, at the very end, wrapped in its own
   best-effort try/except that never discards an already-computed
   `reply_text` on a logging-layer failure. The top-level except-handler
   in `on_stt_final` therefore only ever needs to append the apology as
   the assistant turn — never the user turn again.
9. **`state` transitions to `SPEAKING` only from the round that actually
   produces the returned reply** (fixed after review — MEDIUM). The tool
   loop discards every round's `content_parts` except the last (only the
   final round's text becomes `reply_text`); the state flag now follows
   that same rule instead of firing on the first content token of *any*
   round, including one whose narration is later thrown away because
   that round also carried `tool_calls`. Kept correct now rather than
   guessed right later — docs/05 §3.2 calls the SPEAKING transition
   safety-critical for Phase 3 barge-in cancellation, even though
   nothing reads `state` yet in Phase 2.
10. **`affirmed` is passed unconditionally to every tool round**
    (security review CRITICAL, fixed twice — originally by honoring the
    flag only on a turn's first round here, then refined by a later
    security review to gate on `PendingConfirm.proposed_turn` inside
    `ToolExecutor._confirm_tier` instead, which closes the cross-round
    confirm-bypass without defeating the legitimate same-turn
    read-then-reconfirm flow the round-only gate broke). The fuller
    history lives on the inline comment at the `execute()` call site.
11. **`_MAX_REPLY_CHARS` is a safety BACKSTOP on accumulated reply
    content** (security review HIGH, fixed), not an enforcement of the
    voice-output token budget — see the constant's own comment.
12. **`_run_tool_loop` consumes each round's stream through
    `contextlib.aclosing()`** (code review HIGH, fixed — closes the gap
    `LLMRouter.stream()`'s docstring tracked for this exact call site).
    The `_MAX_REPLY_CHARS` guard raises out of the stream-consumption
    loop — without `aclosing()`, Python's async-generator finalizer
    would only schedule the now-abandoned `stream`'s `.aclose()` on a
    LATER event-loop tick, likely from a different
    task/`contextvars.Context` than the one that opened
    `LLMRouter.stream()`'s own `llm.total` span, breaking that span's
    `context.detach()` and silently dropping it from the trace. `async
    with contextlib.aclosing(stream)` closes it synchronously, in THIS
    frame, on every exit path — normal completion, that exception, or
    any other.
"""

from __future__ import annotations

import contextlib
import inspect
from collections.abc import AsyncGenerator
from typing import cast

from opentelemetry.trace import Span

from app.domain.interfaces import (
    ContextBuilderProto,
    CostTrackerProto,
    LLMRouterProto,
    PromptBuilderProto,
    SafetyLayerProto,
    ToolExecutorProto,
    ToolRegistry,
)
from app.domain.types import (
    LLMEvent,
    Message,
    Role,
    Session,
    SessionUser,
    TaskKind,
    TokenEvent,
    ToolCall,
    ToolResult,
    TurnState,
    UsageEvent,
)
from app.memory.session_memory import SessionMemory
from app.memory.short_term import ShortTermMemory
from app.obs.logging import get_logger
from app.obs.tracing import SPAN_TURN, get_tracer, safe_set_attribute

log = get_logger(__name__)
tracer = get_tracer(__name__)

# Judgment call #2 (module docstring): docs/05 §2's "typically 0-2".
_MAX_TOOL_ROUNDS = 2

# Judgment call #4: the fail-closed reply when screen_output blocks and
# provides no safe_text. States no account fact, offers the re-check.
_BLOCKED_OUTPUT_FALLBACK = (
    "Let me double-check that for you — one moment. "
    "If it's urgent I can also connect you to a human agent."
)

# Judgment call #5: the degraded reply for a critical-path failure.
_TURN_FAILURE_APOLOGY = (
    "I'm sorry, something went wrong on my side just now. "
    "Would you like me to try again, or connect you to a human agent?"
)

# Judgment call #11 (security review HIGH, fixed): a safety BACKSTOP, not
# an enforcement of docs/11 §1's ~150-token voice-output budget (that's a
# separate, not-yet-built mechanism). Nothing anywhere in the pipeline —
# the OpenRouter request body (no max_tokens), LLMRouter, or this class —
# bounded how much text a single streamed completion could accumulate
# before joining, screening, and persisting it; a misbehaving/compromised
# upstream or a model stuck in a repetition loop could grow this
# unboundedly, mirroring the tool-call-fragment issue task 4.3 already
# fixed for the same reason. ~16x the real voice budget — never trips on
# a legitimate answer, bounds the pathological case.
_MAX_REPLY_CHARS = 8_000


class ConversationManager:
    """docs/05 §3.2. One instance per call/session; `on_stt_final` is
    invoked once per user turn by the Phase-2 CLI harness (the Phase-3
    `VoiceAgentWorker` takes over that role unchanged — the brain/media
    boundary docs/05 §1.1 exists to keep this class transport-blind).

    Collaborators arrive as their frozen Protocols (docs/04 §4's DI
    split) with one exception: `session_memory` is the concrete
    `SessionMemory` class, since no `SessionMemoryProto` exists in
    `app/domain/interfaces.py` — this class never constructs any of its
    collaborators, so the E2E harness swaps any of them (via a subclass,
    for `session_memory`) for a fake without touching this file.
    """

    def __init__(
        self,
        *,
        session: Session,
        context_builder: ContextBuilderProto,
        prompt_builder: PromptBuilderProto,
        llm_router: LLMRouterProto,
        tool_executor: ToolExecutorProto,
        safety_layer: SafetyLayerProto,
        cost_tracker: CostTrackerProto,
        session_memory: SessionMemory,
        tool_registry: ToolRegistry,
    ) -> None:
        self._session = session
        self._context_builder = context_builder
        self._prompt_builder = prompt_builder
        self._llm_router = llm_router
        self._tool_executor = tool_executor
        self._safety_layer = safety_layer
        self._cost_tracker = cost_tracker
        self._session_memory = session_memory
        self._tool_registry = tool_registry
        self._principal = SessionUser(user_id=session.user_id)
        self.state: TurnState = TurnState.LISTENING
        self._turn_no = 0

    def record_stt_audio(self, seconds: float) -> None:
        """Media-stage usage forwarded to this call's cost ledger.

        This class owns the per-call `CostTracker`, and the `Brain` seam
        (app/voice/worker.py) is the only wire between the voice worker's
        media stages and it — so STT audio duration and TTS character
        counts arrive here. They are usage quantities, not transport
        detail: nothing about SDP, RTP, or codecs crosses this boundary,
        so docs/05 §1.1's transport-blind rule still holds.
        """
        self._cost_tracker.record_stt_audio(seconds)

    def record_tts_text(self, characters: int) -> None:
        """See `record_stt_audio`."""
        self._cost_tracker.record_tts_text(characters)

    async def on_stt_final(self, text: str) -> str:
        """Opens a turn, runs docs/05 §2's critical path, returns the
        finalized reply. Never raises — a critical-path failure returns
        the degraded apology (judgment call #5) so the CLI/voice channel
        always gets *something* to say."""
        self._turn_no += 1
        self.state = TurnState.THINKING
        with tracer.start_as_current_span(SPAN_TURN) as span:
            safe_set_attribute(span, "session_id", self._session.session_id)
            safe_set_attribute(span, "turn_no", self._turn_no)
            # Judgment call #8: appended once, immediately — never
            # revisited by the except-handler below, so a later failure
            # can't cause a duplicate.
            await self._append_user_turn(text)
            try:
                reply = await self._run_turn(text, span)
            except Exception:
                log.error(
                    "turn_failed",
                    session_id=self._session.session_id,
                    turn_no=self._turn_no,
                    exc_info=True,
                )
                safe_set_attribute(span, "turn.failed", True)
                reply = _TURN_FAILURE_APOLOGY
                await self._append_assistant_turn(reply)
            finally:
                self.state = TurnState.LISTENING
        return reply

    async def on_barge_in(self) -> None:
        """Phase-2 no-op (judgment call #7) — no audio subtree exists to
        cancel until Phase 3's voice pipeline."""

    # ------------------------------------------------------------------
    # The critical path (docs/05 §2, numbered arrows)
    # ------------------------------------------------------------------

    async def _run_turn(self, text: str, span: Span) -> str:
        # Affirmation first (judgment call #6): the classification pairs
        # THIS utterance with the pending confirm as it stood when the
        # user spoke, before any of this turn's executions mutate it.
        pending = await self._session_memory.get_pending_confirm(self._session.session_id)
        affirmed = self._safety_layer.classify_affirmation(text, pending)
        safe_set_attribute(span, "turn.affirmed", affirmed)

        stm = ShortTermMemory()

        # Arrows 2-3: assemble + fence + render.
        bundle = await self._context_builder.build(self._session, current_utterance=text)
        bundle = self._safety_layer.fence_input(bundle)
        stm.context_bundle = bundle
        messages = list(self._prompt_builder.render(bundle))

        tools = self._tool_registry.to_openai_tools()
        tier = self._llm_router.route(TaskKind.DIALOGUE)

        reply_text = await self._run_tool_loop(
            messages, tools=tools, tier=tier, affirmed=affirmed, stm=stm, span=span
        )

        reply_text = self._screen_output(reply_text, stm.tool_results, span=span)

        if self._cost_tracker.over_budget():
            # docs/05 §3.8: warn + span event; the shorter-context
            # degrade signal is Phase-4/5 ContextBuilder machinery.
            log.warning(
                "call_over_budget",
                session_id=self._session.session_id,
                call_total=str(self._cost_tracker.call_total()),
            )
            safe_set_attribute(span, "turn.over_budget", True)

        # Judgment call #8: best-effort — a failure here must never
        # discard an already-computed, correct reply_text.
        await self._append_assistant_turn(reply_text)
        return reply_text

    async def _run_tool_loop(
        self,
        messages: list[Message],
        *,
        tools: list[dict[str, object]],
        tier: object,
        affirmed: bool,
        stm: ShortTermMemory,
        span: Span,
    ) -> str:
        """Arrows 4-7: stream, executing at most `_MAX_TOOL_ROUNDS` tool
        batches (docs/10 §2's pipeline runs inside `execute()`).
        Extracted from `_run_turn` (code-review HIGH — the combined
        method exceeded the house 50-line function limit)."""
        reply_text = ""
        tool_rounds = 0
        while True:
            content_parts: list[str] = []
            tool_calls: list[ToolCall] = []
            stream = await self._open_stream(messages, tier=tier, tools=tools)
            # Judgment call #12 (module docstring): aclosing() ensures
            # `stream` closes synchronously, in this frame, on every exit
            # path — including _ingest_event's _MAX_REPLY_CHARS
            # ValueError, which a bare `async for` would leave to a
            # deferred, possibly cross-task close.
            async with contextlib.aclosing(stream) as events:
                async for event in events:
                    await self._ingest_event(
                        event, content_parts=content_parts, tool_calls=tool_calls, span=span
                    )

            reply_text = "".join(content_parts)

            if not tool_calls:
                # Judgment call #9: this round's content IS the reply —
                # the only point SPEAKING can correctly fire from.
                if content_parts:
                    self.state = TurnState.SPEAKING
                break
            if tool_rounds >= _MAX_TOOL_ROUNDS:
                # Judgment call #2: bound reached — stop executing.
                # Security review HIGH, fixed: this round's content (if
                # any) is narration for calls that never ran — including,
                # in the worst case, the model announcing a
                # confirm-gated action ("I've submitted your request…")
                # that was neither executed NOR ever gated, since the
                # dropped batch never reached ToolExecutor. Voicing it
                # would be a hallucinated action claim SafetyLayer can't
                # reliably catch (narration without figures passes the
                # number screen). Discard it; the empty reply then takes
                # `_screen_output`'s existing dead-air fallback
                # (_BLOCKED_OUTPUT_FALLBACK), which offers a re-check
                # and names no action or account fact.
                log.warning(
                    "tool_loop_bound_reached",
                    session_id=self._session.session_id,
                    turn_no=self._turn_no,
                    dropped_calls=[c.name for c in tool_calls],
                    discarded_narration_chars=len(reply_text),
                )
                safe_set_attribute(span, "turn.tool_loop_bound_hit", True)
                reply_text = ""
                break

            tool_rounds += 1
            messages.append(
                Message(
                    role=Role.ASSISTANT,
                    content=reply_text or None,
                    tool_calls=tuple(tool_calls),
                )
            )
            results = await self._tool_executor.execute(
                tool_calls,
                principal=self._principal,
                session_id=self._session.session_id,
                turn_no=self._turn_no,
                # Judgment call #10 (security review CRITICAL, originally
                # fixed by gating on round number here; refined by a
                # later security review to gate on `PendingConfirm.
                # proposed_turn` inside ToolExecutor instead — see that
                # module's `_confirm_tier` docstring/comment). The
                # earlier round-only gate closed the cross-round
                # confirm-bypass (a model proposing a NEW action in round
                # 1 that supersedes what the user actually said yes to,
                # then re-emitting it in round 2 to ride the stale
                # `affirmed=True`) but also silently defeated a
                # legitimate case: a read in round 1 followed by
                # re-emitting the SAME already-pending action in round 2
                # never got to execute, because `affirmed` was already
                # forced False by then regardless of what the round 2
                # call actually matched. ToolExecutor now has the
                # information this class doesn't — whether the round's
                # call matches a pending action proposed BEFORE this
                # turn opened — so `affirmed` is passed through as
                # classified at turn-open, unconditionally, on every
                # round; the anchor to "the utterance that voiced it"
                # (module docstring judgment call #6) is enforced there.
                affirmed=affirmed,
            )
            for result in results:
                stm.record_tool_result(result)
                messages.append(result.to_llm_message())
            # Loop: stream again with results appended (docs/05 §2's
            # "append results, continue stream").
        return reply_text

    async def _ingest_event(
        self,
        event: LLMEvent,
        *,
        content_parts: list[str],
        tool_calls: list[ToolCall],
        span: Span,
    ) -> None:
        """One streamed event into the round's accumulators — extracted
        from `_run_tool_loop`'s consumption loop (code-review HIGH: the
        judgment-call-#12 `aclosing()` wrapper pushed the combined loop
        past the house 4-level nesting limit, the same reason
        `llm_router.py` extracted `_forward_events`). Mutates
        `content_parts`/`tool_calls` in place; raises judgment call
        #11's `_MAX_REPLY_CHARS` ValueError."""
        if isinstance(event, UsageEvent):
            turn_cost = self._cost_tracker.record_turn(event.usage, event.model, span)
            await self._session_memory.add_cost(self._session.session_id, turn_cost.cost_usd)
        elif (calls := getattr(event, "tool_calls", None)) is not None:
            # Judgment call #1: task 4.3's reassembled-batch event,
            # matched structurally.
            tool_calls.extend(calls)
        elif isinstance(event, TokenEvent):
            content = event.delta.get("content")
            if not content:
                return
            content_parts.append(content)
            accumulated = sum(len(p) for p in content_parts)
            if accumulated > _MAX_REPLY_CHARS:
                log.error(
                    "llm_reply_size_limit_exceeded",
                    session_id=self._session.session_id,
                    turn_no=self._turn_no,
                    accumulated=accumulated,
                    limit=_MAX_REPLY_CHARS,
                )
                raise ValueError(
                    f"streamed reply exceeded {_MAX_REPLY_CHARS} accumulated "
                    "characters — refusing to accumulate further"
                )

    async def _open_stream(
        self, messages: list[Message], *, tier: object, tools: list[dict[str, object]]
    ) -> AsyncGenerator[LLMEvent, None]:
        """`LLMRouterProto.stream` is spelled `async def ... ->
        AsyncIterator`, which mypy reads as coroutine-returning, while a
        real async-generator implementation returns the iterator
        directly — the pre-existing Protocol wart task 4.3 also
        documents (and independently handles the same way in its own
        `llm_router.py`). Handle both shapes at runtime. Declared
        `AsyncGenerator`, not the Protocol's own `AsyncIterator` —
        narrower but accurate (every real implementation, including the
        one this method's whole docstring is about, is a genuine async
        generator with `.aclose()`) and required by `_run_tool_loop`'s
        `contextlib.aclosing(stream)` (judgment call #12, module
        docstring), which needs that method statically."""
        raw_stream = self._llm_router.stream(messages, tier=tier, tools=tools)  # type: ignore[arg-type]
        if inspect.iscoroutine(raw_stream):
            return cast("AsyncGenerator[LLMEvent, None]", await raw_stream)
        return cast("AsyncGenerator[LLMEvent, None]", raw_stream)

    def _screen_output(self, reply_text: str, tool_results: list[ToolResult], *, span: Span) -> str:
        """Arrow: output checks (docs/05 §3.6) — fail closed (judgment
        call #4)."""
        verdict = self._safety_layer.screen_output(reply_text, tool_results)
        if not verdict.allowed:
            log.warning(
                "output_blocked",
                session_id=self._session.session_id,
                turn_no=self._turn_no,
                reason=verdict.reason,
            )
            safe_set_attribute(span, "turn.output_blocked", True)
            reply_text = verdict.safe_text or _BLOCKED_OUTPUT_FALLBACK
        elif verdict.safe_text is not None:
            # Allowed-but-modified (e.g. PII masked): voice the masked form.
            reply_text = verdict.safe_text

        if not reply_text:
            # A tool-only or bound-hit turn can end with no content at
            # all; dead air is the one thing a voice channel can't ship.
            reply_text = _BLOCKED_OUTPUT_FALLBACK
        return reply_text

    # ------------------------------------------------------------------
    # Transcript (docs/05 §3.2: the manager "assembles the transcript")
    # ------------------------------------------------------------------

    async def _append_user_turn(self, text: str) -> None:
        """A synthetic call-open trigger turn (empty utterance,
        PromptBuilder's turn-1 convention) writes no user entry —
        nothing was said. Best-effort (judgment call #8): a transcript
        write failure degrades the NEXT turn's context, never this
        turn's answer."""
        if not text:
            return
        try:
            await self._session_memory.append_message(
                self._session.session_id, Message(role=Role.USER, content=text)
            )
        except Exception:  # noqa: BLE001 — deliberate best-effort, see docstring
            log.error("user_transcript_append_failed", exc_info=True)

    async def _append_assistant_turn(self, reply_text: str) -> None:
        """Best-effort (judgment call #8): never let a logging-layer
        failure here propagate and discard an already-computed
        `reply_text` the caller is about to return/voice."""
        try:
            await self._session_memory.append_message(
                self._session.session_id, Message(role=Role.ASSISTANT, content=reply_text)
            )
        except Exception:  # noqa: BLE001 — deliberate best-effort, see docstring
            log.error("assistant_transcript_append_failed", exc_info=True)


__all__ = ["ConversationManager"]
