"""Unit tests for `app.memory.summarizer` — the rolling conversation
summary (docs/09-memory-architecture.md §4).

Doubling strategy, and why it differs by section:

- **Scheduling and boundary arithmetic** need no model, no Redis, and no
  event loop: `should_fold` / `summary_thru_turn` / `window_start_turn`
  are module-level pure functions, tested directly.
- **The off-turn-path guarantee** is tested against a locally-defined
  `_BlockingRouter` whose stream parks on an `asyncio.Event` the test
  owns. `tests/fakes.py`'s `FakeLLM` replays its script eagerly and has
  no hold seam (unlike `FakeTts`), so a shared fake cannot express "the
  model has not answered yet" — which is the only state in which "did
  `kick()` return anyway?" is a meaningful question. The scheduling seam
  is also driven through an injected `task_factory`, so no test sleeps or
  races.
- **Fold content, cost, and tier** go through the REAL `LLMRouter` over
  `tests/fakes.py`'s `FakeLLM` and the REAL `CostTracker` — the house
  pattern for exercising the utility model with no API key (docs/04
  §9.4). `SessionMemory` is an `AsyncMock(spec=...)`, matching
  `tests/memory/test_session_memory.py`; `RedisClient`'s own summary
  accessors are exercised against the in-memory `FakeRedis` in
  `tests/data/test_redis_client.py`, not here.

What these tests do NOT prove, stated so nothing reads as broader than it
is: nothing here shows a *real* Haiku call honors docs/09 §4.2's
verbatim-preservation contract. A scripted fake returns whatever the test
scripted. What is proved is that the prompt carrying that contract is what
gets sent, that the turns and previous summary reach it intact, and that
whatever comes back is stored without this module mangling it. Model
fidelity is the golden-eval set's job (docs/11 §7), which needs a live
model and does not run here.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
from collections.abc import AsyncIterator
from decimal import Decimal
from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent.cost_tracker import CostTracker
from app.agent.llm_router import LLMRouter
from app.config import Settings
from app.data.redis_client import RedisClient
from app.domain.interfaces import LLMProvider, LLMRouterProto
from app.domain.types import (
    LLMEvent,
    Message,
    ModelTier,
    Role,
    RollingSummary,
    TaskKind,
    TokenEvent,
    ToolCall,
    UsageEvent,
)
from app.memory.session_memory import SessionMemory
from app.memory.summarizer import (
    FIRST_FOLD_TURN,
    FOLD_INTERVAL,
    SUMMARY_TOKEN_BUDGET,
    WINDOW_TURNS,
    BufferedTurn,
    Summarizer,
    render_turns,
    should_fold,
    summary_thru_turn,
    window_start_turn,
)
from tests.fakes import FakeLLM

# backend/tests/memory/test_summarizer.py -> backend/
_BACKEND = Path(__file__).resolve().parents[2]
_PROMPT_FILE = _BACKEND / "app" / "agent" / "prompts" / "rolling_summary.md"

# docs/09 §4.2's canonical summary at `summary_thru_turn = 9`, verbatim
# from the doc. Used as the scripted model output so the store/round-trip
# assertions run against the real fixture rather than a placeholder.
CANONICAL_SUMMARY_THRU_9 = (
    "Rajesh's ₹245 vendor payment to Amazon Business was declined at 2:14 PM —\n"
    "daily limit exceeded (₹24,890 of ₹25,000 used). Agent confirmed wallet\n"
    "balance ₹18,450 via get_wallet_balance and explained the limit resets at\n"
    "midnight. Rajesh asked about raising the limit; agent described the\n"
    "Merchant Pro increase process (₹25,000 → ₹50,000, within 4 business hours) and\n"
    "offered to submit the request. No mutating tool executed yet."
)

_USAGE = {"prompt_tokens": 1000, "completion_tokens": 250}


# --------------------------------------------------------------------------
# Local doubles
# --------------------------------------------------------------------------


class _BlockingRouter:
    """`LLMRouterProto` double whose stream parks until the test releases
    it. Exists so a test can observe the window in which a fold has been
    scheduled but has NOT produced anything — the only state that
    distinguishes "kicked off the turn path" from "awaited on it"."""

    def __init__(self, reply: str = "folded summary") -> None:
        self.gate = asyncio.Event()
        self.started = asyncio.Event()
        self.calls: list[list[Message]] = []
        self.tiers: list[ModelTier] = []
        self.fail_with: BaseException | None = None
        self._reply = reply

    def route(self, task: TaskKind) -> ModelTier:
        return ModelTier.UTILITY if task is TaskKind.UTILITY else ModelTier.DIALOGUE

    def stream(
        self,
        messages: list[Message],
        *,
        tier: ModelTier,
        tools: list[dict[str, Any]] | None = None,
        ttft_deadline_s: float = 1.5,
    ) -> AsyncIterator[LLMEvent]:
        self.calls.append(messages)
        self.tiers.append(tier)
        return self._events()

    async def _events(self) -> AsyncIterator[LLMEvent]:
        self.started.set()
        await self.gate.wait()
        if self.fail_with is not None:
            raise self.fail_with
        yield TokenEvent(delta={"content": self._reply})
        yield UsageEvent(model="anthropic/claude-haiku-4-5", usage=dict(_USAGE))


class _ManualTaskFactory:
    """Records the coroutines `kick()` schedules and wraps each in a real
    `asyncio.Task`, so a test can assert *that* scheduling happened
    without giving up the ability to await the result."""

    def __init__(self) -> None:
        self.scheduled: list[asyncio.Task[Any]] = []

    def __call__(self, coro: Any) -> asyncio.Task[Any]:
        task = asyncio.ensure_future(coro)
        self.scheduled.append(task)
        return task


async def _started(router: _BlockingRouter) -> None:
    """Wait for the scheduled fold to reach its await point, under a
    deadline.

    Bounded deliberately: if `kick()` ever regressed to running the fold
    on the turn path, the fold would never be scheduled and a bare
    `Event.wait()` here would hang the whole suite rather than failing
    this test. A hang is a worse failure than a red test — it reports
    nothing and blocks CI. Verified by mutation: making `kick` async
    turns this into a 2 s failure, not a stall.
    """
    await asyncio.wait_for(router.started.wait(), timeout=2.0)


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


@pytest.fixture
def session_memory() -> AsyncMock:
    memory = AsyncMock(spec=SessionMemory)
    memory.add_cost.return_value = Decimal("0")
    return memory


@pytest.fixture
def cost_tracker(settings: Settings) -> CostTracker:
    """The REAL CostTracker. Its `finalize()` is never called here, so the
    Postgres/Redis collaborators it would need at hang-up are `AsyncMock`s
    that no test exercises — `record_turn`, the only method a fold uses,
    is pure and touches neither."""
    return CostTracker(
        settings,
        session_factory=cast(
            "async_sessionmaker[AsyncSession]", AsyncMock(spec=async_sessionmaker)
        ),
        redis=AsyncMock(spec=RedisClient),
    )


@pytest.fixture
def task_factory() -> _ManualTaskFactory:
    return _ManualTaskFactory()


def _turn(turn_no: int, user: str, agent: str) -> BufferedTurn:
    return BufferedTurn(
        turn_no=turn_no,
        messages=(
            Message(role=Role.USER, content=user),
            Message(role=Role.ASSISTANT, content=agent),
        ),
    )


def _observe(summarizer: Summarizer, first: int, last: int) -> None:
    for n in range(first, last + 1):
        summarizer.observe_turn(
            n,
            [
                Message(role=Role.USER, content=f"customer line {n}"),
                Message(role=Role.ASSISTANT, content=f"agent line {n}"),
            ],
        )


def _real_router(fake_llm: FakeLLM, settings: Settings) -> LLMRouter:
    """The real router over the shared FakeLLM. The cast mirrors
    tests/agent/test_llm_router.py's: `LLMProvider.stream`'s
    `async def ... -> AsyncIterator` spelling doesn't structurally match
    an async generator under mypy."""
    return LLMRouter(cast("LLMProvider", fake_llm), settings)


# --------------------------------------------------------------------------
# docs/09 §4.1 — the schedule
# --------------------------------------------------------------------------


@pytest.mark.parametrize("turn_count", [1, 2, 3, 4, 5, 6, 7, 8])
def test_no_fold_fires_while_the_window_still_holds_every_turn(turn_count: int) -> None:
    """docs/09 §4.1 rule 1: while `turn_count <= 8` no summary exists —
    8 short voice turns fit the 600-token window slot verbatim."""
    assert should_fold(turn_count) is False


def test_the_first_fold_fires_at_turn_nine() -> None:
    """docs/09 §4.1 rule 2: `turn_count` first exceeds 8 at turn 9.

    The literal 9, not `FIRST_FOLD_TURN`. Asserting `should_fold(
    FIRST_FOLD_TURN)` reads fine and proves nothing — move the constant
    and the expectation moves with it, so the test survives the one bug
    it exists to catch. Found by mutation: `FIRST_FOLD_TURN = 10` killed
    no test until this was pinned to the doc's number.
    """
    assert should_fold(9) is True


@pytest.mark.parametrize("turn_count", [15, 21, 27, 33])
def test_folds_re_fire_every_six_turns_after_the_first(turn_count: int) -> None:
    """docs/09 §4.1 rule 3: "thereafter it re-fires every 6 turns (N=15,
    21, ...)"."""
    assert should_fold(turn_count) is True


@pytest.mark.parametrize("turn_count", [10, 11, 12, 13, 14, 16, 17, 20, 22])
def test_no_fold_fires_between_fold_points(turn_count: int) -> None:
    assert should_fold(turn_count) is False


@pytest.mark.parametrize(("turn_count", "expected"), [(9, 3), (15, 9), (21, 15), (27, 21)])
def test_the_boundary_keeps_the_last_six_turns_verbatim(
    turn_count: int, expected: int
) -> None:
    """docs/09 §4.1's worked examples: "At N=9 that means summarize turns
    1-3"; at N=15 the summary covers 1..9."""
    assert summary_thru_turn(turn_count) == expected
    assert turn_count - summary_thru_turn(turn_count) == 6


def test_the_schedule_constants_match_the_numbers_docs09_pins() -> None:
    """docs/09 §4.1: fire at turn 9, keep the last 6 verbatim, re-fire
    every 6. Pinned as literals so the tests above, which are written
    against these numbers, cannot silently be redefined by editing them."""
    assert (FIRST_FOLD_TURN, WINDOW_TURNS, FOLD_INTERVAL) == (9, 6, 6)


def test_the_window_starts_at_the_turn_after_the_boundary() -> None:
    """The whole no-overlap/no-gap property in one assertion: for every
    fold point, the summarized range `1..thru` and the verbatim range
    `window_start..N` are adjacent, disjoint, and together cover `1..N`
    with nothing counted twice and nothing missing."""
    for turn_count in range(9, 60, 6):
        thru = summary_thru_turn(turn_count)
        start = window_start_turn(thru)

        summarized = set(range(1, thru + 1))
        verbatim = set(range(start, turn_count + 1))

        assert not (summarized & verbatim), f"turn {turn_count}: overlap"
        assert summarized | verbatim == set(range(1, turn_count + 1)), f"turn {turn_count}: gap"
        assert len(verbatim) == 6


def test_before_any_fold_the_window_starts_at_turn_one() -> None:
    """`thru_turn = 0` is how "no summary yet" is spelled — docs/09 §4.1
    rule 1's state, where the window holds the whole call."""
    assert window_start_turn(0) == 1


def test_consecutive_folds_partition_the_turns_they_consume() -> None:
    """The incremental fold's input is "the turns now leaving the window"
    (docs/09 §4.1 rule 3): at N=15 that is turns 4-9, i.e. exactly the
    turns after the previous boundary. No turn is folded twice, and none
    is skipped."""
    boundaries = [summary_thru_turn(n) for n in (9, 15, 21, 27)]
    assert boundaries == [3, 9, 15, 21]

    consumed: list[int] = []
    previous = 0
    for thru in boundaries:
        consumed.extend(range(previous + 1, thru + 1))
        previous = thru
    assert consumed == list(range(1, 22))


# --------------------------------------------------------------------------
# The off-turn-path scheduling seam (docs/09 §4.1 rule 4)
# --------------------------------------------------------------------------


def test_kick_is_synchronous_so_a_fold_cannot_be_awaited_on_the_turn_path() -> None:
    """The structural half of the off-turn-path guarantee. A sync method
    cannot `await` the fold, so no caller can put a ~1.5 s Haiku call
    inside a turn whose p95 budget is 2.0 s without first changing this
    signature.

    Scope, stated exactly: this proves `kick` cannot itself block. It does
    NOT prove a future caller won't `await summarizer.fold_pending(...)`
    on the turn path instead — `fold_pending` is async by necessity (the
    post-call pipeline awaits it) and nothing here can stop that misuse.
    """
    assert not inspect.iscoroutinefunction(Summarizer.kick)
    assert not inspect.isasyncgenfunction(Summarizer.kick)


async def test_kick_returns_before_the_fold_has_produced_anything(
    session_memory: AsyncMock, cost_tracker: CostTracker, task_factory: _ManualTaskFactory
) -> None:
    """The behavioral half: with the model parked, `kick()` still returns,
    and it returns having written nothing. If the fold ran on the turn
    path this call would not return until the gate opened."""
    router = _BlockingRouter()
    summarizer = Summarizer(
        cast("LLMRouterProto", router),
        cast("SessionMemory", session_memory),
        cost_tracker=cost_tracker,
        task_factory=task_factory,
    )
    _observe(summarizer, 1, 9)

    assert summarizer.kick("sess_1", 9) is True

    # Let the scheduled task start and reach its await point. The caller
    # is demonstrably making progress while the model has not answered.
    await _started(router)
    assert summarizer.summary is None
    session_memory.set_summary.assert_not_awaited()

    router.gate.set()
    await asyncio.wait_for(summarizer.drain(), timeout=5.0)

    assert summarizer.summary is not None
    assert summarizer.summary.thru_turn == 3
    session_memory.set_summary.assert_awaited_once()


async def test_the_turn_after_a_kick_proceeds_while_the_fold_is_still_running(
    session_memory: AsyncMock, cost_tracker: CostTracker, task_factory: _ManualTaskFactory
) -> None:
    """docs/09 §4.1 rule 4: "The next turn uses whichever summary is
    current." Turn 10's bookkeeping completes with the turn-9 fold still
    in flight — the fold is not a barrier."""
    router = _BlockingRouter()
    summarizer = Summarizer(
        cast("LLMRouterProto", router),
        cast("SessionMemory", session_memory),
        cost_tracker=cost_tracker,
        task_factory=task_factory,
    )
    _observe(summarizer, 1, 9)
    summarizer.kick("sess_1", 9)
    await _started(router)

    # Turn 10 happens; it is not a fold turn, and it does not wait.
    summarizer.observe_turn(10, [Message(role=Role.USER, content="and one more thing")])
    assert summarizer.kick("sess_1", 10) is False
    assert summarizer.summary is None  # the fold genuinely has not landed

    router.gate.set()
    await asyncio.wait_for(summarizer.drain(), timeout=5.0)
    assert summarizer.summary is not None


async def test_kick_on_a_non_fold_turn_schedules_nothing(
    session_memory: AsyncMock, cost_tracker: CostTracker, task_factory: _ManualTaskFactory
) -> None:
    router = _BlockingRouter()
    summarizer = Summarizer(
        cast("LLMRouterProto", router),
        cast("SessionMemory", session_memory),
        cost_tracker=cost_tracker,
        task_factory=task_factory,
    )
    _observe(summarizer, 1, 8)

    for turn_count in range(1, 9):
        assert summarizer.kick("sess_1", turn_count) is False

    assert task_factory.scheduled == []
    assert router.calls == []


async def test_a_second_kick_while_a_fold_is_in_flight_does_not_start_a_second_fold(
    session_memory: AsyncMock, cost_tracker: CostTracker, task_factory: _ManualTaskFactory
) -> None:
    """Single-flight. Two concurrent folds could complete out of order and
    leave the OLDER boundary written last, which would make
    `summary_thru_turn` disagree with the summary's actual coverage — the
    exact overlap/gap failure the boundary field exists to prevent."""
    router = _BlockingRouter()
    summarizer = Summarizer(
        cast("LLMRouterProto", router),
        cast("SessionMemory", session_memory),
        cost_tracker=cost_tracker,
        task_factory=task_factory,
    )
    _observe(summarizer, 1, 15)
    assert summarizer.kick("sess_1", 9) is True
    await _started(router)

    assert summarizer.kick("sess_1", 15) is False
    assert len(task_factory.scheduled) == 1
    assert len(router.calls) == 1

    router.gate.set()
    await asyncio.wait_for(summarizer.drain(), timeout=5.0)


async def test_a_repeat_kick_for_an_already_covered_boundary_is_a_no_op(
    session_memory: AsyncMock, cost_tracker: CostTracker, task_factory: _ManualTaskFactory
) -> None:
    """Idempotent re-kick: a caller that fires twice for turn 9 must not
    burn a second $0.0023 fold re-summarizing the same range."""
    router = _BlockingRouter()
    summarizer = Summarizer(
        cast("LLMRouterProto", router),
        cast("SessionMemory", session_memory),
        cost_tracker=cost_tracker,
        task_factory=task_factory,
    )
    _observe(summarizer, 1, 9)
    router.gate.set()

    assert summarizer.kick("sess_1", 9) is True
    await asyncio.wait_for(summarizer.drain(), timeout=5.0)
    assert summarizer.fold_count == 1

    assert summarizer.kick("sess_1", 9) is False
    assert summarizer.fold_count == 1
    assert len(router.calls) == 1


async def test_a_failed_fold_is_recorded_rather_than_raised_at_the_caller(
    session_memory: AsyncMock, cost_tracker: CostTracker, task_factory: _ManualTaskFactory
) -> None:
    """A detached task's exception would otherwise surface only as
    asyncio's "exception was never retrieved" at GC time — the silent
    swallow `cost_tracker.py`'s judgment call #1 warns about."""
    router = _BlockingRouter()
    router.fail_with = RuntimeError("haiku is down")
    router.gate.set()
    summarizer = Summarizer(
        cast("LLMRouterProto", router),
        cast("SessionMemory", session_memory),
        cost_tracker=cost_tracker,
        task_factory=task_factory,
    )
    _observe(summarizer, 1, 9)

    assert summarizer.kick("sess_1", 9) is True
    await asyncio.wait_for(summarizer.drain(), timeout=5.0)

    assert isinstance(summarizer.last_error, RuntimeError)
    assert summarizer.summary is None
    assert summarizer.fold_count == 0
    session_memory.set_summary.assert_not_awaited()


async def test_a_failed_fold_keeps_its_turns_so_the_next_fold_covers_them(
    session_memory: AsyncMock, cost_tracker: CostTracker, task_factory: _ManualTaskFactory
) -> None:
    """docs/09 §4.1's boundary contract survives a failure: the summary
    does not advance past what was written, and the turns the failed fold
    would have covered are folded by the next one instead — a wider
    range, never a hole."""
    router = _BlockingRouter()
    router.fail_with = RuntimeError("haiku is down")
    router.gate.set()
    summarizer = Summarizer(
        cast("LLMRouterProto", router),
        cast("SessionMemory", session_memory),
        cost_tracker=cost_tracker,
        task_factory=task_factory,
    )
    _observe(summarizer, 1, 9)
    summarizer.kick("sess_1", 9)
    await asyncio.wait_for(summarizer.drain(), timeout=5.0)

    assert [t.turn_no for t in summarizer.pending_turns] == list(range(1, 10))

    router.fail_with = None
    _observe(summarizer, 10, 15)
    summarizer.kick("sess_1", 15)
    await asyncio.wait_for(summarizer.drain(), timeout=5.0)

    assert summarizer.summary is not None
    assert summarizer.summary.thru_turn == 9
    # Turns 1-9 were all folded by the recovery fold; 10-15 stay verbatim.
    assert [t.turn_no for t in summarizer.pending_turns] == list(range(10, 16))
    sent = router.calls[-1][0].content or ""
    assert "customer line 1" in sent
    assert "customer line 9" in sent


async def test_drain_is_a_no_op_when_nothing_is_in_flight(
    session_memory: AsyncMock, cost_tracker: CostTracker
) -> None:
    summarizer = Summarizer(
        cast("LLMRouterProto", _BlockingRouter()),
        cast("SessionMemory", session_memory),
        cost_tracker=cost_tracker,
    )
    await asyncio.wait_for(summarizer.drain(), timeout=5.0)  # must not hang or raise


# --------------------------------------------------------------------------
# The fold's input and output (docs/09 §4.1 rule 3, §4.2's contract)
# --------------------------------------------------------------------------


async def test_the_first_fold_covers_turns_one_to_three_and_writes_the_boundary(
    session_memory: AsyncMock,
    cost_tracker: CostTracker,
    fake_llm: FakeLLM,
    settings: Settings,
) -> None:
    """docs/09 §4.1 rule 2, end to end through the real router: at N=9 the
    fold's input is turns 1-3 and its output is stamped `thru_turn=3`."""
    fake_llm.script_turn(
        TokenEvent(delta={"content": "turns 1-3 summarized"}),
        UsageEvent(model=settings.openrouter_utility_model, usage=dict(_USAGE)),
    )
    summarizer = Summarizer(
        _real_router(fake_llm, settings),
        cast("SessionMemory", session_memory),
        cost_tracker=cost_tracker,
    )
    _observe(summarizer, 1, 9)

    result = await summarizer.fold_pending("sess_1", thru_turn=summary_thru_turn(9))

    assert result == RollingSummary(text="turns 1-3 summarized", thru_turn=3)
    session_memory.set_summary.assert_awaited_once_with("sess_1", result)

    sent = fake_llm.calls[0].messages[0]["content"]
    for turn_no in (1, 2, 3):
        assert f"[turn {turn_no}] customer: customer line {turn_no}" in sent
    for turn_no in (4, 9):
        assert f"[turn {turn_no}]" not in sent


async def test_the_incremental_fold_feeds_the_previous_summary_plus_departing_turns(
    session_memory: AsyncMock,
    cost_tracker: CostTracker,
    fake_llm: FakeLLM,
    settings: Settings,
) -> None:
    """docs/09 §4.1 rule 3: "at N=15: summary-of-1-3 + turns 4-9". The
    fold is a fold, not a re-read of the whole call."""
    fake_llm.script(
        [
            TokenEvent(delta={"content": "summary of turns 1-3"}),
            UsageEvent(model=settings.openrouter_utility_model, usage=dict(_USAGE)),
        ],
        [
            TokenEvent(delta={"content": CANONICAL_SUMMARY_THRU_9}),
            UsageEvent(model=settings.openrouter_utility_model, usage=dict(_USAGE)),
        ],
    )
    summarizer = Summarizer(
        _real_router(fake_llm, settings),
        cast("SessionMemory", session_memory),
        cost_tracker=cost_tracker,
    )
    _observe(summarizer, 1, 9)
    await summarizer.fold_pending("sess_1", thru_turn=3)
    _observe(summarizer, 10, 15)
    await summarizer.fold_pending("sess_1", thru_turn=9)

    second = fake_llm.calls[1].messages[0]["content"]
    assert "summary of turns 1-3" in second
    for turn_no in (4, 5, 6, 7, 8, 9):
        assert f"[turn {turn_no}] customer: customer line {turn_no}" in second
    for turn_no in (1, 2, 3, 10, 15):
        assert f"[turn {turn_no}] customer:" not in second


async def test_the_canonical_summary_is_stored_byte_for_byte(
    session_memory: AsyncMock,
    cost_tracker: CostTracker,
    fake_llm: FakeLLM,
    settings: Settings,
) -> None:
    """docs/09 §4.2's canonical fixture round-trips unmodified: the rupee
    amounts (₹245, ₹24,890, ₹25,000, ₹18,450, ₹50,000), the tool name
    `get_wallet_balance`, and the voiced commitment all survive because
    this module stores what the model returned and edits nothing.

    Only leading/trailing whitespace is stripped — asserted explicitly so
    the one transformation this module does apply is visible."""
    fake_llm.script_turn(
        TokenEvent(delta={"content": f"  {CANONICAL_SUMMARY_THRU_9}\n"}),
        UsageEvent(model=settings.openrouter_utility_model, usage=dict(_USAGE)),
    )
    summarizer = Summarizer(
        _real_router(fake_llm, settings),
        cast("SessionMemory", session_memory),
        cost_tracker=cost_tracker,
    )
    _observe(summarizer, 1, 15)

    result = await summarizer.fold_pending("sess_1", thru_turn=9)

    assert result is not None
    assert result.text == CANONICAL_SUMMARY_THRU_9
    for anchor in ("₹245", "₹24,890", "₹25,000", "₹18,450", "₹50,000", "get_wallet_balance"):
        assert anchor in result.text


async def test_the_prompt_sent_is_the_docs11_rolling_summary_prompt(
    session_memory: AsyncMock,
    cost_tracker: CostTracker,
    fake_llm: FakeLLM,
    settings: Settings,
) -> None:
    """The §4.2 preservation contract reaches the model. The clause is
    what the fold's correctness rests on, so a prompt edit that drops it
    must fail a test rather than quietly degrade summaries."""
    fake_llm.script_turn(
        TokenEvent(delta={"content": "ok"}),
        UsageEvent(model=settings.openrouter_utility_model, usage=dict(_USAGE)),
    )
    summarizer = Summarizer(
        _real_router(fake_llm, settings),
        cast("SessionMemory", session_memory),
        cost_tracker=cost_tracker,
    )
    _observe(summarizer, 1, 9)

    await summarizer.fold_pending("sess_1", thru_turn=3)

    sent = fake_llm.calls[0].messages[0]["content"]
    assert "Preserve VERBATIM, never paraphrase" in sent
    assert "every rupee amount, every reference or" in sent
    assert "Do NOT invent facts not present in the input." in sent
    # Both placeholders were interpolated, not shipped literally.
    assert "{summary}" not in sent
    assert "{turns}" not in sent


async def test_the_first_fold_tells_the_model_there_is_no_prior_summary(
    session_memory: AsyncMock,
    cost_tracker: CostTracker,
    fake_llm: FakeLLM,
    settings: Settings,
) -> None:
    """The `{summary}` slot is never left blank: an empty line between two
    `---` fences reads as a truncated prompt rather than as "nothing
    yet"."""
    fake_llm.script_turn(
        TokenEvent(delta={"content": "ok"}),
        UsageEvent(model=settings.openrouter_utility_model, usage=dict(_USAGE)),
    )
    summarizer = Summarizer(
        _real_router(fake_llm, settings),
        cast("SessionMemory", session_memory),
        cost_tracker=cost_tracker,
    )
    _observe(summarizer, 1, 9)

    await summarizer.fold_pending("sess_1", thru_turn=3)

    sent = fake_llm.calls[0].messages[0]["content"]
    assert "--- current summary ---\n(none — this is the first summary of this call)" in sent


async def test_a_fold_with_no_turns_in_range_writes_nothing(
    session_memory: AsyncMock,
    cost_tracker: CostTracker,
    fake_llm: FakeLLM,
    settings: Settings,
) -> None:
    summarizer = Summarizer(
        _real_router(fake_llm, settings),
        cast("SessionMemory", session_memory),
        cost_tracker=cost_tracker,
    )
    _observe(summarizer, 10, 15)

    assert await summarizer.fold_pending("sess_1", thru_turn=3) is None
    session_memory.set_summary.assert_not_awaited()
    assert fake_llm.calls == []


async def test_an_empty_completion_does_not_erase_the_existing_summary(
    session_memory: AsyncMock,
    cost_tracker: CostTracker,
    fake_llm: FakeLLM,
    settings: Settings,
) -> None:
    """A fold that returns nothing must not overwrite good compressed
    history with an empty string — that would silently delete every turn
    the summary carried."""
    fake_llm.script(
        [
            TokenEvent(delta={"content": CANONICAL_SUMMARY_THRU_9}),
            UsageEvent(model=settings.openrouter_utility_model, usage=dict(_USAGE)),
        ],
        [
            TokenEvent(delta={"content": "   "}),
            UsageEvent(model=settings.openrouter_utility_model, usage=dict(_USAGE)),
        ],
    )
    summarizer = Summarizer(
        _real_router(fake_llm, settings),
        cast("SessionMemory", session_memory),
        cost_tracker=cost_tracker,
    )
    _observe(summarizer, 1, 15)
    await summarizer.fold_pending("sess_1", thru_turn=3)
    _observe(summarizer, 16, 21)

    with pytest.raises(ValueError, match="refusing to overwrite"):
        await summarizer.fold_pending("sess_1", thru_turn=9)

    assert summarizer.summary is not None
    assert summarizer.summary.text == CANONICAL_SUMMARY_THRU_9
    assert summarizer.summary.thru_turn == 3


async def test_an_over_budget_summary_is_stored_whole_not_truncated(
    session_memory: AsyncMock,
    cost_tracker: CostTracker,
    fake_llm: FakeLLM,
    settings: Settings,
) -> None:
    """docs/09 §4's 250-token cap is a warning here, not a cut: slicing at
    a character offset is the edit most likely to bisect a rupee amount or
    a reference id, which is exactly what §4.2 forbids. Mechanical
    trimming belongs to `ContextCompressor` at render time (§7 strategy
    2), which this module does not stand in for."""
    oversize = "₹18,450 balance confirmed. " * 200
    fake_llm.script_turn(
        TokenEvent(delta={"content": oversize}),
        UsageEvent(model=settings.openrouter_utility_model, usage=dict(_USAGE)),
    )
    summarizer = Summarizer(
        _real_router(fake_llm, settings),
        cast("SessionMemory", session_memory),
        cost_tracker=cost_tracker,
    )
    _observe(summarizer, 1, 9)

    result = await summarizer.fold_pending("sess_1", thru_turn=3)

    assert result is not None
    assert result.text == oversize.strip()
    assert len(result.text) / 3.5 > SUMMARY_TOKEN_BUDGET


# --------------------------------------------------------------------------
# Tier and cost attribution (docs/09 §4.1: ~$0.0023 per fold)
# --------------------------------------------------------------------------


async def test_the_fold_runs_on_the_utility_tier(
    session_memory: AsyncMock,
    cost_tracker: CostTracker,
    fake_llm: FakeLLM,
    settings: Settings,
) -> None:
    """docs/09 §4.1: the fold is Haiku. Asserted through the real router,
    so this checks the tier actually resolves to the configured utility
    model rather than that `TaskKind.UTILITY` was passed somewhere."""
    fake_llm.script_turn(
        TokenEvent(delta={"content": "ok"}),
        UsageEvent(model=settings.openrouter_utility_model, usage=dict(_USAGE)),
    )
    summarizer = Summarizer(
        _real_router(fake_llm, settings),
        cast("SessionMemory", session_memory),
        cost_tracker=cost_tracker,
    )
    _observe(summarizer, 1, 9)

    await summarizer.fold_pending("sess_1", thru_turn=3)

    assert fake_llm.calls[0].models == [settings.openrouter_utility_model]
    assert settings.openrouter_utility_model not in settings.dialogue_models
    # No tools on a fold — it is a text compression, not an agent turn.
    assert fake_llm.calls[0].tools is None


async def test_a_fold_is_attributed_to_the_calls_cost(
    session_memory: AsyncMock,
    cost_tracker: CostTracker,
    fake_llm: FakeLLM,
    settings: Settings,
) -> None:
    """docs/09 §4.1 prices a fold at ~$0.0023 and says a 15-turn call
    fires two. Both halves of attribution are checked: `CostTracker`'s
    per-call total (which becomes the `call_costs` row) and the Redis
    running counter (which feeds the budget guard).

    The exact figure follows the test `Settings`' utility prices, not
    docs/09's — the doc's $0.0023 is Haiku's real published rate. What is
    asserted is that the fold's usage is priced at the UTILITY rate, which
    is the attribution property."""
    fake_llm.script_turn(
        TokenEvent(delta={"content": "ok"}),
        UsageEvent(model=settings.openrouter_utility_model, usage=dict(_USAGE)),
    )
    summarizer = Summarizer(
        _real_router(fake_llm, settings),
        cast("SessionMemory", session_memory),
        cost_tracker=cost_tracker,
    )
    _observe(summarizer, 1, 9)

    await summarizer.fold_pending("sess_1", thru_turn=3)

    expected = (
        Decimal(1000) * settings.llm_utility_input_usd_per_mtok
        + Decimal(250) * settings.llm_utility_output_usd_per_mtok
    ) / Decimal("1000000")
    assert cost_tracker.call_total() == expected
    session_memory.add_cost.assert_awaited_once_with("sess_1", expected)


async def test_a_fold_that_fails_mid_stream_still_bills_what_the_model_returned(
    session_memory: AsyncMock,
    cost_tracker: CostTracker,
    fake_llm: FakeLLM,
    settings: Settings,
) -> None:
    """The usage frame arrived, so the tokens were billed; the fold then
    produced no usable text. Dropping the cost here would make a call's
    reported spend quietly lower than the invoice."""
    fake_llm.script_turn(
        TokenEvent(delta={"content": ""}),
        UsageEvent(model=settings.openrouter_utility_model, usage=dict(_USAGE)),
    )
    summarizer = Summarizer(
        _real_router(fake_llm, settings),
        cast("SessionMemory", session_memory),
        cost_tracker=cost_tracker,
    )
    _observe(summarizer, 1, 9)

    with pytest.raises(ValueError, match="refusing to overwrite"):
        await summarizer.fold_pending("sess_1", thru_turn=3)

    assert cost_tracker.call_total() > Decimal("0")
    session_memory.add_cost.assert_awaited_once()


# --------------------------------------------------------------------------
# The pending-turn buffer
# --------------------------------------------------------------------------


def test_observe_turn_rejects_a_repeated_turn_number(
    session_memory: AsyncMock, cost_tracker: CostTracker
) -> None:
    """A repeat would put the same turn in the fold's input twice and make
    the coverage range a lie — fail loudly rather than guess."""
    summarizer = Summarizer(
        cast("LLMRouterProto", _BlockingRouter()),
        cast("SessionMemory", session_memory),
        cost_tracker=cost_tracker,
    )
    summarizer.observe_turn(1, [Message(role=Role.USER, content="hi")])

    with pytest.raises(ValueError, match="must strictly increase"):
        summarizer.observe_turn(1, [Message(role=Role.USER, content="hi again")])


def test_observe_turn_rejects_a_regressing_turn_number(
    session_memory: AsyncMock, cost_tracker: CostTracker
) -> None:
    summarizer = Summarizer(
        cast("LLMRouterProto", _BlockingRouter()),
        cast("SessionMemory", session_memory),
        cost_tracker=cost_tracker,
    )
    _observe(summarizer, 1, 5)

    with pytest.raises(ValueError, match="must strictly increase"):
        summarizer.observe_turn(3, [Message(role=Role.USER, content="back in time")])


def test_buffer_overflow_drops_the_oldest_turns_and_counts_them(
    session_memory: AsyncMock, cost_tracker: CostTracker
) -> None:
    """Deliberately drop-oldest, not raise: `observe_turn` runs inside a
    live voice turn and raising there would drop a merchant's call to
    protect a compression nicety. The cost is that a later summary has a
    content hole for the dropped turns, which `dropped_turns` makes
    visible — it is not a silent loss."""
    summarizer = Summarizer(
        cast("LLMRouterProto", _BlockingRouter()),
        cast("SessionMemory", session_memory),
        cost_tracker=cost_tracker,
    )
    _observe(summarizer, 1, 60)

    assert summarizer.dropped_turns == 60 - len(summarizer.pending_turns)
    assert summarizer.dropped_turns > 0
    kept = [t.turn_no for t in summarizer.pending_turns]
    assert kept[-1] == 60
    assert kept == list(range(kept[0], 61))


# --------------------------------------------------------------------------
# Prompt rendering
# --------------------------------------------------------------------------


def test_render_turns_labels_each_message_with_its_turn_and_speaker() -> None:
    rendered = render_turns([_turn(4, "my payment failed", "let me check that")])

    assert rendered == (
        "[turn 4] customer: my payment failed\n[turn 4] agent: let me check that"
    )


def test_render_turns_carries_tool_calls_and_their_outcomes() -> None:
    """docs/09 §4.2 requires "tool calls made and their outcomes" to
    survive the fold. They cannot survive it if they never reach its
    input — this asserts the rendering that makes them available, not
    that a model preserves them."""
    turn = BufferedTurn(
        turn_no=5,
        messages=(
            Message(
                role=Role.ASSISTANT,
                tool_calls=(
                    ToolCall(id="c1", name="get_wallet_balance", arguments={}),
                ),
            ),
            Message(
                role=Role.TOOL,
                name="get_wallet_balance",
                tool_call_id="c1",
                content="balance ₹18,450",
            ),
        ),
    )

    rendered = render_turns([turn])

    assert "[turn 5] agent called get_wallet_balance({})" in rendered
    assert "[turn 5] tool get_wallet_balance: balance ₹18,450" in rendered


def test_render_turns_is_empty_for_no_turns() -> None:
    assert render_turns([]) == ""


# --------------------------------------------------------------------------
# The prompt file (docs/11 §6 owns the text; docs/11 §7 makes it code)
# --------------------------------------------------------------------------

# sha256 of app/agent/prompts/rolling_summary.md. Same guard
# tests/agent/test_context_builder.py puts on persona.md and
# business_rules.md: docs/11 §7 treats a prompt edit as a change that must
# go through the golden-eval gate, so an unreviewed edit has to fail a
# test. Regenerate deliberately when the prompt legitimately changes.
_ROLLING_SUMMARY_SHA256 = "5d7727dc936a622d75383dbee4e90651d45959e572be2720843e9cbbf0b67524"


def _prompt_sha256() -> str:
    """Hash the TEXT-mode read, not the raw bytes.

    `read_bytes()` here was a real portability bug, caught by the mutation
    sweep rewriting the file: this repo checks out CRLF on Windows and LF
    in Linux CI, so a byte-level pin passes on one and fails on the other
    for a file nobody edited. Universal-newline text mode is what
    `ContextBuilder._load_prompt` actually reads with, so this hashes the
    same string the running code sees. Same helper shape as
    tests/agent/test_context_builder.py's `_prompt_file_sha256`.
    """
    return hashlib.sha256(
        _PROMPT_FILE.read_text(encoding="utf-8").encode("utf-8")
    ).hexdigest()


def test_rolling_summary_prompt_bytes_are_pinned() -> None:
    digest = _prompt_sha256()
    assert digest == _ROLLING_SUMMARY_SHA256, (
        "app/agent/prompts/rolling_summary.md changed. docs/11 §7 makes a prompt "
        "edit a gated change (golden-eval set, version bump), not a silent one — "
        f"update this pin deliberately if the edit is intended. Got: {digest}"
    )


def test_rolling_summary_prompt_declares_both_placeholders() -> None:
    """`_render_prompt` interpolates exactly these two. A prompt that lost
    one would silently ship the model an un-substituted literal."""
    text = _PROMPT_FILE.read_text(encoding="utf-8")
    assert text.count("{summary}") == 1
    assert text.count("{turns}") == 1


def test_rolling_summary_prompt_states_the_preservation_contract() -> None:
    """docs/09 §4.2's contract is a correctness requirement, and the
    prompt is the only place it is expressed to the model."""
    text = _PROMPT_FILE.read_text(encoding="utf-8")
    assert "Preserve VERBATIM, never paraphrase" in text
    for required in ("rupee amount", "transaction id", "tool call", "commitment"):
        assert required in text, f"prompt no longer mentions {required!r}"
