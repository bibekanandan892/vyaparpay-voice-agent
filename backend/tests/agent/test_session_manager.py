"""Unit tests for app.agent.session_manager.SessionManager (docs/05 §3.1).

Same no-Docker strategy as tests/data/repositories/test_repositories.py:
the DB side is an `AsyncMock(spec=AsyncSession)` handed to the manager
through a minimal fake `async_sessionmaker` (a callable returning an
async context manager that yields the mock — the only sessionmaker
surface `SessionManager` uses), and the Redis side is
`AsyncMock(spec=RedisClient)` per the pattern in
tests/memory/test_session_memory.py. `ConversationRepo` runs for real on
top of the mocked session, so these tests exercise the manager's actual
composition (repo entity construction included) without a live Postgres.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from structlog.testing import capture_logs

from app.agent.session_manager import (
    SessionManager,
    SummaryPipelineDeps,
    _classify_intents,
    _classify_resolution,
    _opened_issues_from_invocations,
    _tools_used,
)
from app.api.errors import ResourceNotFoundError
from app.auth.signaling import mint_signaling_token
from app.data.redis_client import RedisClient
from app.domain.interfaces import SessionManagerProto
from app.domain.types import (
    ConversationSummary,
    EndReason,
    PendingConfirm,
    Resolution,
    RollingSummary,
    Session,
    SessionState,
)
from app.memory.conversation_summary_store import ConversationSummaryStore
from app.memory.summarizer import Summarizer
from app.memory.user_profile import IssueOpen
from app.models.orm import CallCost, Conversation, ConversationTurn, ToolInvocation
from app.models.orm import UserProfile as UserProfileRow

_SESSION_ID_RE = re.compile(r"^[0-9a-f]{12}$")
_SHA256_HEX_RE = re.compile(r"^[0-9a-f]{64}$")


class _FakeSessionCtx:
    """The async-context-manager half of the fake sessionmaker below."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def __aenter__(self) -> AsyncSession:
        return self._session

    async def __aexit__(self, *exc_info: object) -> bool:
        return False


def fake_sessionmaker(session: AsyncSession) -> async_sessionmaker[AsyncSession]:
    """A callable with `async_sessionmaker`'s one used behavior — call it,
    get an async context manager yielding an `AsyncSession` — always
    yielding the same mock so tests can assert against it."""
    return cast("async_sessionmaker[AsyncSession]", lambda: _FakeSessionCtx(session))


@pytest.fixture
def db_session() -> AsyncMock:
    session = AsyncMock(spec=AsyncSession)
    # `end()` now reads `ToolAuditRepo.list_for_session()` in the same
    # transaction (post-call-summary-profile-wiring task) — every
    # existing test that never configured `.execute()` would otherwise
    # see `list(result.scalars().all())` try to iterate an unconfigured
    # mock and raise. Default to "no tool invocations this call"; tests
    # that care override via `_configure_invocations`.
    #
    # `MagicMock()`, not the auto-created child mock: an `AsyncMock`'s
    # own child attributes (including `.execute.return_value`) default to
    # `AsyncMock` too, and `Result.scalars()`/`.all()` are synchronous
    # real methods — chaining off the auto-created child silently turns
    # `result.scalars()` into an unawaited coroutine instead of a list
    # (verified by probe: `type(session.execute.return_value)` is
    # `AsyncMock` unless overridden like this).
    result = MagicMock()
    result.scalars.return_value.all.return_value = []
    session.execute.return_value = result
    return session


@pytest.fixture
def redis_client() -> AsyncMock:
    client = AsyncMock(spec=RedisClient)
    # Shared defaults for the post-call summary stage's two reads
    # (SessionMemory.get_pending_confirm / .get_summary, both thin
    # pass-throughs to these same methods) — an unconfigured AsyncMock
    # would otherwise return another AsyncMock, not None, and silently
    # fail summary-stage tests for the wrong reason. Tests that care
    # override either explicitly.
    client.get_pending_confirm.return_value = None
    client.get_summary.return_value = None
    return client


@pytest.fixture
def manager(db_session: AsyncMock, redis_client: AsyncMock) -> SessionManager:
    return SessionManager(fake_sessionmaker(db_session), redis_client)


def _conversation(
    *,
    session_id: str = "sess_1",
    state: str = "in_call",
    started_at: datetime | None = None,
) -> Conversation:
    return Conversation(
        session_id=session_id,
        user_id="usr_rajesh01",
        signaling_token_hash="deadbeef",
        state=state,
        started_at=(
            started_at if started_at is not None else datetime(2026, 7, 24, 14, 10, tzinfo=UTC)
        ),
    )


def _added_turns(db_session: AsyncMock) -> list[ConversationTurn]:
    """Every `ConversationTurn` the repo `session.add`-ed, in call order."""
    return [
        c.args[0] for c in db_session.add.call_args_list if isinstance(c.args[0], ConversationTurn)
    ]


def test_session_manager_satisfies_the_frozen_protocol(manager: SessionManager) -> None:
    # The annotated assignment is the actual check — mypy verifies the
    # structural conformance to SessionManagerProto (app/domain/interfaces.py).
    proto: SessionManagerProto = manager
    assert proto is manager


# --------------------------------------------------------------------------
# create (docs/05 §3.1, docs/12 §4.1)
# --------------------------------------------------------------------------


async def test_create_inserts_conversation_row_and_commits(
    manager: SessionManager, db_session: AsyncMock
) -> None:
    session = await manager.create("usr_rajesh01", None, [])

    db_session.add.assert_called_once()
    row = db_session.add.call_args.args[0]
    assert isinstance(row, Conversation)
    assert row.session_id == session.session_id
    assert row.user_id == "usr_rajesh01"
    db_session.flush.assert_awaited_once()
    db_session.commit.assert_awaited_once()
    db_session.rollback.assert_not_awaited()


async def test_create_returns_well_formed_created_session(manager: SessionManager) -> None:
    session = await manager.create("usr_rajesh01", None, [])

    assert isinstance(session, Session)
    assert session.user_id == "usr_rajesh01"
    assert session.state is SessionState.CREATED
    assert session.ended_at is None
    assert session.started_at.tzinfo is not None  # tz-aware, app-side (judgment call #4)


async def test_create_mints_short_hex_session_ids_that_are_unique(
    manager: SessionManager,
) -> None:
    """12 lowercase hex chars (judgment call #1: canon's 'a1f3c9' shape,
    widened from 6 to 12 chars for PK collision safety) — and never
    containing ':', which RedisClient's colon-delimited keys reject."""
    first = await manager.create("usr_rajesh01", None, [])
    second = await manager.create("usr_rajesh01", None, [])

    assert _SESSION_ID_RE.match(first.session_id)
    assert _SESSION_ID_RE.match(second.session_id)
    assert first.session_id != second.session_id


async def test_create_persists_the_caller_supplied_token_digest(
    manager: SessionManager, db_session: AsyncMock
) -> None:
    """Judgment call #2 (Phase 3): `POST /v1/sessions` mints the real
    one-time signaling token and hands this method only its SHA-256
    digest, which must land verbatim in the NOT NULL column — the same
    value the route writes to `signal_token:{id}` in Redis."""
    minted = mint_signaling_token()

    await manager.create("usr_rajesh01", None, [], signaling_token_hash=minted.token_hash)

    assert db_session.add.call_args.args[0].signaling_token_hash == minted.token_hash


async def test_create_without_a_digest_falls_back_to_a_preimage_free_one(
    manager: SessionManager, db_session: AsyncMock
) -> None:
    """The pre-REST callers' path (scripts/demo_cli.py, the E2E harness)
    against `SessionManagerProto`'s frozen three-argument signature: a
    well-formed 64-hex digest that is PER-ROW random with no reachable
    preimage — the plaintext is minted and dropped — so no candidate
    token can ever hash-match it and a naive `sha256(candidate) ==
    row.signaling_token_hash` verifier fails closed by construction."""
    await manager.create("usr_rajesh01", None, [])
    first = db_session.add.call_args.args[0].signaling_token_hash
    assert _SHA256_HEX_RE.match(first)

    await manager.create("usr_rajesh01", None, [])
    second = db_session.add.call_args.args[0].signaling_token_hash
    assert _SHA256_HEX_RE.match(second)
    assert first != second  # per-row, not a shared constant


async def test_create_accepts_and_ignores_screen_context_and_events(
    manager: SessionManager, db_session: AsyncMock
) -> None:
    """Per SessionManagerProto's docstring these are always None/[] in
    Phase 2 — but the contract is accept-and-ignore, not reject."""
    session = await manager.create(
        "usr_rajesh01",
        {"screen": "payments_list"},
        [{"event": "payment_failed"}],
    )

    assert session.state is SessionState.CREATED
    row = db_session.add.call_args.args[0]
    assert isinstance(row, Conversation)  # nothing about the row changed shape


async def test_create_rolls_back_when_the_insert_fails(
    manager: SessionManager, db_session: AsyncMock
) -> None:
    db_session.flush.side_effect = RuntimeError("db down")

    with pytest.raises(RuntimeError):
        await manager.create("usr_rajesh01", None, [])

    db_session.rollback.assert_awaited_once()
    db_session.commit.assert_not_awaited()


# --------------------------------------------------------------------------
# attach — Phase-2 no-op stub (SessionManagerProto docstring)
# --------------------------------------------------------------------------


async def test_attach_returns_session_mirroring_the_row(
    manager: SessionManager, db_session: AsyncMock
) -> None:
    row = _conversation(state="in_call")
    db_session.get.return_value = row

    session = await manager.attach("sess_1")

    assert session.session_id == "sess_1"
    assert session.user_id == "usr_rajesh01"
    assert session.state is SessionState.IN_CALL
    assert session.started_at == row.started_at
    assert session.ended_at is None


async def test_attach_does_not_transition_state(
    manager: SessionManager, db_session: AsyncMock
) -> None:
    """The created -> in_call transition belongs to the real Phase-3
    signaling attach — the stub must not fake it."""
    row = _conversation(state="created")
    db_session.get.return_value = row

    session = await manager.attach("sess_1")

    assert row.state == "created"
    assert session.state is SessionState.CREATED
    db_session.flush.assert_not_awaited()


async def test_attach_unknown_session_raises_resource_not_found(
    manager: SessionManager, db_session: AsyncMock
) -> None:
    db_session.get.return_value = None

    with pytest.raises(ResourceNotFoundError) as exc_info:
        await manager.attach("sess_missing")

    assert exc_info.value.details == {"session_id": "sess_missing"}


# --------------------------------------------------------------------------
# heartbeat — existence check only, no TTL touch (docs/05 §3.1)
# --------------------------------------------------------------------------


async def test_heartbeat_is_an_existence_check_with_no_writes(
    manager: SessionManager, db_session: AsyncMock, redis_client: AsyncMock
) -> None:
    db_session.get.return_value = _conversation()

    await manager.heartbeat("sess_1")  # returns None per the Proto; must not raise

    db_session.get.assert_awaited_once_with(Conversation, "sess_1")
    db_session.flush.assert_not_awaited()
    assert redis_client.method_calls == []  # no TTL touch, no Redis at all


async def test_heartbeat_unknown_session_raises_resource_not_found(
    manager: SessionManager, db_session: AsyncMock
) -> None:
    db_session.get.return_value = None

    with pytest.raises(ResourceNotFoundError):
        await manager.heartbeat("sess_missing")


# --------------------------------------------------------------------------
# end — idempotent close + the Phase-2 post-call drain (docs/09 §8)
# --------------------------------------------------------------------------


def _turn_record(turn_no: int, role: str, **extra: Any) -> dict[str, Any]:
    return {"turn_no": turn_no, "role": role, **extra}


def _invocation(
    tool_name: str,
    *,
    status: str = "ok",
    input: dict[str, Any] | None = None,  # noqa: A002 -- mirrors ToolInvocation's own column name
    output: dict[str, Any] | None = None,
) -> ToolInvocation:
    return ToolInvocation(
        session_id="sess_1",
        tool_name=tool_name,
        input=input if input is not None else {},
        output=output,
        status=status,
        latency_ms=12,
    )


def _configure_invocations(db_session: AsyncMock, invocations: list[ToolInvocation]) -> None:
    """`ToolAuditRepo.list_for_session()`'s `result.scalars().all()`
    chain, pinned to a specific list — overrides the `db_session` fixture's
    own "no invocations" default. Mutates the existing `MagicMock` the
    fixture already installed at `execute.return_value` rather than
    replacing it wholesale, for the same reason that fixture uses a
    plain `MagicMock` in the first place."""
    db_session.execute.return_value.scalars.return_value.all.return_value = invocations


async def test_end_marks_conversation_ended_and_drains_turns_in_order(
    manager: SessionManager, db_session: AsyncMock, redis_client: AsyncMock
) -> None:
    row = _conversation(state="in_call")
    db_session.get.return_value = row
    redis_client.get_turns.return_value = [
        _turn_record(1, "user"),
        _turn_record(2, "agent", tool_calls=["get_wallet_balance"]),
        _turn_record(3, "user"),
    ]

    await manager.end("sess_1", EndReason.HANGUP)

    assert row.state == "ended"
    assert row.ended_at is not None
    turns = _added_turns(db_session)
    assert [(t.turn_no, t.role) for t in turns] == [(1, "user"), (2, "agent"), (3, "user")]
    assert turns[1].tool_calls == ["get_wallet_balance"]
    db_session.commit.assert_awaited_once()


async def test_end_never_persists_transcript_text(
    manager: SessionManager, db_session: AsyncMock, redis_client: AsyncMock
) -> None:
    """docs/12 §4.2's transcript-non-persistence rule: even a record that
    carries a 'text' key drains to a NULL-text row."""
    db_session.get.return_value = _conversation()
    redis_client.get_turns.return_value = [
        _turn_record(1, "user", text="My payment got declined")
    ]

    await manager.end("sess_1", EndReason.HANGUP)

    (turn,) = _added_turns(db_session)
    assert turn.text is None


async def test_end_parses_cost_and_started_at_from_the_drained_record(
    manager: SessionManager, db_session: AsyncMock, redis_client: AsyncMock
) -> None:
    db_session.get.return_value = _conversation()
    redis_client.get_turns.return_value = [
        _turn_record(
            2,
            "agent",
            latency_ms=420,
            input_tokens=2000,
            output_tokens=120,
            cost_usd="0.0123",
            trace_id="trace-abc",
            started_at="2026-07-24T14:12:00+00:00",
        )
    ]

    await manager.end("sess_1", EndReason.HANGUP)

    (turn,) = _added_turns(db_session)
    assert turn.latency_ms == 420
    assert turn.input_tokens == 2000
    assert turn.output_tokens == 120
    assert turn.cost_usd == Decimal("0.0123")
    assert turn.trace_id == "trace-abc"
    assert turn.started_at == datetime(2026, 7, 24, 14, 12, tzinfo=UTC)


async def test_end_is_idempotent_double_end_drains_once(
    manager: SessionManager, db_session: AsyncMock, redis_client: AsyncMock
) -> None:
    """docs/05 §3.1: a double hang-up fires the post-call pipeline once,
    not twice — the second call is a no-op, no second drain, no error."""
    db_session.get.return_value = _conversation(state="in_call")
    redis_client.get_turns.return_value = [_turn_record(1, "user")]

    with capture_logs() as logs:
        await manager.end("sess_1", EndReason.HANGUP)
        await manager.end("sess_1", EndReason.ESCALATED)

    assert redis_client.get_turns.await_count == 1
    assert len(_added_turns(db_session)) == 1
    assert len([e for e in logs if e["event"] == "call_ended"]) == 1


async def test_end_on_an_already_ended_session_is_a_no_op(
    manager: SessionManager, db_session: AsyncMock, redis_client: AsyncMock
) -> None:
    """Same gate as the double-end test, but starting from a session that
    was already 'ended' before this manager ever saw it — the state
    column is the gate, not manager-local memory."""
    db_session.get.return_value = _conversation(state="ended")

    await manager.end("sess_1", EndReason.TIMEOUT)

    redis_client.get_turns.assert_not_awaited()
    db_session.flush.assert_not_awaited()


async def test_end_unknown_session_raises_resource_not_found(
    manager: SessionManager, db_session: AsyncMock, redis_client: AsyncMock
) -> None:
    db_session.get.return_value = None

    with pytest.raises(ResourceNotFoundError):
        await manager.end("sess_missing", EndReason.ERROR)

    redis_client.get_turns.assert_not_awaited()
    db_session.rollback.assert_awaited_once()


async def test_end_leaves_redis_keys_to_expire_on_ttl(
    manager: SessionManager, db_session: AsyncMock, redis_client: AsyncMock
) -> None:
    """docs/09 §8: keys are read once and left to their 24h TTL — the
    drain's only Redis traffic is the one read, never a delete."""
    db_session.get.return_value = _conversation()
    redis_client.get_turns.return_value = []

    await manager.end("sess_1", EndReason.HANGUP)

    assert [c[0] for c in redis_client.method_calls] == ["get_turns"]


async def test_end_with_no_turn_records_still_ends_the_conversation(
    manager: SessionManager, db_session: AsyncMock, redis_client: AsyncMock
) -> None:
    row = _conversation()
    db_session.get.return_value = row
    redis_client.get_turns.return_value = []

    await manager.end("sess_1", EndReason.TIMEOUT)

    assert row.state == "ended"
    assert _added_turns(db_session) == []
    db_session.commit.assert_awaited_once()


async def test_end_logs_call_ended_with_reason_and_turn_count(
    manager: SessionManager, db_session: AsyncMock, redis_client: AsyncMock
) -> None:
    """Judgment call #5: EndReason has no conversations column, so it is
    recorded on docs/05 §3.10's `call_ended` structlog event."""
    db_session.get.return_value = _conversation(
        started_at=datetime(2026, 7, 24, 14, 10, tzinfo=UTC)
    )
    redis_client.get_turns.return_value = [_turn_record(1, "user"), _turn_record(2, "agent")]

    with capture_logs() as logs:
        await manager.end("sess_1", EndReason.ESCALATED)

    (event,) = [e for e in logs if e["event"] == "call_ended"]
    assert event["session_id"] == "sess_1"
    assert event["reason"] == "escalated"
    assert event["turn_count"] == 2
    assert isinstance(event["duration_s"], int)
    assert event["duration_s"] >= 0


async def test_end_rolls_back_the_whole_drain_when_a_record_is_malformed(
    manager: SessionManager, db_session: AsyncMock, redis_client: AsyncMock
) -> None:
    """Judgment call #6: a record missing its required keys aborts (and
    rolls back) the whole transaction — the ended-flip included — so the
    retry re-runs the drain whole from the still-live Redis list
    (docs/09 §8), instead of committing a half-drained call."""
    db_session.get.return_value = _conversation()
    redis_client.get_turns.return_value = [{"role": "user"}]  # no turn_no

    with pytest.raises(KeyError):
        await manager.end("sess_1", EndReason.HANGUP)

    db_session.rollback.assert_awaited_once()
    db_session.commit.assert_not_awaited()


# --------------------------------------------------------------------------
# end — judgment call #9: the finalize-before-end ordering invariant
# --------------------------------------------------------------------------


def _get_by_model(
    *,
    conversation: Conversation | None,
    call_cost: CallCost | None,
    user_profile: UserProfileRow | None = None,
) -> Any:
    """Builds a `db_session.get` side_effect that discriminates by the
    model class each repo passes through (`ConversationRepo.get`,
    `CostRepo.get`, and `UserProfileRepo.get` are all the same inherited
    `SqlAlchemyRepository.get` shape or a thin override of it, so a
    single uniform `return_value` can't tell them apart). `**kwargs`
    absorbs `UserProfileRepo.get`'s own `with_for_update` keyword, which
    the other two callers never pass."""

    async def fake_get(model: type, _id: str, **kwargs: Any) -> Any:
        if model is Conversation:
            return conversation
        if model is CallCost:
            return call_cost
        if model is UserProfileRow:
            return user_profile
        raise AssertionError(f"unexpected model queried: {model!r}")

    return fake_get


async def test_end_succeeds_when_a_call_costs_row_already_exists(
    manager: SessionManager, db_session: AsyncMock, redis_client: AsyncMock
) -> None:
    """Happy path for judgment call #9: `CostTracker.finalize()` already
    ran (a `call_costs` row exists for this session_id), so `end()`
    proceeds with the drain exactly as before this check was added.

    Two commits land on the shared mock session, not one: the post-call
    profile-merge stage (judgment call #10) opens its own transaction
    after the primary one commits — `user_profile=None` (no existing row)
    lets that second transaction complete cleanly instead of raising, so
    `rollback` genuinely stays unawaited rather than merely being caught
    and swallowed."""
    conversation = _conversation(state="in_call")
    db_session.get.side_effect = _get_by_model(
        conversation=conversation, call_cost=CallCost(session_id="sess_1"), user_profile=None
    )
    redis_client.get_turns.return_value = [_turn_record(1, "user")]

    await manager.end("sess_1", EndReason.HANGUP)

    assert conversation.state == "ended"
    assert len(_added_turns(db_session)) == 1
    assert db_session.commit.await_count == 2  # primary transaction + profile-merge
    db_session.rollback.assert_not_awaited()


# --------------------------------------------------------------------------
# Post-call memory pipeline (docs/09 §8) — post-call-summary-profile-wiring
# task. `_classify_resolution`/`_classify_intents`/`_tools_used`/
# `_opened_issues_from_invocations` are pure (SessionManager's own judgment
# call #11/#13), tested directly with literal expected values — never by
# calling the function under test to compute its own expected output.
# --------------------------------------------------------------------------


def test_classify_resolution_escalated_reason_wins_over_every_other_signal() -> None:
    """Judgment call #11's first, most unambiguous check: `end()`'s own
    caller already decided this, so it wins even over a live pending
    confirm or a successful tool call."""
    pending = PendingConfirm(
        tool="request_limit_increase", args={}, proposed_turn=3, invocation_id=""
    )
    resolution = _classify_resolution(
        reason=EndReason.ESCALATED,
        pending_confirm=pending,
        invocations=[_invocation("get_wallet_balance", status="ok")],
    )
    assert resolution is Resolution.ESCALATED


def test_classify_resolution_a_live_pending_confirm_is_abandoned() -> None:
    """The signal the task brief pointed at: a mutating action proposed
    and never affirmed, executed, or superseded before the call ended."""
    pending = PendingConfirm(
        tool="request_limit_increase",
        args={"current_limit": 25_000, "requested_limit": 50_000},
        proposed_turn=5,
        invocation_id="",
    )
    resolution = _classify_resolution(
        reason=EndReason.HANGUP, pending_confirm=pending, invocations=[]
    )
    assert resolution is Resolution.ABANDONED


def test_classify_resolution_a_successful_tool_call_is_resolved() -> None:
    resolution = _classify_resolution(
        reason=EndReason.HANGUP,
        pending_confirm=None,
        invocations=[
            _invocation("get_payment_status", status="ok"),
            _invocation("request_limit_increase", status="error"),
        ],
    )
    assert resolution is Resolution.RESOLVED


def test_classify_resolution_defaults_to_pending_with_no_signal_at_all() -> None:
    resolution = _classify_resolution(
        reason=EndReason.HANGUP, pending_confirm=None, invocations=[]
    )
    assert resolution is Resolution.PENDING


def test_classify_resolution_every_tool_failing_stays_pending_not_abandoned() -> None:
    """A denied/errored tool attempt with no LIVE pending confirm is not
    the same as an abandoned confirm gate — nothing was proposed and left
    hanging, it simply didn't succeed."""
    resolution = _classify_resolution(
        reason=EndReason.HANGUP,
        pending_confirm=None,
        invocations=[_invocation("request_limit_increase", status="denied")],
    )
    assert resolution is Resolution.PENDING


def test_classify_intents_maps_known_tools_and_dedupes_in_first_seen_order() -> None:
    intents = _classify_intents(
        [
            _invocation("get_wallet_balance"),
            _invocation("get_payment_status"),
            _invocation("get_wallet_balance"),  # repeat later in the call
        ]
    )
    assert intents == ("balance_check", "payment_status")


def test_classify_intents_falls_back_to_the_tool_name_when_unmapped() -> None:
    intents = _classify_intents([_invocation("escalate_to_human")])
    assert intents == ("escalate_to_human",)


def test_tools_used_dedupes_in_first_invocation_order() -> None:
    used = _tools_used(
        [
            _invocation("get_wallet_balance"),
            _invocation("request_limit_increase"),
            _invocation("get_wallet_balance"),
        ]
    )
    assert used == ("get_wallet_balance", "request_limit_increase")


def test_opened_issues_from_invocations_builds_one_issue_per_successful_request() -> None:
    issues = _opened_issues_from_invocations(
        [
            _invocation(
                "request_limit_increase",
                status="ok",
                input={"current_limit": 25_000, "requested_limit": 50_000},
                output={
                    "request_id": "LMT-2026-0724-0913",
                    "status": "submitted",
                    "eta_hours": 4,
                },
            )
        ]
    )
    assert len(issues) == 1
    assert issues[0].id == "LMT-2026-0724-0913"
    assert issues[0].status == "submitted"
    assert issues[0].summary == "Daily limit increase requested: ₹25,000 → ₹50,000"


def test_opened_issues_from_invocations_ignores_non_ok_and_unrelated_tools() -> None:
    issues = _opened_issues_from_invocations(
        [
            _invocation("get_wallet_balance", status="ok"),
            _invocation("request_limit_increase", status="denied"),
            _invocation("request_limit_increase", status="error"),
        ]
    )
    assert issues == ()


# --------------------------------------------------------------------------
# Post-call memory pipeline — wired through end() (judgment calls #10-#14)
# --------------------------------------------------------------------------


class _FakeSummarizer:
    """`Summarizer`-shaped double: `end()` only ever calls `fold_pending`
    (judgment call #12) — no `observe_turn`/`kick`, since nothing
    populates a real Summarizer's buffer on the turn path either."""

    def __init__(
        self, result: RollingSummary | None = None, *, error: BaseException | None = None
    ) -> None:
        self._result = result
        self._error = error
        self.calls: list[tuple[str, int]] = []

    async def fold_pending(self, session_id: str, *, thru_turn: int) -> RollingSummary | None:
        self.calls.append((session_id, thru_turn))
        if self._error is not None:
            raise self._error
        return self._result


class _RecordingSummaryStore:
    """`ConversationSummaryStore`-shaped double — `SessionManager` only
    ever calls `save()` on this collaborator."""

    def __init__(self, *, error: BaseException | None = None) -> None:
        self.saved: list[ConversationSummary] = []
        self._error = error

    async def save(self, summary: ConversationSummary) -> None:
        if self._error is not None:
            raise self._error
        self.saved.append(summary)


def _pipeline_deps(
    summarizer: _FakeSummarizer, store: _RecordingSummaryStore
) -> SummaryPipelineDeps:
    return SummaryPipelineDeps(
        summarizer_factory=lambda: cast("Summarizer", summarizer),
        conversation_summary_store=cast("ConversationSummaryStore", store),
    )


def _patch_user_profile_memory(
    monkeypatch: pytest.MonkeyPatch, calls: list[dict[str, Any]]
) -> None:
    """Replaces `UserProfileMemory` where `SessionManager` constructs it,
    so these tests prove the WIRING (what `_merge_profile` calls
    `merge_post_call` with) rather than re-exercising that class's own,
    already-tested internals (tests/memory/test_user_profile.py)."""

    class _RecordingUserProfileMemory:
        def __init__(self, repo: Any) -> None:
            self._repo = repo

        async def merge_post_call(
            self,
            principal: Any,
            *,
            session_id: str,
            extraction: Any = None,
            opened_issues: Any = (),
            resolved_issue_ids: Any = (),
        ) -> None:
            calls.append(
                {
                    "principal": principal,
                    "session_id": session_id,
                    "extraction": extraction,
                    "opened_issues": tuple(opened_issues),
                    "resolved_issue_ids": tuple(resolved_issue_ids),
                }
            )

    monkeypatch.setattr("app.agent.session_manager.UserProfileMemory", _RecordingUserProfileMemory)


async def test_end_saves_a_summary_with_classified_resolution_and_intents(
    db_session: AsyncMock, redis_client: AsyncMock
) -> None:
    """Proves the wiring end to end: fold_pending's result, the
    tool-audit-derived resolution/intents/tools_used, and the drained
    turn_count/cost_usd all reach the saved ConversationSummary
    unmodified — a literal expected value, not a re-derivation."""
    conversation = _conversation(state="in_call")
    db_session.get.side_effect = _get_by_model(
        conversation=conversation,
        call_cost=CallCost(session_id="sess_1", total_usd=Decimal("0.1234")),
        user_profile=None,
    )
    redis_client.get_turns.return_value = [_turn_record(1, "user"), _turn_record(2, "agent")]
    _configure_invocations(db_session, [_invocation("get_wallet_balance", status="ok")])

    rolling = RollingSummary(text="Rajesh's ₹245 payment...", thru_turn=2)
    summarizer = _FakeSummarizer(rolling)
    store = _RecordingSummaryStore()
    manager = SessionManager(
        fake_sessionmaker(db_session),
        redis_client,
        summary_pipeline=_pipeline_deps(summarizer, store),
    )

    await manager.end("sess_1", EndReason.HANGUP)

    assert summarizer.calls == [("sess_1", 2)]  # thru_turn = len(turn_records)
    assert len(store.saved) == 1
    saved = store.saved[0]
    assert saved.session_id == "sess_1"
    assert saved.user_id == "usr_rajesh01"
    assert saved.summary == "Rajesh's ₹245 payment..."
    assert saved.resolution is Resolution.RESOLVED
    assert saved.tools_used == ("get_wallet_balance",)
    assert saved.intents == ("balance_check",)
    assert saved.turn_count == 2
    assert saved.cost_usd == Decimal("0.1234")


async def test_end_skips_summary_save_when_nothing_was_ever_buffered(
    db_session: AsyncMock, redis_client: AsyncMock
) -> None:
    """Judgment call #12: a fresh Summarizer's buffer is empty and
    SessionMemory.get_summary() has nothing either — this IS the only
    reachable state today (nothing wires observe_turn()/kick() into the
    turn loop yet). The stage is skipped and logged, never fabricated,
    and end() still succeeds."""
    db_session.get.side_effect = _get_by_model(
        conversation=_conversation(state="in_call"),
        call_cost=CallCost(session_id="sess_1", total_usd=Decimal("0.01")),
        user_profile=None,
    )
    redis_client.get_turns.return_value = [_turn_record(1, "user")]
    # redis_client.get_summary already defaults to None (fixture).

    summarizer = _FakeSummarizer(None)  # fold_pending: nothing to fold
    store = _RecordingSummaryStore()
    manager = SessionManager(
        fake_sessionmaker(db_session),
        redis_client,
        summary_pipeline=_pipeline_deps(summarizer, store),
    )

    with capture_logs() as logs:
        await manager.end("sess_1", EndReason.HANGUP)

    assert store.saved == []
    assert any(
        e["event"] == "session_manager.post_call_summary_skipped_no_content" for e in logs
    )


async def test_end_falls_back_to_the_existing_rolling_summary_when_nothing_new_folded(
    db_session: AsyncMock, redis_client: AsyncMock
) -> None:
    """The forward-compatibility half of judgment call #12: once a
    future task wires kick()/observe_turn(), whatever it already wrote to
    session:{id}.summary is picked up here with no further change."""
    db_session.get.side_effect = _get_by_model(
        conversation=_conversation(state="in_call"),
        call_cost=CallCost(session_id="sess_1", total_usd=Decimal("0.02")),
        user_profile=None,
    )
    redis_client.get_turns.return_value = [_turn_record(1, "user")]
    existing = RollingSummary(text="already folded earlier in the call", thru_turn=3)
    redis_client.get_summary.return_value = existing

    summarizer = _FakeSummarizer(None)
    store = _RecordingSummaryStore()
    manager = SessionManager(
        fake_sessionmaker(db_session),
        redis_client,
        summary_pipeline=_pipeline_deps(summarizer, store),
    )

    await manager.end("sess_1", EndReason.HANGUP)

    assert len(store.saved) == 1
    assert store.saved[0].summary == "already folded earlier in the call"


# --------------------------------------------------------------------------
# `real_summarizer` (summarizer-turn-loop-wiring task) — the tail-turn-gap
# closure (session_manager.py judgment call #12, updated): end()'s summary
# stage folds through the REAL per-call Summarizer, not a fresh throwaway,
# whenever the caller supplies one.
# --------------------------------------------------------------------------


async def test_end_folds_through_the_real_summarizer_when_one_is_supplied(
    db_session: AsyncMock, redis_client: AsyncMock
) -> None:
    """The core proof of the tail-turn-gap closure: when `real_summarizer`
    is passed to `end()`, `_save_summary` calls `fold_pending` on THAT
    instance — never on the throwaway `pipeline.summarizer_factory()`
    double, which must not even be invoked."""
    db_session.get.side_effect = _get_by_model(
        conversation=_conversation(state="in_call"),
        call_cost=CallCost(session_id="sess_1", total_usd=Decimal("0.05")),
        user_profile=None,
    )
    redis_client.get_turns.return_value = [_turn_record(n, "user") for n in range(1, 8)]

    real_rolling = RollingSummary(text="folded from the real per-call buffer", thru_turn=7)
    real_summarizer = _FakeSummarizer(real_rolling)

    factory_calls: list[str] = []

    def _unreachable_factory() -> Summarizer:
        factory_calls.append("called")
        raise AssertionError("the throwaway factory must not be reached")

    store = _RecordingSummaryStore()
    pipeline = SummaryPipelineDeps(
        summarizer_factory=_unreachable_factory,
        conversation_summary_store=cast("ConversationSummaryStore", store),
    )
    manager = SessionManager(fake_sessionmaker(db_session), redis_client, summary_pipeline=pipeline)

    await manager.end(
        "sess_1", EndReason.HANGUP, real_summarizer=cast("Summarizer", real_summarizer)
    )

    assert factory_calls == []
    assert real_summarizer.calls == [("sess_1", 7)]  # thru_turn = len(turn_records)
    assert len(store.saved) == 1
    assert store.saved[0].summary == "folded from the real per-call buffer"


async def test_end_still_falls_back_when_the_real_summarizer_has_nothing_new_to_fold(
    db_session: AsyncMock, redis_client: AsyncMock
) -> None:
    """A real per-call Summarizer whose buffer is already fully covered
    by an earlier rolling fold (turn_count lands exactly on a fold
    boundary) legitimately returns `None` from `fold_pending` — the
    existing `SessionMemory.get_summary()` fallback must still run, the
    same as the no-real-summarizer case."""
    db_session.get.side_effect = _get_by_model(
        conversation=_conversation(state="in_call"),
        call_cost=CallCost(session_id="sess_1", total_usd=Decimal("0.02")),
        user_profile=None,
    )
    redis_client.get_turns.return_value = [_turn_record(1, "user")]
    existing = RollingSummary(text="last rolling fold's own text", thru_turn=9)
    redis_client.get_summary.return_value = existing

    real_summarizer = _FakeSummarizer(None)  # nothing left to fold

    def _unreachable_factory() -> Summarizer:
        raise AssertionError(
            "real_summarizer is not None, so the throwaway factory must never run"
        )

    store = _RecordingSummaryStore()
    pipeline = SummaryPipelineDeps(
        summarizer_factory=_unreachable_factory,
        conversation_summary_store=cast("ConversationSummaryStore", store),
    )
    manager = SessionManager(fake_sessionmaker(db_session), redis_client, summary_pipeline=pipeline)

    await manager.end(
        "sess_1", EndReason.HANGUP, real_summarizer=cast("Summarizer", real_summarizer)
    )

    assert len(store.saved) == 1
    assert store.saved[0].summary == "last rolling fold's own text"


async def test_end_without_real_summarizer_uses_the_throwaway_factory_as_before(
    db_session: AsyncMock, redis_client: AsyncMock
) -> None:
    """`real_summarizer` defaults to `None` — every pre-existing caller
    (and every test above this section) reproduces judgment call #12's
    original fallback chain exactly, unchanged."""
    conversation = _conversation(state="in_call")
    db_session.get.side_effect = _get_by_model(
        conversation=conversation,
        call_cost=CallCost(session_id="sess_1", total_usd=Decimal("0.1234")),
        user_profile=None,
    )
    redis_client.get_turns.return_value = [_turn_record(1, "user"), _turn_record(2, "agent")]
    _configure_invocations(db_session, [])

    rolling = RollingSummary(text="from the throwaway factory", thru_turn=2)
    summarizer = _FakeSummarizer(rolling)
    store = _RecordingSummaryStore()
    manager = SessionManager(
        fake_sessionmaker(db_session),
        redis_client,
        summary_pipeline=_pipeline_deps(summarizer, store),
    )

    await manager.end("sess_1", EndReason.HANGUP)  # no real_summarizer= at all

    assert summarizer.calls == [("sess_1", 2)]
    assert store.saved[0].summary == "from the throwaway factory"


async def test_end_without_a_summary_pipeline_configured_still_ends_and_logs_the_skip(
    manager: SessionManager, db_session: AsyncMock, redis_client: AsyncMock
) -> None:
    """`app/api/routes/sessions.py`'s SessionManager (judgment call #10)
    never sets `summary_pipeline` — `end()` must still complete."""
    db_session.get.side_effect = _get_by_model(
        conversation=_conversation(state="in_call"),
        call_cost=CallCost(session_id="sess_1", total_usd=Decimal("0.01")),
        user_profile=None,
    )
    redis_client.get_turns.return_value = [_turn_record(1, "user")]

    with capture_logs() as logs:
        await manager.end("sess_1", EndReason.HANGUP)  # must not raise

    assert any(
        e["event"] == "session_manager.post_call_summary_pipeline_not_configured" for e in logs
    )


async def test_end_summary_save_failure_does_not_block_call_end_or_profile_merge(
    monkeypatch: pytest.MonkeyPatch, db_session: AsyncMock, redis_client: AsyncMock
) -> None:
    """The single most important behavioral property this task adds: a
    failing summary/embed stage — here `ConversationSummaryStore.save()`
    itself raising, the same externally-visible outcome an embedding
    -provider failure inside it produces (that class's own SAVEPOINT
    isolation covers the internal case; this covers SessionManager's own
    boundary around the whole stage) — must not stop
    `conversations.state` from having already committed, and must not
    stop the independent profile-merge stage from running."""
    conversation = _conversation(state="in_call")
    db_session.get.side_effect = _get_by_model(
        conversation=conversation,
        call_cost=CallCost(session_id="sess_1", total_usd=Decimal("0.03")),
        user_profile=None,
    )
    redis_client.get_turns.return_value = [_turn_record(1, "user")]

    rolling = RollingSummary(text="text", thru_turn=1)
    summarizer = _FakeSummarizer(rolling)
    store = _RecordingSummaryStore(error=RuntimeError("embedding provider down"))
    profile_calls: list[dict[str, Any]] = []
    _patch_user_profile_memory(monkeypatch, profile_calls)

    manager = SessionManager(
        fake_sessionmaker(db_session),
        redis_client,
        summary_pipeline=_pipeline_deps(summarizer, store),
    )

    with capture_logs() as logs:
        await manager.end("sess_1", EndReason.HANGUP)  # must not raise

    # The primary transaction (state flip + drain) already committed
    # BEFORE the summary stage ever ran — this assertion is the proof.
    assert conversation.state == "ended"
    assert store.saved == []  # save() raised; nothing landed
    assert any(e["event"] == "session_manager.post_call_summary_failed" for e in logs)
    assert len(profile_calls) == 1  # the OTHER stage still ran despite it


async def test_end_merges_profile_with_an_opened_issue_from_a_successful_tool(
    monkeypatch: pytest.MonkeyPatch,
    manager: SessionManager,
    db_session: AsyncMock,
    redis_client: AsyncMock,
) -> None:
    """docs/09 §5.2's tool-confirmed opened_issues, reached through
    end() -> _merge_profile -> UserProfileMemory.merge_post_call, proven
    against the real request_limit_increase output shape. Uses the
    default `manager` fixture (no summary_pipeline) — profile-merge needs
    none, by design (judgment call #10)."""
    conversation = _conversation(state="in_call")
    db_session.get.side_effect = _get_by_model(
        conversation=conversation,
        call_cost=CallCost(session_id="sess_1", total_usd=Decimal("0.04")),
        user_profile=None,
    )
    redis_client.get_turns.return_value = [_turn_record(1, "user")]
    _configure_invocations(
        db_session,
        [
            _invocation(
                "request_limit_increase",
                status="ok",
                input={"current_limit": 25_000, "requested_limit": 50_000},
                output={
                    "request_id": "LMT-2026-0724-0913",
                    "status": "submitted",
                    "eta_hours": 4,
                },
            )
        ],
    )
    calls: list[dict[str, Any]] = []
    _patch_user_profile_memory(monkeypatch, calls)

    await manager.end("sess_1", EndReason.HANGUP)

    assert len(calls) == 1
    call = calls[0]
    assert call["session_id"] == "sess_1"
    assert call["principal"].user_id == "usr_rajesh01"
    assert call["extraction"] is None  # judgment call #13: deferred, not a guess
    assert call["resolved_issue_ids"] == ()
    (issue,) = call["opened_issues"]
    assert isinstance(issue, IssueOpen)
    assert issue.id == "LMT-2026-0724-0913"
    assert issue.status == "submitted"
    assert issue.summary == "Daily limit increase requested: ₹25,000 → ₹50,000"


async def test_end_does_not_open_an_issue_for_a_denied_limit_increase(
    monkeypatch: pytest.MonkeyPatch,
    manager: SessionManager,
    db_session: AsyncMock,
    redis_client: AsyncMock,
) -> None:
    db_session.get.side_effect = _get_by_model(
        conversation=_conversation(state="in_call"),
        call_cost=CallCost(session_id="sess_1", total_usd=Decimal("0.01")),
        user_profile=None,
    )
    redis_client.get_turns.return_value = [_turn_record(1, "user")]
    _configure_invocations(
        db_session, [_invocation("request_limit_increase", status="denied", output=None)]
    )
    calls: list[dict[str, Any]] = []
    _patch_user_profile_memory(monkeypatch, calls)

    await manager.end("sess_1", EndReason.HANGUP)

    assert calls[0]["opened_issues"] == ()


async def test_end_called_twice_runs_the_post_call_memory_pipeline_only_once(
    monkeypatch: pytest.MonkeyPatch, db_session: AsyncMock, redis_client: AsyncMock
) -> None:
    """docs/09 §8's idempotency, extended to the new pipeline: the
    duplicate-end gate (judgment call #7) short-circuits BEFORE the
    post-call memory pipeline ever runs, so a second `end()` call — the
    shape `app/voice/run.py`'s `_end_with_retry` produces retrying after
    the first call already succeeded — never re-attempts the summary/
    embed or profile stages (judgment call #14)."""
    conversation = _conversation(state="in_call")
    db_session.get.side_effect = _get_by_model(
        conversation=conversation,
        call_cost=CallCost(session_id="sess_1", total_usd=Decimal("0.01")),
        user_profile=None,
    )
    redis_client.get_turns.return_value = [_turn_record(1, "user")]

    rolling = RollingSummary(text="text", thru_turn=1)
    summarizer = _FakeSummarizer(rolling)
    store = _RecordingSummaryStore()
    profile_calls: list[dict[str, Any]] = []
    _patch_user_profile_memory(monkeypatch, profile_calls)

    manager = SessionManager(
        fake_sessionmaker(db_session),
        redis_client,
        summary_pipeline=_pipeline_deps(summarizer, store),
    )

    await manager.end("sess_1", EndReason.HANGUP)
    await manager.end("sess_1", EndReason.HANGUP)  # retried, e.g. _end_with_retry-style

    assert len(store.saved) == 1
    assert len(profile_calls) == 1
    assert summarizer.calls == [("sess_1", 1)]


async def test_end_raises_when_finalize_never_ran(
    manager: SessionManager, db_session: AsyncMock, redis_client: AsyncMock
) -> None:
    """The bug this check exists to catch: no `call_costs` row means
    `CostTracker.finalize()` never ran for this session — `end()` must
    fail loudly instead of silently draining an empty/stale turn list
    into Postgres with no other signal anything went wrong."""
    conversation = _conversation(state="in_call")
    db_session.get.side_effect = _get_by_model(conversation=conversation, call_cost=None)

    with pytest.raises(RuntimeError, match="before CostTracker.finalize"):
        await manager.end("sess_1", EndReason.HANGUP)

    # Never reached the drain, and the ended-flip was rolled back.
    redis_client.get_turns.assert_not_awaited()
    assert conversation.state == "in_call"
    db_session.rollback.assert_awaited_once()
    db_session.commit.assert_not_awaited()


async def test_end_ordering_check_is_skipped_for_an_already_ended_session(
    manager: SessionManager, db_session: AsyncMock, redis_client: AsyncMock
) -> None:
    """The duplicate-end no-op gate (judgment call #7) runs first — a
    second `end()` on an already-ended session must still no-op cleanly
    even if, hypothetically, no call_costs row were queried at all."""
    db_session.get.side_effect = _get_by_model(
        conversation=_conversation(state="ended"), call_cost=None
    )

    await manager.end("sess_1", EndReason.TIMEOUT)  # must not raise

    redis_client.get_turns.assert_not_awaited()
    db_session.flush.assert_not_awaited()
