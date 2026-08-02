"""End-to-end replay of the canonical Rajesh/₹245/daily-limit-exceeded
call (docs/01-product-and-use-case.md §6-7) through the REAL agent loop —
Phase-2 plan task 6.2.

Every other test in this codebase either unit-tests one component behind
hand-rolled Protocol fakes (tests/agent/*.py) or is a narrower
testcontainers-gated ORM/repo check (tests/models/test_orm.py). This is
the one test that wires the REAL `ConversationManager`, `SessionManager`,
`ContextBuilder`, `PromptBuilder`, `LLMRouter`, `ToolExecutor`,
`SafetyLayer`, `CostTracker`, `SessionMemory`, and the real registered
tools (`get_wallet_balance`, `get_payment_status`,
`request_limit_increase`) against a REAL Postgres (testcontainers, same
pattern as tests/models/test_orm.py — including running the Alembic
migration) and a real-shaped Redis (`RedisClient` wrapping
`tests/support/fake_redis.py`'s `FakeRedis`, an in-memory stand-in for
`redis.asyncio.Redis` promoted out of tests/data/test_redis_client.py for
exactly this reuse). The ONLY stub anywhere in this test is the LLM
(`tests/fakes.py`'s `FakeLLM`, wrapped by the REAL `LLMRouter` — so the
real streamed-tool-call reassembly logic runs too, not just the
downstream tool loop).

Two "spy" wrappers (`_RecordingToolExecutor`, `_RecordingSafetyLayer`)
sit between `ConversationManager` and its real `ToolExecutor`/
`SafetyLayer` collaborators. They do not replace or alter any real
logic — every method delegates straight through to the real instance —
they only record what was called/returned so assertions below don't have
to re-derive expected values by hand (a second, hand-maintained copy of
the pipeline's expected behavior would be exactly the kind of drift risk
this test exists to catch, not reproduce).

Phase-2 adaptation of the canonical script (per the Phase-2 plan's scope
decisions #1-#2 — screen context doesn't exist until Phase 4): turn 1 is
the synthetic call-open trigger (empty utterance) and gets a
profile-only greeting, not a screen-aware one; turn 2 has Rajesh state
his own problem in words instead of the agent reacting to a screen.
Turns 3-9 reproduce the canonical script's substance (docs/10-tool-
calling.md §6's T5-T7 trace in particular). Wording throughout is this
test's own choice (there is no live LLM to script against) — the
STRUCTURE (which tools fire, in what order, the confirm-gate hold/
release, the idempotency-key turns, the final reference-id shape) is
what must match, and every "voiced" number in a scripted reply is
independently re-verified by the REAL `SafetyLayer.screen_output()`
against that turn's own real tool results — see the module docstring's
"no hallucinated numbers" note below for why replies deliberately don't
restate figures from an EARLIER turn's tool results.

Judgment calls / honesty notes, flagged per house style:

1. **No hallucinated numbers, proven by the real checker, not just by
   test-author intent.** `SafetyLayer.screen_output()` is deliberately
   never stubbed — every scripted assistant reply that mentions a ₹
   amount is checked, for real, against that SAME TURN's own tool
   results (`ShortTermMemory.tool_results` is rebuilt fresh every turn,
   confirmed by reading `conversation_manager.py`'s `_run_turn`). This
   has a real, non-obvious consequence for turn 7's reply: the executed
   `request_limit_increase` result's `data` is `{request_id, status,
   eta_hours}` — it does NOT carry `current_limit`/`requested_limit`
   again (unlike turn 5's `gate.action.summary`, which does). So turn
   7's scripted reply deliberately does NOT restate "₹25,000 to
   ₹50,000" — doing so would have been rejected by the real
   `screen_output()` check as an unverified fact for THAT turn, even
   though the identical figures were perfectly verified two turns
   earlier. This is exercised, not routed around: `screen_output`'s
   turn-scoped verification is a genuine, intentional Phase-2 property
   this test surfaces rather than hides.
2. **`merchant_limits.limit_paise`/`used_paise` do NOT change at turn 7,
   and this test does not assert that they do.** Reading
   `LimitRepo.submit_increase_request` closely: it only ever sets
   `request_id`/`requested_paise`/`request_status`/`requested_at` — the
   live `limit_paise` the merchant is actually capped at only moves once
   the documented-but-unbuilt "4-business-hour auto-approve job"
   (`LimitRepo`'s own module docstring) runs, which is not part of this
   diff or any earlier one. The task brief's suggested assertion phrasing
   ("assert the row's `limit_paise` or `used_paise` actually changed")
   does not hold against the real implementation; asserting it would
   make this test wrong, not the code, so this test instead asserts the
   fields that genuinely do change (`request_id` shape, `requested_paise`,
   `request_status`, `requested_at`) and leaves this note rather than
   silently asserting something false.
3. **`conversation_turns` ends up with 12 rows, not 9, and every one has
   `role='agent'`.** `SessionManager.end()` drains `session:{id}:turns`
   verbatim; that Redis list is populated by `CostTracker.finalize()`,
   which appends one record per `record_turn()` call — i.e. once per
   *LLM completion* (every stream round, tool-call rounds included), not
   once per user-facing conversation turn (`ConversationManager` itself
   never appends turn records — see that module's own judgment call #3).
   This 9-turn conversation makes 12 LLM stream calls (turns 3, 5, 7 each
   need a tool round + a follow-up content round; the other six turns
   are one round each: 1+1+2+1+2+1+2+1+1=12), and `CostTracker.finalize`
   always records `"role": "agent"` (every completion it prices is the
   agent's). So there is no per-user-turn row and no `role='user'` row
   anywhere in `conversation_turns` in Phase 2 — this test asserts the
   real count (pinned to 12, with the arithmetic spelled out) and the
   real all-agent role set, rather than the "one row per user turn"
   shape a first read of the task brief might suggest.
4. **`ConversationManager`'s module-level `tracer` can be bound to a
   stale/no-op provider depending on test execution order.**
   `opentelemetry.trace.ProxyTracer._tracer` (verified against the
   installed `opentelemetry-api` by reading its source directly) resolves
   and then PERMANENTLY caches `_real_tracer` the first time a real
   `TracerProvider` is active when it's accessed — it never re-checks
   after that. `conversation_manager.py`'s `tracer = get_tracer(__name__)`
   is a module-level object created once at import time; if some earlier
   test in the same pytest session already exercised `on_stt_final()`
   while a DIFFERENT `TracerProvider` was globally active (e.g.
   `tests/api/test_routes.py`'s `TestClient(app)` triggers the real app
   lifespan's `setup_observability()`, and `tests/agent/` sorts before
   `tests/api/` alphabetically so `on_stt_final` calls there happen
   before that provider is ever set), this module's cached tracer would
   silently keep exporting to that OTHER provider forever, regardless of
   this test's own `setup_observability(..., exporter=...)` call. This
   test defends against that explicitly (see `_reset_tracer_provider`
   below) by resetting the cached `_real_tracer` back to `None`, in
   addition to the global provider reset `tests/obs/test_tracing.py`
   already established the pattern for — otherwise the turn-span-count
   assertion would be order-dependent and could silently see 0 spans.

Docker note (same, already-accepted gap as tests/models/test_orm.py): no
Docker is available in the authoring environment, so this test cannot
actually be executed and observed green here. It was written and
cross-checked by reading every real collaborator's actual signature and
behavior (not guessed), matching the same honesty convention
tests/models/test_orm.py itself already establishes for this project.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from collections.abc import Generator
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import cast

import pytest
from opentelemetry import trace as otel_trace
from opentelemetry.sdk.trace import TracerProvider as SdkTracerProvider
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.util._once import Once
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from testcontainers.postgres import PostgresContainer

import app.tools  # noqa: F401 -- import side effect: registers the 3 Phase-2 tools
import app.tools.registry as registry_module
from app.agent import conversation_manager as conversation_manager_module
from app.agent.context_builder import ContextBuilder
from app.agent.conversation_manager import ConversationManager
from app.agent.cost_tracker import CostTracker
from app.agent.llm_router import LLMRouter
from app.agent.prompt_builder import PromptBuilder
from app.agent.safety_layer import SafetyLayer
from app.agent.session_manager import SessionManager
from app.agent.tool_executor import ToolExecutor
from app.config import Settings
from app.data.engine import create_engine_and_sessionmaker
from app.data.redis_client import RedisClient
from app.domain.interfaces import LLMProvider, LLMRouterProto
from app.domain.types import (
    ContextBundle,
    EndReason,
    PendingConfirm,
    SafetyVerdict,
    SessionUser,
    TokenEvent,
    ToolCall,
    ToolCallsEvent,
    ToolInvocationStatus,
    ToolResult,
    UsageEvent,
)
from app.memory.session_memory import SessionMemory
from app.models.orm import CallCost, Conversation, ConversationTurn, MerchantLimit, ToolInvocation
from app.obs.tracing import SPAN_TURN, setup_observability
from app.tools.registry import registry
from scripts import seed as seed_module
from tests.fakes import FakeLLM
from tests.support.fake_redis import FakeRedis

# backend/tests/e2e/test_canonical_conversation.py -> backend/
BACKEND_ROOT = Path(__file__).resolve().parents[2]

_REQUEST_ID_RE = re.compile(r"^LMT-\d{4}-\d{4}-\d{4}$")

# The 12 LLM-stream-round arithmetic this test pins (module docstring
# note #3): turns 1,2,4,6,8,9 are one round each (6); turns 3,5,7 are a
# tool round + a follow-up content round each (6) -> 12 total.
_EXPECTED_STREAM_CALLS = 12
_EXPECTED_CONVERSATION_TURN_SPANS = 9


# --------------------------------------------------------------------------
# Postgres testcontainer + Alembic migration (identical pattern to
# tests/models/test_orm.py's postgres_container/database_url/_migrated).
# --------------------------------------------------------------------------


@pytest.fixture(scope="session")
def postgres_container() -> Generator[PostgresContainer, None, None]:
    """One `pgvector/pgvector:pg16` container for the whole test session."""
    with PostgresContainer("pgvector/pgvector:pg16", driver="asyncpg") as container:
        yield container


@pytest.fixture(scope="session")
def database_url(postgres_container: PostgresContainer) -> str:
    """asyncpg-driver URL, e.g. postgresql+asyncpg://test:test@host:port/test."""
    return postgres_container.get_connection_url()


@pytest.fixture(scope="session", autouse=True)
def _migrated(database_url: str) -> None:
    """Run `alembic upgrade head` against the container exactly once per
    test session (docs/12 §10 — migrations, not `create_all()`)."""
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=str(BACKEND_ROOT),
        env={**os.environ, "DATABASE_URL": database_url},
        check=True,
    )


# --------------------------------------------------------------------------
# OpenTelemetry reset (tests/obs/test_tracing.py's pattern, extended with
# module docstring note #4's defensive ProxyTracer un-cache).
# --------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_tracer_provider() -> Generator[None, None, None]:
    """Reset the global `TracerProvider` + its `Once()` guard before and
    after this test (tests/obs/test_tracing.py's established pattern —
    without resetting `_TRACER_PROVIDER_SET_ONCE`, `setup_observability`
    would silently configure nothing here). ALSO un-caches
    `ConversationManager`'s module-level `tracer`'s resolved
    `_real_tracer`, if it has one — module docstring note #4 explains why
    this extra step is needed for THIS test specifically."""
    otel_trace._TRACER_PROVIDER = None
    otel_trace._TRACER_PROVIDER_SET_ONCE = Once()
    _uncache_conversation_manager_tracer()
    yield
    otel_trace._TRACER_PROVIDER = None
    otel_trace._TRACER_PROVIDER_SET_ONCE = Once()
    _uncache_conversation_manager_tracer()


def _uncache_conversation_manager_tracer() -> None:
    tracer_obj = conversation_manager_module.tracer
    if hasattr(tracer_obj, "_real_tracer"):
        tracer_obj._real_tracer = None  # type: ignore[attr-defined]


# --------------------------------------------------------------------------
# FakeLLM event-scripting helpers — build the exact `LLMEvent` sequence
# one `LLMRouter.stream()` round needs. `ToolCallsEvent` is yielded
# directly (a legal `LLMEvent` member `OpenRouterLLM` never emits but
# `LLMRouter`'s own assembler explicitly passes through unmodified,
# confirmed by reading `_ToolCallAssembler.process()`) rather than faking
# raw streamed fragments — this still exercises the REAL LLMRouter's wire
# adaptation and event handling, just not its fragment-reassembly path
# (which has its own dedicated unit tests in tests/agent/test_llm_router.py).
# --------------------------------------------------------------------------


def _content_round(
    text: str, *, model: str, prompt_tokens: int = 900, completion_tokens: int = 50
) -> tuple[TokenEvent, UsageEvent]:
    return (
        TokenEvent(delta={"content": text}),
        UsageEvent(
            model=model,
            usage={"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens},
        ),
    )


def _tool_round(
    *calls: ToolCall, model: str, prompt_tokens: int = 950, completion_tokens: int = 30
) -> tuple[ToolCallsEvent, UsageEvent]:
    return (
        ToolCallsEvent(tool_calls=calls),
        UsageEvent(
            model=model,
            usage={"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens},
        ),
    )


# --------------------------------------------------------------------------
# Recording spies — real collaborators underneath, see module docstring.
# --------------------------------------------------------------------------


@dataclass
class _ToolRound:
    calls: list[ToolCall]
    principal: SessionUser
    session_id: str
    turn_no: int
    affirmed: bool
    results: list[ToolResult]


class _RecordingToolExecutor:
    """Delegates every `execute()` call to a REAL `ToolExecutor`,
    recording each round for later assertion. No pipeline logic lives
    here — see module docstring."""

    def __init__(self, real: ToolExecutor) -> None:
        self._real = real
        self.rounds: list[_ToolRound] = []

    async def execute(
        self,
        calls: list[ToolCall],
        *,
        principal: SessionUser,
        session_id: str,
        turn_no: int,
        affirmed: bool = False,
    ) -> list[ToolResult]:
        results = await self._real.execute(
            calls,
            principal=principal,
            session_id=session_id,
            turn_no=turn_no,
            affirmed=affirmed,
        )
        self.rounds.append(
            _ToolRound(
                calls=calls,
                principal=principal,
                session_id=session_id,
                turn_no=turn_no,
                affirmed=affirmed,
                results=results,
            )
        )
        return results


@dataclass
class _ScreenOutputCall:
    text: str
    tool_results: list[ToolResult]
    verdict: SafetyVerdict


@dataclass
class _AffirmationCall:
    utterance: str
    pending: PendingConfirm | None
    result: bool


class _RecordingSafetyLayer:
    """Delegates every call to a REAL `SafetyLayer` — same rationale as
    `_RecordingToolExecutor` above. `authorize_tool` is implemented only
    to satisfy `SafetyLayerProto`'s shape; `ConversationManager` never
    calls it directly (only `ToolExecutor` does, and that class is wired
    to the real, unwrapped `SafetyLayer` instance directly, below)."""

    def __init__(self, real: SafetyLayer) -> None:
        self._real = real
        self.screen_output_calls: list[_ScreenOutputCall] = []
        self.affirmation_calls: list[_AffirmationCall] = []

    def fence_input(self, bundle: ContextBundle) -> ContextBundle:
        return self._real.fence_input(bundle)

    def screen_output(self, text: str, tool_results: list[ToolResult]) -> SafetyVerdict:
        verdict = self._real.screen_output(text, tool_results)
        self.screen_output_calls.append(
            _ScreenOutputCall(text=text, tool_results=tool_results, verdict=verdict)
        )
        return verdict

    def classify_affirmation(self, utterance: str, pending: PendingConfirm | None) -> bool:
        result = self._real.classify_affirmation(utterance, pending)
        self.affirmation_calls.append(
            _AffirmationCall(utterance=utterance, pending=pending, result=result)
        )
        return result

    def authorize_tool(self, call: ToolCall, principal: SessionUser) -> bool:
        return self._real.authorize_tool(call, principal)


# --------------------------------------------------------------------------
# Postgres assertion helpers (kept out of the main test function to keep
# it readable — each opens its own short-lived session, mirroring how the
# real components under test open theirs).
# --------------------------------------------------------------------------


async def _tool_invocations_by_key(
    sessionmaker: async_sessionmaker[AsyncSession], session_id: str
) -> dict[tuple[str, int | None], ToolInvocation]:
    async with sessionmaker() as session:
        result = await session.execute(
            select(ToolInvocation).where(ToolInvocation.session_id == session_id)
        )
        rows = list(result.scalars().all())
    return {(row.tool_name, row.turn_no): row for row in rows}


async def _merchant_limit_row(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> MerchantLimit | None:
    async with sessionmaker() as session:
        return await session.get(
            MerchantLimit, (seed_module.MERCHANT_ID, seed_module.LIMIT_TYPE)
        )


async def _conversation_row(
    sessionmaker: async_sessionmaker[AsyncSession], session_id: str
) -> Conversation | None:
    async with sessionmaker() as session:
        return await session.get(Conversation, session_id)


async def _conversation_turn_rows(
    sessionmaker: async_sessionmaker[AsyncSession], session_id: str
) -> list[ConversationTurn]:
    async with sessionmaker() as session:
        result = await session.execute(
            select(ConversationTurn).where(ConversationTurn.session_id == session_id)
        )
        return list(result.scalars().all())


async def _call_cost_row(
    sessionmaker: async_sessionmaker[AsyncSession], session_id: str
) -> CallCost | None:
    async with sessionmaker() as session:
        return await session.get(CallCost, session_id)


# --------------------------------------------------------------------------
# The test
# --------------------------------------------------------------------------


async def test_canonical_conversation_resolves_end_to_end(database_url: str) -> None:
    # `Settings`' SecretStr fields accept plain str at construction (pydantic
    # coerces it), but mypy's stub types the *parameter* as SecretStr — the
    # same mismatch tests/conftest.py's settings_factory and
    # tests/obs/test_tracing.py's _settings() helper already carry a
    # `# type: ignore[arg-type]` for. Built as one `Settings(**kwargs)` call
    # (rather than one keyword arg per line) so the single ignore comment
    # actually covers every argument, not just the last one.
    settings_kwargs: dict[str, object] = {
        "jwt_secret": "test-jwt-secret",
        "database_url": database_url,
        "openrouter_api_key": "test-openrouter-key",
    }
    settings = Settings(**settings_kwargs)  # type: ignore[arg-type]
    dialogue_model = settings.openrouter_dialogue_model

    exporter = InMemorySpanExporter()
    setup_observability(settings, exporter=exporter)

    engine, sessionmaker = create_engine_and_sessionmaker(settings)
    sessionmaker_snapshot = registry_module._sessionmaker  # type: ignore[attr-defined]
    try:
        # -- Seed the canonical fixtures (in-process, via the real seed
        # module's entry point — docs/12 §8) -------------------------
        async with sessionmaker() as seed_session:
            await seed_module.seed(seed_session)

        # -- Wire every REAL collaborator -----------------------------
        fake_redis = FakeRedis()
        redis_client = RedisClient(fake_redis, session_ttl_seconds=settings.session_ttl_seconds)  # type: ignore[arg-type]
        session_memory = SessionMemory(redis_client)
        context_builder = ContextBuilder(sessionmaker, session_memory)
        prompt_builder = PromptBuilder()
        fake_llm = FakeLLM()
        # mypy reads LLMProvider.stream's `async def ... -> AsyncIterator`
        # Protocol spelling as coroutine-returning, so async-generator
        # implementations (FakeLLM, OpenRouterLLM) don't structurally match
        # under mypy even though they are exactly what the codebase ships
        # (LLMRouter's own module docstring documents this; the identical
        # cast is tests/agent/test_llm_router.py's established convention).
        llm_router = LLMRouter(cast(LLMProvider, fake_llm), settings)
        real_safety = SafetyLayer(registry)
        real_tool_executor = ToolExecutor(
            registry=registry, safety=real_safety, redis=redis_client, sessionmaker=sessionmaker
        )
        recording_executor = _RecordingToolExecutor(real_tool_executor)
        recording_safety = _RecordingSafetyLayer(real_safety)
        cost_tracker = CostTracker(settings, session_factory=sessionmaker, redis=redis_client)
        session_manager = SessionManager(sessionmaker, redis_client)

        session = await session_manager.create(
            user_id=seed_module.MERCHANT_ID, screen_context=None, recent_events=[]
        )
        session_id = session.session_id

        manager = ConversationManager(
            session=session,
            context_builder=context_builder,
            prompt_builder=prompt_builder,
            # Same coroutine-vs-generator Protocol mismatch as the FakeLLM
            # cast above, one level up: LLMRouterProto.stream() has the
            # identical `async def ... -> AsyncIterator` spelling and
            # LLMRouter.stream() is a real async generator.
            llm_router=cast(LLMRouterProto, llm_router),
            tool_executor=recording_executor,
            safety_layer=recording_safety,
            cost_tracker=cost_tracker,
            session_memory=session_memory,
            tool_registry=registry,
        )

        # ==============================================================
        # Turn 1 — synthetic call-open trigger (empty utterance):
        # profile-only greeting, no tool call (Phase-2 plan decision #2).
        # ==============================================================
        greeting = "Hello! This is Asha from VyaparPay. How can I help you today?"
        fake_llm.script_turn(*_content_round(greeting, model=dialogue_model))

        reply1 = await manager.on_stt_final("")
        assert reply1 == greeting
        assert recording_executor.rounds == []

        # ==============================================================
        # Turn 2 — Rajesh states his own problem (Phase-2 adaptation:
        # no screen context exists yet to react to). No tool call.
        # ==============================================================
        t2_ack = "I'm sorry to hear that — let me look into it for you right away."
        fake_llm.script_turn(*_content_round(t2_ack, model=dialogue_model))

        reply2 = await manager.on_stt_final(
            "My ₹245 payment to Amazon Business got declined, can you help?"
        )
        assert reply2 == t2_ack
        assert recording_executor.rounds == []

        # ==============================================================
        # Turn 3 — the LLM emits BOTH get_wallet_balance and
        # get_payment_status as ONE parallel tool_calls batch (docs/10 §2:
        # sibling reads run under asyncio.gather), then answers from the
        # real tool data.
        # ==============================================================
        wallet_call = ToolCall(id="call_wallet", name="get_wallet_balance", arguments={})
        payment_call = ToolCall(
            id="call_payment",
            name="get_payment_status",
            arguments={"payment_id": seed_module.TXN_ID},
        )
        t3_reply = (
            "Good news — you have ₹18,450 in your wallet, so the money is there. "
            "The ₹245 payment to Amazon Business was declined because of your daily "
            "transaction limit: you've used ₹24,890 of your ₹25,000 daily limit today."
        )
        fake_llm.script_turn(*_tool_round(wallet_call, payment_call, model=dialogue_model))
        fake_llm.script_turn(*_content_round(t3_reply, model=dialogue_model))

        reply3 = await manager.on_stt_final("What happened? Do I have the money for it?")

        assert reply3 == t3_reply
        assert len(recording_executor.rounds) == 1
        t3_round = recording_executor.rounds[0]
        assert t3_round.turn_no == 3
        assert t3_round.affirmed is False
        assert [c.name for c in t3_round.calls] == ["get_wallet_balance", "get_payment_status"]
        # No hallucinated payment_id: the exact seeded TXN_ID, never a
        # fabricated one.
        assert t3_round.calls[0].arguments == {}
        assert t3_round.calls[1].arguments == {"payment_id": seed_module.TXN_ID}

        wallet_result, payment_result = t3_round.results
        assert wallet_result.ok is True
        assert wallet_result.data is not None
        assert wallet_result.data["balance"] == 18_450
        assert wallet_result.data["currency"] == "INR"
        assert payment_result.ok is True
        assert payment_result.data == {
            "payment_id": seed_module.TXN_ID,
            "status": "declined",
            "amount": 245,
            "counterparty": "Amazon Business",
            "decline_code": "DAILY_LIMIT_EXCEEDED",
            "limit_context": {
                "limit": 25_000,
                "used_today": 24_890,
                "resets_at": "2026-07-25T00:00:00+05:30",
            },
        }

        # The real, unstubbed safety layer independently re-verified
        # every number in t3_reply against t3's own tool results.
        t3_screen = recording_safety.screen_output_calls[-1]
        assert t3_screen.text == t3_reply
        assert t3_screen.verdict.allowed is True

        # ==============================================================
        # Turn 4 — Rajesh asks for the limit raise; Asha does not call
        # request_limit_increase yet (docs/10 §6: the call fires at
        # turn 5, after being proposed).
        # ==============================================================
        t4_reply = "Sure, I can help with that — let me set up a limit increase request for you."
        fake_llm.script_turn(*_content_round(t4_reply, model=dialogue_model))

        reply4 = await manager.on_stt_final("Can you raise my daily limit so this goes through?")
        assert reply4 == t4_reply
        assert len(recording_executor.rounds) == 1  # unchanged

        # ==============================================================
        # Turn 5 — the LLM proposes request_limit_increase. No pending
        # confirm exists yet, so ToolExecutor MUST hold the gate, not
        # execute — assert the model literally cannot execute here.
        # ==============================================================
        limit_call_t5 = ToolCall(
            id="call_limit_5",
            name="request_limit_increase",
            arguments={"current_limit": 25_000, "requested_limit": 50_000},
        )
        t5_reply = (
            "I can raise your daily transaction limit from ₹25,000 to ₹50,000. "
            "Shall I go ahead and submit that request?"
        )
        fake_llm.script_turn(*_tool_round(limit_call_t5, model=dialogue_model))
        fake_llm.script_turn(*_content_round(t5_reply, model=dialogue_model))

        reply5 = await manager.on_stt_final(
            "Please request the maximum increase you're able to for my account."
        )

        assert reply5 == t5_reply
        assert len(recording_executor.rounds) == 2
        t5_round = recording_executor.rounds[1]
        assert t5_round.turn_no == 5
        assert t5_round.affirmed is False  # nothing pending yet at turn-open
        assert t5_round.calls[0].arguments == {"current_limit": 25_000, "requested_limit": 50_000}

        t5_result = t5_round.results[0]
        assert t5_result.ok is False
        assert t5_result.status is ToolInvocationStatus.PENDING_CONFIRM
        assert t5_result.gate is not None
        assert t5_result.gate["status"] == "pending_confirm"
        # Confirm-gate hold: the exact idempotency key format,
        # session_id:tool:turn — the gate-hold key (turn 5).
        assert t5_result.idempotency_key == f"{session_id}:request_limit_increase:5"

        t5_screen = recording_safety.screen_output_calls[-1]
        assert t5_screen.verdict.allowed is True  # ₹25,000/₹50,000 verified via gate.action.summary

        # Real-pipeline proof the model cannot execute at turn 5: no
        # merchant_limits write happened, and no idempotency VALUE (as
        # opposed to the audit row's key label) was ever stored.
        limit_row_after_t5 = await _merchant_limit_row(sessionmaker)
        assert limit_row_after_t5 is not None
        assert limit_row_after_t5.request_status is None
        assert await redis_client.get_idempotency(session_id, "request_limit_increase", 5) is None

        pending_after_t5 = await redis_client.get_pending_confirm(session_id)
        assert pending_after_t5 == PendingConfirm(
            tool="request_limit_increase",
            args={"current_limit": 25_000, "requested_limit": 50_000},
            proposed_turn=5,
            invocation_id="",  # ToolExecutor._hold_gate's documented placeholder
        )

        # Supplementary direct check (task's "or directly" option) on the
        # REAL, unwrapped SafetyLayer instance — independent of whatever
        # ConversationManager does with the result.
        assert real_safety.classify_affirmation("Yes, do it.", pending_after_t5) is True

        # ==============================================================
        # Turn 6 — "Yes, do it." classifies as an affirmation of the
        # turn-5 pending, but the model does NOT re-emit the tool call
        # this turn (pure acknowledgement) — the re-emission must land as
        # turn 7's FIRST tool round, respecting the round-scoping fix
        # (conversation_manager.py's judgment call #10: affirmed is only
        # honored on a turn's first tool round, and only a genuinely new
        # user turn gets a fresh affirmed classification at all).
        # ==============================================================
        t6_reply = "Sure, one moment while I get that submitted for you."
        fake_llm.script_turn(*_content_round(t6_reply, model=dialogue_model))

        reply6 = await manager.on_stt_final("Yes, do it.")

        assert reply6 == t6_reply
        assert len(recording_executor.rounds) == 2  # unchanged — no tool call this turn
        t6_affirmation = recording_safety.affirmation_calls[-1]
        assert t6_affirmation.utterance == "Yes, do it."
        assert t6_affirmation.result is True  # classified as an affirmation...
        # ...but never consumed: no execute() call happened this turn.

        # ==============================================================
        # Turn 7 — the LLM re-emits request_limit_increase with the SAME
        # args, as round 1 of THIS turn. The turn-5 pending still matches
        # (turn 6 never touched it) and this turn's own affirmed
        # classification is True -> executes for real.
        # ==============================================================
        limit_call_t7 = ToolCall(
            id="call_limit_7",
            name="request_limit_increase",
            arguments={"current_limit": 25_000, "requested_limit": 50_000},
        )
        t7_reply = (
            "All done — I've submitted your request to raise the daily limit, and it "
            "should be reviewed within about 4 hours."
        )
        fake_llm.script_turn(*_tool_round(limit_call_t7, model=dialogue_model))
        fake_llm.script_turn(*_content_round(t7_reply, model=dialogue_model))

        reply7 = await manager.on_stt_final("Go ahead, please.")

        assert reply7 == t7_reply
        assert len(recording_executor.rounds) == 3
        t7_round = recording_executor.rounds[2]
        assert t7_round.turn_no == 7
        assert t7_round.affirmed is True  # THIS turn's own classification
        assert t7_round.calls[0].arguments == {"current_limit": 25_000, "requested_limit": 50_000}

        t7_result = t7_round.results[0]
        assert t7_result.ok is True
        assert t7_result.status is ToolInvocationStatus.OK
        assert t7_result.data is not None
        assert _REQUEST_ID_RE.match(t7_result.data["request_id"])
        assert t7_result.data["status"] == "submitted"
        assert t7_result.data["eta_hours"] == 4
        # Execution idempotency key: turn 7, not turn 5.
        assert t7_result.idempotency_key == f"{session_id}:request_limit_increase:7"

        t7_screen = recording_safety.screen_output_calls[-1]
        assert t7_screen.verdict.allowed is True

        # A REAL merchant_limits UPDATE happened — see module docstring
        # note #2 for exactly which columns (not limit_paise/used_paise).
        limit_row_after_t7 = await _merchant_limit_row(sessionmaker)
        assert limit_row_after_t7 is not None
        assert _REQUEST_ID_RE.match(limit_row_after_t7.request_id or "")
        assert limit_row_after_t7.requested_paise == 50_000 * 100
        assert limit_row_after_t7.request_status == "submitted"
        assert limit_row_after_t7.requested_at is not None
        assert limit_row_after_t7.limit_paise == seed_module.DAILY_LIMIT_PAISE  # unchanged
        assert limit_row_after_t7.used_paise == seed_module.DAILY_LIMIT_USED_PAISE  # unchanged

        assert await redis_client.get_pending_confirm(session_id) is None  # consumed
        idem_raw = await redis_client.get_idempotency(session_id, "request_limit_increase", 7)
        assert idem_raw is not None
        idem_envelope = json.loads(idem_raw)
        assert idem_envelope["ok"] is True
        assert idem_envelope["status"] == "ok"
        assert _REQUEST_ID_RE.match(idem_envelope["data"]["request_id"])

        # ==============================================================
        # Turn 8 — dialogue only, exercises the conversation continuing
        # normally post-execution.
        # ==============================================================
        t8_reply = (
            "The team will review it and you'll be notified once it's approved. "
            "Is there anything else I can help with?"
        )
        fake_llm.script_turn(*_content_round(t8_reply, model=dialogue_model))

        reply8 = await manager.on_stt_final("What happens next?")
        assert reply8 == t8_reply
        assert len(recording_executor.rounds) == 3  # unchanged

        # ==============================================================
        # Turn 9 — dialogue only, sets up call-end.
        # ==============================================================
        t9_reply = "You're welcome! Have a great day."
        fake_llm.script_turn(*_content_round(t9_reply, model=dialogue_model))

        reply9 = await manager.on_stt_final("No, that's all. Thanks!")
        assert reply9 == t9_reply
        assert len(recording_executor.rounds) == 3  # unchanged

        # Every scripted stream() call was consumed exactly once, in
        # order (FakeLLM raises loudly on an under/over-scripted call —
        # this length check pins the round arithmetic module docstring
        # note #3 depends on).
        assert len(fake_llm.calls) == _EXPECTED_STREAM_CALLS
        offered_tools = {t["function"]["name"] for t in (fake_llm.calls[0].tools or [])}
        assert offered_tools == {
            "get_wallet_balance",
            "get_payment_status",
            "request_limit_increase",
        }

        # ==============================================================
        # Call end: finalize cost, then end the session (post-call drain).
        # ==============================================================
        await cost_tracker.finalize(session_id)
        redis_turns_before_end = await redis_client.get_turns(session_id)
        await session_manager.end(session_id, EndReason.HANGUP)

        # -- conversations -------------------------------------------
        conversation_row = await _conversation_row(sessionmaker, session_id)
        assert conversation_row is not None
        assert conversation_row.state == "ended"
        assert conversation_row.ended_at is not None

        # -- conversation_turns (module docstring note #3) ------------
        turn_rows = await _conversation_turn_rows(sessionmaker, session_id)
        assert len(redis_turns_before_end) == _EXPECTED_STREAM_CALLS
        assert len(turn_rows) == len(redis_turns_before_end)
        assert {row.role for row in turn_rows} == {"agent"}

        # -- call_costs (upserted once) --------------------------------
        cost_row = await _call_cost_row(sessionmaker, session_id)
        assert cost_row is not None
        assert cost_row.total_usd > Decimal("0")
        assert cost_row.llm_dialogue_usd == cost_row.total_usd
        assert cost_row.llm_utility_usd == Decimal("0")

        # -- tool_invocations: exactly 4 audit rows, the real audit trail
        invocations = await _tool_invocations_by_key(sessionmaker, session_id)
        assert set(invocations) == {
            ("get_wallet_balance", 3),
            ("get_payment_status", 3),
            ("request_limit_increase", 5),
            ("request_limit_increase", 7),
        }
        wallet_row = invocations[("get_wallet_balance", 3)]
        assert wallet_row.status == "ok"
        assert wallet_row.input == {}
        assert wallet_row.output is not None
        assert wallet_row.output["balance"] == 18_450
        assert wallet_row.idempotency_key is None  # reads carry no idempotency semantics

        payment_row = invocations[("get_payment_status", 3)]
        assert payment_row.status == "ok"
        assert payment_row.input == {"payment_id": seed_module.TXN_ID}
        assert payment_row.output is not None
        assert payment_row.output["decline_code"] == "DAILY_LIMIT_EXCEEDED"

        gate_row = invocations[("request_limit_increase", 5)]
        assert gate_row.status == "pending_confirm"
        assert gate_row.input == {"current_limit": 25_000, "requested_limit": 50_000}
        assert gate_row.idempotency_key == f"{session_id}:request_limit_increase:5"

        executed_row = invocations[("request_limit_increase", 7)]
        assert executed_row.status == "ok"
        assert executed_row.input == {"current_limit": 25_000, "requested_limit": 50_000}
        assert executed_row.output is not None
        assert _REQUEST_ID_RE.match(executed_row.output["request_id"])
        assert executed_row.idempotency_key == f"{session_id}:request_limit_increase:7"

        # -- spans: one `turn` span per on_stt_final() call, 9 total ---
        # `get_tracer_provider()` is typed to the abstract API TracerProvider
        # (no force_flush); `setup_observability()` above always installs
        # the concrete SDK one, which does — same gap
        # tests/obs/test_tracing.py's equivalent call already carries.
        cast(SdkTracerProvider, otel_trace.get_tracer_provider()).force_flush()
        spans = exporter.get_finished_spans()
        turn_spans = [s for s in spans if s.name == SPAN_TURN]
        assert len(turn_spans) == _EXPECTED_CONVERSATION_TURN_SPANS

        tool_exec_spans = [s for s in spans if s.name.startswith("tool.exec.")]
        # 4 calls total: get_wallet_balance + get_payment_status (turn 3),
        # request_limit_increase at the gate-hold (turn 5) AND the
        # execution (turn 7) — every ToolExecutor._execute_one call opens
        # one, gated or not, so the name repeats but the span doesn't.
        assert len(tool_exec_spans) == 4
        assert {s.name for s in tool_exec_spans} == {
            "tool.exec.get_wallet_balance",
            "tool.exec.get_payment_status",
            "tool.exec.request_limit_increase",
        }
        assert len(spans) == _EXPECTED_CONVERSATION_TURN_SPANS + len(tool_exec_spans)
    finally:
        registry_module._sessionmaker = sessionmaker_snapshot  # type: ignore[attr-defined]
        await engine.dispose()


__all__: list[str] = []
