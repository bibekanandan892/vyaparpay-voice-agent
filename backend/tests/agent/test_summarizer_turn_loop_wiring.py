"""End-to-end proof — no Docker, no Postgres — that the
summarizer-turn-loop-wiring task closes docs/17-roadmap.md §2.5's
returning-caller milestone dependency chain:

    ConversationManager.on_stt_final (the turn loop)
        -> Summarizer.observe_turn() / kick()          (this task)
        -> a real rolling fold, off the turn path       (app/memory/summarizer.py)
        -> SessionMemory.get_summary()                  (already built)
        -> app.memory.slots.render_summary_slot()       (already built)
        -> the exact call ContextBuilder._get_memory_summary makes
           for the <memory_summary> prompt slot (already wired, T3)

Every earlier Phase-5 batch was tested in isolation with the seam directly
above it left unexercised — `session_manager.py`'s own judgment call #12
and `Summarizer`'s own module docstring both said so explicitly: nothing
in `app/` constructed a `Summarizer` or called `observe_turn()`/`kick()`
before this task. This file is the one test that drives the REAL
`ConversationManager` through 9 real turns and checks that a real fold
really landed where `ContextBuilder` really reads it — the same
`render_summary_slot`/`SessionMemory.get_summary` call, not a re-derived
stand-in for it.

Doubling strategy, matching tests/e2e/test_canonical_conversation.py's
(the Docker-gated sibling this file deliberately does NOT need): the LLM
is the only stub (`tests/fakes.py::FakeLLM`, wrapped by the REAL
`LLMRouter`, so the real usage-event reassembly path runs too), and
`SessionMemory` is REAL, wrapping a REAL `RedisClient` over
`tests/support/fake_redis.py::FakeRedis` — the same in-memory Redis
stand-in `test_canonical_conversation.py` uses for exactly this reason
(docs/04 §9.4's provider-mocking strategy: respx/testcontainers for
wire-level tests, an in-process fake for everything downstream of it).
`ContextBuilder`/`ToolExecutor`/`SafetyLayer` are minimal local Protocol
fakes (no tool calls are scripted, so a real `ToolExecutor` would add
Postgres/registry setup this test does not need) — `PromptBuilder` is
the REAL class, since nothing about it needs a double.

The `CostTracker` is REAL and shared between `ConversationManager` and
`Summarizer`, exactly as `app/voice/run.py::_make_brain_factory` wires
production (point 5 of this task's brief): its `finalize()` is never
called (no Postgres here), but `record_turn()` — the method both
collaborators actually use — is pure in-memory and needs none.

Not a tautology: the expected fold text is a literal string this file
scripts through `FakeLLM`, asserted byte-for-byte against what
`SessionMemory.get_summary()` reads back — never re-derived by calling
the code under test.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, cast

from app.agent.conversation_manager import ConversationManager
from app.agent.cost_tracker import CostTracker
from app.agent.llm_router import LLMRouter
from app.agent.prompt_builder import PromptBuilder
from app.config import Settings
from app.data.redis_client import RedisClient
from app.domain.interfaces import LLMProvider, LLMRouterProto
from app.domain.types import (
    ContextBundle,
    PendingConfirm,
    SafetyVerdict,
    Session,
    SessionState,
    SessionUser,
    TokenEvent,
    ToolCall,
    ToolResult,
    UsageEvent,
)
from app.memory.session_memory import SessionMemory
from app.memory.slots import render_summary_slot
from app.memory.summarizer import FIRST_FOLD_TURN, Summarizer, summary_thru_turn
from tests.fakes import FakeLLM
from tests.support.fake_redis import FakeRedis

_SESSION_ID = "sess_e2e_fold"
_USER_ID = "usr_rajesh01"

# The literal fold output this test scripts through FakeLLM — the ONLY
# thing that makes the model "say". Asserted byte-for-byte at the end, so
# this is what proves the test is not computing its expected value by
# calling the code under test (the tautology this project has been
# burned by before).
_FOLD_TEXT = (
    "Rajesh reported a declined payment and asked three follow-up "
    "questions about his account; the agent answered each one directly."
)


class _FakeContextBuilder:
    """Returns a minimal, well-formed bundle carrying the real utterance
    — this test does not exercise slot 1-5/7 rendering, only that the
    turn loop calls through to the real `Summarizer`."""

    async def build(self, session: Session, *, current_utterance: str) -> ContextBundle:
        return ContextBundle(
            persona="Asha is VyaparPay's voice assistant.",
            business_rules="Be concise. Never invent account facts.",
            user_profile="",
            current_utterance=current_utterance,
        )


class _FakeSafetyLayer:
    """Allow-everything double — no PII/number claims are scripted in
    this test's replies, so there is nothing for a real SafetyLayer to
    catch that this double's absence would hide."""

    def fence_input(self, bundle: ContextBundle) -> ContextBundle:
        return bundle

    def screen_output(self, text: str, tool_results: list[ToolResult]) -> SafetyVerdict:
        return SafetyVerdict(allowed=True)

    def classify_affirmation(self, utterance: str, pending: PendingConfirm | None) -> bool:
        return False

    def authorize_tool(self, call: ToolCall, principal: SessionUser) -> bool:
        return True


class _FakeToolExecutor:
    """Never actually called — no turn in this test scripts a tool call —
    kept only because `ConversationManager` requires the collaborator to
    exist."""

    async def execute(
        self,
        calls: list[ToolCall],
        *,
        principal: SessionUser,
        session_id: str,
        turn_no: int,
        affirmed: bool = False,
    ) -> list[ToolResult]:
        raise AssertionError("no turn in this test scripts a tool call")


class _FakeToolRegistry:
    def get(self, name: str) -> Any:
        return None

    def all(self) -> list[Any]:
        return []

    def to_openai_tools(self) -> list[dict[str, Any]]:
        return []


def _dialogue_turn(text: str, *, model: str) -> list[Any]:
    return [
        TokenEvent(delta={"content": text}),
        UsageEvent(model=model, usage={"prompt_tokens": 120, "completion_tokens": 20}),
    ]


def _fold_turn(text: str, *, model: str) -> list[Any]:
    return [
        TokenEvent(delta={"content": text}),
        UsageEvent(model=model, usage={"prompt_tokens": 900, "completion_tokens": 60}),
    ]


async def test_a_real_rolling_fold_lands_where_context_builder_reads_it(
    settings: Settings, fake_llm: FakeLLM
) -> None:
    """Drives 9 real turns through the real `ConversationManager` +
    `Summarizer` (default `task_factory`, exactly production's shape),
    forces the turn-9 fold to completion deterministically via
    `summarizer.drain()` (the same call `on_call_ended` makes at
    hang-up), then reads the summary back through the exact function
    `ContextBuilder._get_memory_summary` calls."""
    assert FIRST_FOLD_TURN == 9  # this test's own turn count depends on it

    redis_client = RedisClient(
        cast(Any, FakeRedis()), session_ttl_seconds=settings.session_ttl_seconds
    )
    session_memory = SessionMemory(redis_client)

    llm_router = LLMRouter(cast(LLMProvider, fake_llm), settings)
    # `session_factory=None`: `finalize()` (the only method that would
    # touch it) is never called here — there is no Postgres in this test,
    # by design (module docstring). `record_turn()`, the method both
    # ConversationManager and Summarizer actually use, is pure in-memory.
    cost_tracker = CostTracker(settings, session_factory=cast(Any, None), redis=redis_client)
    summarizer = Summarizer(
        cast(LLMRouterProto, llm_router), session_memory, cost_tracker=cost_tracker
    )

    session = Session(
        session_id=_SESSION_ID,
        user_id=_USER_ID,
        state=SessionState.IN_CALL,
        started_at=datetime.now(UTC),
    )
    manager = ConversationManager(
        session=session,
        context_builder=_FakeContextBuilder(),
        prompt_builder=PromptBuilder(),
        llm_router=cast(LLMRouterProto, llm_router),
        tool_executor=_FakeToolExecutor(),
        safety_layer=_FakeSafetyLayer(),
        cost_tracker=cost_tracker,
        summarizer=summarizer,
        session_memory=session_memory,
        tool_registry=_FakeToolRegistry(),
    )

    dialogue_model = settings.openrouter_dialogue_model
    utility_model = settings.openrouter_utility_model

    # Turns 1-8: should_fold is False for every one (docs/09 §4.1 rule 1)
    # — no fold scheduled, nothing for FakeLLM to serve beyond the
    # dialogue reply itself.
    for turn_no in range(1, FIRST_FOLD_TURN):
        fake_llm.script_turn(*_dialogue_turn(f"Reply {turn_no}.", model=dialogue_model))

    # Turn 9: the dialogue reply, THEN (queued right after it — kick()
    # only SCHEDULES the fold, it does not run until this test explicitly
    # drains it below, so FakeLLM's FIFO queue sees the dialogue call
    # first regardless) the fold's own completion.
    fake_llm.script_turn(*_dialogue_turn("Reply 9.", model=dialogue_model))
    fake_llm.script_turn(*_fold_turn(_FOLD_TEXT, model=utility_model))

    for turn_no in range(1, FIRST_FOLD_TURN + 1):
        reply = await manager.on_stt_final(f"Turn {turn_no} from Rajesh.")
        assert reply == f"Reply {turn_no}."

    # The orphaned-fold-safe way to force the in-flight fold to
    # completion deterministically — the exact call `on_call_ended`
    # makes at hang-up (app/voice/run.py), not a sleep-and-hope.
    await summarizer.drain()

    stored = await session_memory.get_summary(_SESSION_ID)
    assert stored is not None
    assert stored.thru_turn == summary_thru_turn(FIRST_FOLD_TURN)  # == 3

    # THE assertion: the exact function ContextBuilder._get_memory_summary
    # (app/agent/context_builder.py) calls on this exact value, applied
    # here directly, matches the literal text this test scripted through
    # FakeLLM — byte for byte, never re-derived from the code under test.
    assert render_summary_slot(stored) == _FOLD_TEXT

    # Bonus, not the headline claim: the fold's own cost landed on the
    # SAME CostTracker instance the 9 dialogue turns billed to (point 5
    # of this task's brief) — proven by construction (one shared
    # instance, one running total) rather than by a second, unread
    # tracker's absence.
    assert len(fake_llm.calls) == FIRST_FOLD_TURN + 1  # 9 dialogue + 1 fold
    assert cost_tracker.call_total() > Decimal("0")
