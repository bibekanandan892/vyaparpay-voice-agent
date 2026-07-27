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
   sibling branch that may merge before or after this one. Importing it
   here would couple this PR's merge order to that one — the exact
   hazard PR #11 documented against PR #10. Instead the loop checks
   `getattr(event, "tool_calls", None)`: any event carrying a non-None
   `tool_calls` sequence of `ToolCall`s is the router's reassembled
   batch. Tighten to an `isinstance` check in a follow-up once both
   branches are on `main`.
2. **The tool loop is bounded at 2 execution rounds** (docs/05 §2: "the
   loop bound is small (typically 0–2 iterations)"; the Phase-2 plan
   pins "0–2"). If the model emits tool calls a third time, the loop
   stops executing and falls back to whatever content has accumulated —
   a bounded-but-thin answer beats an unbounded loop on a voice budget.
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
   The user's utterance still lands in the transcript window so the
   next turn has honest context; the apology line does too.
6. **Affirmation is classified once, at turn open**, from the utterance
   against the pending confirm present at that moment, and the same
   flag is passed to every `execute()` this turn — the gate semantics
   (docs/10 §4) anchor a "yes" to the utterance that voiced it, not to
   whatever the pending state morphs into mid-turn.
7. **`on_barge_in` is a Phase-2 no-op** — kept in the contract for
   shape-stability into Phase 3 (`ConversationManagerProto`'s own
   docstring); there is no audio to cancel yet.
"""

from __future__ import annotations

import inspect
from collections.abc import AsyncIterator
from typing import cast

from opentelemetry import trace

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
    TurnState,
    UsageEvent,
)
from app.memory.session_memory import SessionMemory
from app.memory.short_term import ShortTermMemory
from app.obs.logging import get_logger
from app.obs.tracing import safe_set_attribute

log = get_logger(__name__)
tracer = trace.get_tracer(__name__)

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


class ConversationManager:
    """docs/05 §3.2. One instance per call/session; `on_stt_final` is
    invoked once per user turn by the Phase-2 CLI harness (the Phase-3
    `VoiceAgentWorker` takes over that role unchanged — the brain/media
    boundary docs/05 §1.1 exists to keep this class transport-blind).

    All collaborators arrive as their frozen Protocols (docs/04 §4's DI
    split); this class never constructs one, so the E2E harness swaps
    any of them for a fake without touching this file.
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

    async def on_stt_final(self, text: str) -> str:
        """Opens a turn, runs docs/05 §2's critical path, returns the
        finalized reply. Never raises — a critical-path failure returns
        the degraded apology (judgment call #5) so the CLI/voice channel
        always gets *something* to say."""
        self._turn_no += 1
        self.state = TurnState.THINKING
        with tracer.start_as_current_span("turn") as span:
            safe_set_attribute(span, "session_id", self._session.session_id)
            safe_set_attribute(span, "turn_no", self._turn_no)
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
                # Transcript still gets both sides (judgment call #5) —
                # best-effort: if Redis is the thing that's down, the
                # turn already degraded; don't mask the original error
                # with a second one.
                try:
                    await self._append_transcript(text, reply)
                except Exception:  # noqa: BLE001 — deliberate best-effort
                    log.error("transcript_append_failed", exc_info=True)
            finally:
                self.state = TurnState.LISTENING
        return reply

    async def on_barge_in(self) -> None:
        """Phase-2 no-op (judgment call #7) — no audio subtree exists to
        cancel until Phase 3's voice pipeline."""

    # ------------------------------------------------------------------
    # The critical path (docs/05 §2, numbered arrows)
    # ------------------------------------------------------------------

    async def _run_turn(self, text: str, span: trace.Span) -> str:
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

        # Arrows 4-7: stream, executing at most _MAX_TOOL_ROUNDS tool
        # batches (docs/10 §2's pipeline runs inside execute()).
        reply_text = ""
        tool_rounds = 0
        while True:
            content_parts: list[str] = []
            tool_calls: list[ToolCall] = []
            # `LLMRouterProto.stream` is spelled `async def ... ->
            # AsyncIterator`, which mypy reads as coroutine-returning,
            # while a real async-generator implementation returns the
            # iterator directly — the pre-existing Protocol wart task 4.3
            # also documents. Handle both shapes at runtime.
            raw_stream = self._llm_router.stream(messages, tier=tier, tools=tools)
            stream = (
                await raw_stream
                if inspect.iscoroutine(raw_stream)
                else cast("AsyncIterator[LLMEvent]", raw_stream)
            )
            async for event in stream:
                if isinstance(event, UsageEvent):
                    turn_cost = self._cost_tracker.record_turn(event.usage, event.model, span)
                    await self._session_memory.add_cost(
                        self._session.session_id, turn_cost.cost_usd
                    )
                elif (calls := getattr(event, "tool_calls", None)) is not None:
                    # Judgment call #1: task 4.3's reassembled-batch
                    # event, matched structurally.
                    tool_calls.extend(calls)
                elif isinstance(event, TokenEvent):
                    content = event.delta.get("content")
                    if content:
                        if not content_parts:
                            self.state = TurnState.SPEAKING
                        content_parts.append(content)

            reply_text = "".join(content_parts)

            if not tool_calls:
                break
            if tool_rounds >= _MAX_TOOL_ROUNDS:
                # Judgment call #2: bound reached — stop executing, keep
                # whatever the model already said.
                log.warning(
                    "tool_loop_bound_reached",
                    session_id=self._session.session_id,
                    turn_no=self._turn_no,
                    dropped_calls=[c.name for c in tool_calls],
                )
                safe_set_attribute(span, "turn.tool_loop_bound_hit", True)
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
                affirmed=affirmed,
            )
            for result in results:
                stm.record_tool_result(result)
                messages.append(result.to_llm_message())
            # Loop: stream again with results appended (docs/05 §2's
            # "append results, continue stream").

        # Arrow: output checks (docs/05 §3.6) — fail closed (judgment
        # call #4).
        verdict = self._safety_layer.screen_output(reply_text, stm.tool_results)
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

        if self._cost_tracker.over_budget():
            # docs/05 §3.8: warn + span event; the shorter-context
            # degrade signal is Phase-4/5 ContextBuilder machinery.
            log.warning(
                "call_over_budget",
                session_id=self._session.session_id,
                call_total=str(self._cost_tracker.call_total()),
            )
            safe_set_attribute(span, "turn.over_budget", True)

        await self._append_transcript(text, reply_text)
        return reply_text

    async def _append_transcript(self, user_text: str, reply_text: str) -> None:
        """The two transcript-window writes this class owns (docs/05
        §3.2: the manager "assembles the transcript"). A synthetic
        call-open trigger turn (empty utterance, PromptBuilder's turn-1
        convention) writes no user entry — nothing was said."""
        if user_text:
            await self._session_memory.append_message(
                self._session.session_id, Message(role=Role.USER, content=user_text)
            )
        await self._session_memory.append_message(
            self._session.session_id, Message(role=Role.ASSISTANT, content=reply_text)
        )


__all__ = ["ConversationManager"]
