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
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from structlog.testing import capture_logs

from app.agent.session_manager import SessionManager
from app.api.errors import ResourceNotFoundError
from app.data.redis_client import RedisClient
from app.domain.interfaces import SessionManagerProto
from app.domain.types import EndReason, Session, SessionState
from app.models.orm import CallCost, Conversation, ConversationTurn

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
    return AsyncMock(spec=AsyncSession)


@pytest.fixture
def redis_client() -> AsyncMock:
    return AsyncMock(spec=RedisClient)


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


async def test_create_uses_a_preimage_free_per_row_token_hash_placeholder(
    manager: SessionManager, db_session: AsyncMock
) -> None:
    """NOT NULL column, no Phase-2 signaling token to hash (judgment call
    #2, hardened after security review HIGH): a well-formed 64-hex digest
    that is PER-ROW random with no known preimage — never a shared,
    publicly-derivable constant a naive future verifier could be fed."""
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
    *, conversation: Conversation | None, call_cost: CallCost | None
) -> Any:
    """Builds a `db_session.get` side_effect that discriminates by the
    model class each repo passes through (both `ConversationRepo.get`
    and `CostRepo.get` are the same inherited `SqlAlchemyRepository.get`,
    so a single uniform `return_value` can't tell them apart)."""

    async def fake_get(model: type, _id: str) -> Any:
        if model is Conversation:
            return conversation
        if model is CallCost:
            return call_cost
        raise AssertionError(f"unexpected model queried: {model!r}")

    return fake_get


async def test_end_succeeds_when_a_call_costs_row_already_exists(
    manager: SessionManager, db_session: AsyncMock, redis_client: AsyncMock
) -> None:
    """Happy path for judgment call #9: `CostTracker.finalize()` already
    ran (a `call_costs` row exists for this session_id), so `end()`
    proceeds with the drain exactly as before this check was added."""
    conversation = _conversation(state="in_call")
    db_session.get.side_effect = _get_by_model(
        conversation=conversation, call_cost=CallCost(session_id="sess_1")
    )
    redis_client.get_turns.return_value = [_turn_record(1, "user")]

    await manager.end("sess_1", EndReason.HANGUP)

    assert conversation.state == "ended"
    assert len(_added_turns(db_session)) == 1
    db_session.commit.assert_awaited_once()
    db_session.rollback.assert_not_awaited()


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
