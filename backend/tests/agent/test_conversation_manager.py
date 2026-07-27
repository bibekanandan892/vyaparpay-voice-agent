"""Unit tests for app.agent.conversation_manager.ConversationManager.

Every collaborator is a Protocol fake scripted per test (docs/05 §1.1's
testability argument: the brain runs on `stt.final` strings and fakes,
no transport, no Docker). The tool-call event is a LOCAL pydantic model
carrying `tool_calls` — deliberately not imported from task 4.3's
sibling branch — pinning the manager's documented structural detection
(judgment call #1 in the module docstring) rather than an isinstance
coupling that would force a merge order between the two PRs.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from opentelemetry.trace import Span
from pydantic import BaseModel, ConfigDict

from app.agent.conversation_manager import (
    _BLOCKED_OUTPUT_FALLBACK,
    _TURN_FAILURE_APOLOGY,
    ConversationManager,
)
from app.domain.types import (
    ContextBundle,
    LLMEvent,
    Message,
    ModelTier,
    PendingConfirm,
    Role,
    SafetyVerdict,
    Session,
    SessionState,
    SessionUser,
    TaskKind,
    TokenEvent,
    ToolCall,
    ToolInvocationStatus,
    ToolResult,
    TurnCost,
    UsageEvent,
)
from app.memory.session_memory import SessionMemory

# ---------------------------------------------------------------------------
# Fakes (Protocol-shaped, scripted per test)
# ---------------------------------------------------------------------------


class ToolCallsBatch(BaseModel):
    """Structural stand-in for task 4.3's reassembled-tool-calls event —
    any object with a `.tool_calls` attribute matches the manager's
    documented duck-typed check."""

    model_config = ConfigDict(frozen=True)

    tool_calls: tuple[ToolCall, ...]


def _bundle() -> ContextBundle:
    return ContextBundle(
        persona="P", business_rules="B", user_profile="U", current_utterance="hi"
    )


class FakeContextBuilder:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.raises: Exception | None = None

    async def build(self, session: Session, *, current_utterance: str) -> ContextBundle:
        if self.raises is not None:
            raise self.raises
        self.calls.append(current_utterance)
        return _bundle()


class FakePromptBuilder:
    def render(self, bundle: ContextBundle) -> list[Message]:
        return [Message(role=Role.SYSTEM, content="SYS")]


class FakeRouter:
    """Queue one list of events per expected stream() call."""

    def __init__(self) -> None:
        self._turns: list[list[Any]] = []
        self.calls: list[list[Message]] = []

    def script(self, *turns: list[Any]) -> None:
        self._turns.extend(turns)

    def route(self, task: TaskKind) -> ModelTier:
        assert task is TaskKind.DIALOGUE
        return ModelTier.DIALOGUE

    async def stream(
        self,
        messages: list[Message],
        *,
        tier: ModelTier,
        tools: list[dict[str, Any]] | None = None,
        ttft_deadline_s: float = 1.5,
    ) -> Any:
        self.calls.append(list(messages))
        assert self._turns, "FakeRouter.stream() called with nothing scripted"
        events = self._turns.pop(0)
        for event in events:
            yield event


def _ok_result(call: ToolCall, data: dict[str, Any] | None = None) -> ToolResult:
    return ToolResult(
        tool_call_id=call.id,
        tool_name=call.name,
        ok=True,
        data=data or {"value": 1},
        status=ToolInvocationStatus.OK,
        latency_ms=5,
    )


class FakeToolExecutor:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def execute(
        self,
        calls: list[ToolCall],
        *,
        principal: SessionUser,
        session_id: str,
        turn_no: int,
        affirmed: bool = False,
    ) -> list[ToolResult]:
        self.calls.append(
            {
                "calls": calls,
                "principal": principal,
                "session_id": session_id,
                "turn_no": turn_no,
                "affirmed": affirmed,
            }
        )
        return [_ok_result(c) for c in calls]


class FakeSafetyLayer:
    def __init__(self) -> None:
        self.affirmation = False
        self.verdict = SafetyVerdict(allowed=True)
        self.classify_args: list[tuple[str, PendingConfirm | None]] = []

    def fence_input(self, bundle: ContextBundle) -> ContextBundle:
        return bundle

    def screen_output(self, text: str, tool_results: list[ToolResult]) -> SafetyVerdict:
        return self.verdict

    def classify_affirmation(self, utterance: str, pending: PendingConfirm | None) -> bool:
        self.classify_args.append((utterance, pending))
        return self.affirmation if pending is not None else False

    def authorize_tool(self, call: ToolCall, principal: SessionUser) -> bool:
        return True


class FakeCostTracker:
    def __init__(self) -> None:
        self.recorded: list[tuple[dict[str, Any], str]] = []
        self.over = False

    def record_turn(self, usage: dict[str, Any], model: str, span: Span) -> TurnCost:
        self.recorded.append((usage, model))
        return TurnCost(
            model=model, input_tokens=10, output_tokens=5, cost_usd=Decimal("0.001")
        )

    def call_total(self) -> Decimal:
        return Decimal("0.001") * len(self.recorded)

    def over_budget(self, cap_usd: Decimal = Decimal("1.00")) -> bool:
        return self.over

    async def finalize(self, session_id: str) -> None:  # pragma: no cover - unused
        raise NotImplementedError


class FakeSessionMemory(SessionMemory):
    """Subclasses the concrete SessionMemory purely for the type; every
    Redis-touching method is overridden with in-memory state."""

    def __init__(self) -> None:  # deliberately no super().__init__
        self.appended: list[Message] = []
        self.pending: PendingConfirm | None = None
        self.costs: list[Decimal] = []

    async def get_pending_confirm(self, session_id: str) -> PendingConfirm | None:
        return self.pending

    async def append_message(self, session_id: str, message: Message) -> None:
        self.appended.append(message)

    async def add_cost(self, session_id: str, delta: Decimal) -> Decimal:
        self.costs.append(delta)
        return sum(self.costs, Decimal("0"))


class FakeRegistry:
    def get(self, name: str) -> Any:
        return None

    def all(self) -> list[Any]:
        return []

    def to_openai_tools(self) -> list[dict[str, Any]]:
        return [{"type": "function", "function": {"name": "t"}}]


def _session() -> Session:
    from datetime import UTC, datetime

    return Session(
        session_id="s1",
        user_id="usr_rajesh01",
        state=SessionState.IN_CALL,
        started_at=datetime.now(UTC),
    )


def _manager(
    router: FakeRouter,
    *,
    safety: FakeSafetyLayer | None = None,
    executor: FakeToolExecutor | None = None,
    memory: FakeSessionMemory | None = None,
    tracker: FakeCostTracker | None = None,
    builder: FakeContextBuilder | None = None,
) -> tuple[ConversationManager, FakeToolExecutor, FakeSessionMemory, FakeCostTracker]:
    executor = executor or FakeToolExecutor()
    memory = memory or FakeSessionMemory()
    tracker = tracker or FakeCostTracker()
    manager = ConversationManager(
        session=_session(),
        context_builder=builder or FakeContextBuilder(),
        prompt_builder=FakePromptBuilder(),
        llm_router=router,
        tool_executor=executor,
        safety_layer=safety or FakeSafetyLayer(),
        cost_tracker=tracker,
        session_memory=memory,
        tool_registry=FakeRegistry(),
    )
    return manager, executor, memory, tracker


def _text_events(*parts: str, model: str = "m") -> list[LLMEvent]:
    events: list[LLMEvent] = [TokenEvent(delta={"content": p}) for p in parts]
    events.append(UsageEvent(model=model, usage={"prompt_tokens": 10}))
    return events


def _call(name: str = "get_wallet_balance", id_: str = "c1") -> ToolCall:
    return ToolCall(id=id_, name=name, arguments={})


# ---------------------------------------------------------------------------
# Plain dialogue turn (no tools)
# ---------------------------------------------------------------------------


async def test_dialogue_turn_returns_joined_stream_text() -> None:
    router = FakeRouter()
    router.script(_text_events("Hello ", "Rajesh."))
    manager, executor, memory, tracker = _manager(router)

    reply = await manager.on_stt_final("my payment failed")

    assert reply == "Hello Rajesh."
    assert executor.calls == []
    assert tracker.recorded and tracker.recorded[0][1] == "m"


async def test_turn_appends_user_then_assistant_to_transcript() -> None:
    router = FakeRouter()
    router.script(_text_events("Sure."))
    manager, _, memory, _ = _manager(router)

    await manager.on_stt_final("what's my balance?")

    assert [m.role for m in memory.appended] == [Role.USER, Role.ASSISTANT]
    assert memory.appended[0].content == "what's my balance?"
    assert memory.appended[1].content == "Sure."


async def test_usage_cost_lands_in_session_memory() -> None:
    router = FakeRouter()
    router.script(_text_events("Ok."))
    manager, _, memory, _ = _manager(router)

    await manager.on_stt_final("hi")

    assert memory.costs == [Decimal("0.001")]


async def test_call_open_trigger_turn_appends_no_user_message() -> None:
    router = FakeRouter()
    router.script(_text_events("Hi, how can I help?"))
    manager, _, memory, _ = _manager(router)

    await manager.on_stt_final("")

    assert [m.role for m in memory.appended] == [Role.ASSISTANT]


async def test_turn_no_increments_across_turns() -> None:
    router = FakeRouter()
    router.script(
        [ToolCallsBatch(tool_calls=(_call(),)), *_text_events()],
        _text_events("a"),
        [ToolCallsBatch(tool_calls=(_call(id_="c2"),)), *_text_events()],
        _text_events("b"),
    )
    manager, executor, _, _ = _manager(router)

    await manager.on_stt_final("one")
    await manager.on_stt_final("two")

    assert [c["turn_no"] for c in executor.calls] == [1, 2]


# ---------------------------------------------------------------------------
# Tool loop (docs/05 §2's arrows 5-7)
# ---------------------------------------------------------------------------


async def test_tool_round_feeds_results_back_and_streams_again() -> None:
    router = FakeRouter()
    calls = (_call("get_wallet_balance", "c1"), _call("get_payment_status", "c2"))
    router.script(
        [ToolCallsBatch(tool_calls=calls), *_text_events()],
        _text_events("Your balance is fine."),
    )
    manager, executor, _, _ = _manager(router)

    reply = await manager.on_stt_final("do I have money?")

    assert reply == "Your balance is fine."
    assert len(executor.calls) == 1
    assert [c.name for c in executor.calls[0]["calls"]] == [
        "get_wallet_balance",
        "get_payment_status",
    ]
    # Second stream call must carry: assistant tool_calls msg + 2 tool msgs.
    second = router.calls[1]
    assert second[-3].role is Role.ASSISTANT
    assert second[-3].tool_calls is not None and len(second[-3].tool_calls) == 2
    assert [m.role for m in second[-2:]] == [Role.TOOL, Role.TOOL]


async def test_executor_receives_principal_session_and_turn() -> None:
    router = FakeRouter()
    router.script([ToolCallsBatch(tool_calls=(_call(),)), *_text_events()], _text_events("d"))
    manager, executor, _, _ = _manager(router)

    await manager.on_stt_final("check it")

    call = executor.calls[0]
    assert call["principal"] == SessionUser(user_id="usr_rajesh01")
    assert call["session_id"] == "s1"
    assert call["turn_no"] == 1
    assert call["affirmed"] is False


async def test_tool_loop_bound_stops_executing_after_two_rounds() -> None:
    router = FakeRouter()
    batch = [ToolCallsBatch(tool_calls=(_call(),)), *_text_events()]
    router.script(list(batch), list(batch), list(batch))
    manager, executor, _, _ = _manager(router)

    reply = await manager.on_stt_final("loop forever")

    assert len(executor.calls) == 2  # the third batch is dropped, not executed
    assert reply == _BLOCKED_OUTPUT_FALLBACK  # no content accumulated


# ---------------------------------------------------------------------------
# Affirmation pairing (docs/10 §4 via judgment call #6)
# ---------------------------------------------------------------------------


async def test_affirmed_flag_passes_through_when_pending_and_yes() -> None:
    router = FakeRouter()
    router.script(
        [ToolCallsBatch(tool_calls=(_call("request_limit_increase"),)), *_text_events()],
        _text_events("Submitted."),
    )
    safety = FakeSafetyLayer()
    safety.affirmation = True
    memory = FakeSessionMemory()
    memory.pending = PendingConfirm(
        tool="request_limit_increase", args={}, proposed_turn=1, invocation_id="i1"
    )
    manager, executor, _, _ = _manager(router, safety=safety, memory=memory)

    await manager.on_stt_final("yes, do it")

    assert executor.calls[0]["affirmed"] is True
    assert safety.classify_args[0] == ("yes, do it", memory.pending)


# ---------------------------------------------------------------------------
# Output screening (docs/05 §3.6, fail closed)
# ---------------------------------------------------------------------------


async def test_blocked_output_falls_back_to_safe_hedge() -> None:
    router = FakeRouter()
    router.script(_text_events("You have ₹99,999."))
    safety = FakeSafetyLayer()
    safety.verdict = SafetyVerdict(allowed=False, reason="unverified amount")
    manager, _, memory, _ = _manager(router, safety=safety)

    reply = await manager.on_stt_final("balance?")

    assert reply == _BLOCKED_OUTPUT_FALLBACK
    assert memory.appended[-1].content == _BLOCKED_OUTPUT_FALLBACK


async def test_blocked_output_uses_layer_safe_text_when_provided() -> None:
    router = FakeRouter()
    router.script(_text_events("bad"))
    safety = FakeSafetyLayer()
    safety.verdict = SafetyVerdict(allowed=False, reason="r", safe_text="Let me re-check.")
    manager, _, _, _ = _manager(router, safety=safety)

    assert await manager.on_stt_final("x") == "Let me re-check."


async def test_allowed_but_masked_output_voices_the_masked_form() -> None:
    router = FakeRouter()
    router.script(_text_events("card 1234"))
    safety = FakeSafetyLayer()
    safety.verdict = SafetyVerdict(allowed=True, safe_text="card ****")
    manager, _, _, _ = _manager(router, safety=safety)

    assert await manager.on_stt_final("x") == "card ****"


# ---------------------------------------------------------------------------
# Failure degradation (docs/05 §3.2)
# ---------------------------------------------------------------------------


async def test_critical_path_failure_returns_apology_never_raises() -> None:
    router = FakeRouter()  # nothing scripted — stream would assert if reached
    builder = FakeContextBuilder()
    builder.raises = RuntimeError("db exploded")
    manager, _, memory, _ = _manager(router, builder=builder)

    reply = await manager.on_stt_final("help")

    assert reply == _TURN_FAILURE_APOLOGY
    assert [m.role for m in memory.appended] == [Role.USER, Role.ASSISTANT]
    assert memory.appended[-1].content == _TURN_FAILURE_APOLOGY


async def test_barge_in_is_a_phase2_noop() -> None:
    router = FakeRouter()
    manager, _, _, _ = _manager(router)

    await manager.on_barge_in()  # must neither raise nor cancel anything
