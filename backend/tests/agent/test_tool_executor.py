"""Unit tests for `app/agent/tool_executor.py` (docs/10-tool-calling.md
§2 pipeline, §4 confirm gate + idempotency, §5 error surface, §6 the
canonical T5→T7 trace).

Pure in-process, no Docker: Redis is a duck-typed in-memory fake (the
tests/data/test_redis_client.py pattern), the sessionmaker/session pair
is the tests/tools/test_registry.py fake (extended with `add`/`flush` so
`ToolAuditRepo.insert` works against it), and fake tools are registered
through the real `@tool` decorator under an autouse snapshot/restore
fixture so the shared `_REGISTRY`/`_RAW_HANDLERS` dicts are never
polluted across tests.
"""

from __future__ import annotations

import asyncio
import importlib
import json
from collections.abc import Iterator
from typing import Any
from uuid import uuid4

import asyncpg.exceptions
import pytest
from pydantic import BaseModel, ConfigDict
from sqlalchemy.exc import IntegrityError
from structlog.testing import capture_logs

import app.tools  # noqa: F401 -- import side effect: registers the 3 Phase-2 tools
from app.agent.safety_layer import SafetyLayer
from app.agent.tool_executor import ToolExecutor
from app.api.errors import LimitRequestAlreadyPendingError
from app.domain.types import (
    PendingConfirm,
    SessionUser,
    Tier,
    ToolCall,
    ToolInvocationStatus,
)
from app.models import ToolInvocation
from app.tools.registry import registry, tool

# See tests/tools/test_registry.py for why importlib (not an attribute
# import) is required to reach the real module past the package's
# `registry`-instance shadowing.
registry_module = importlib.import_module("app.tools.registry")

_SESSION_ID = "a1f3c9"
_GATE_INSTRUCTION = (
    "State the action and its consequence, then ask for explicit confirmation. "
    "Do not treat this as executed."
)


# --------------------------------------------------------------------------
# Fakes
# --------------------------------------------------------------------------


class _FakeRedisClient:
    """The four `RedisClient` methods ToolExecutor uses, in-memory.
    Idempotency keys are stored under the plain composite (no
    `idempotency:` prefix) — the fake stands in for the typed wrapper,
    not for Redis itself."""

    def __init__(self) -> None:
        self.pending: dict[str, PendingConfirm] = {}
        self.idempotency: dict[str, str] = {}

    async def get_pending_confirm(self, session_id: str) -> PendingConfirm | None:
        return self.pending.get(session_id)

    async def set_pending_confirm(
        self, session_id: str, pending: PendingConfirm | None
    ) -> None:
        if pending is None:
            self.pending.pop(session_id, None)
        else:
            self.pending[session_id] = pending

    async def get_idempotency(
        self, session_id: str, tool_name: str, turn_no: int
    ) -> str | None:
        return self.idempotency.get(f"{session_id}:{tool_name}:{turn_no}")

    async def set_idempotency(
        self, session_id: str, tool_name: str, turn_no: int, value: str
    ) -> None:
        self.idempotency[f"{session_id}:{tool_name}:{turn_no}"] = value


class _FakeSession:
    """Session-shaped async context manager supporting what
    `ToolAuditRepo.insert` (add + flush) and the executor (commit) issue.
    `flush` mimics INSERT..RETURNING by assigning a primary key, and — for
    the security-review HIGH regression coverage below — mimics the real
    `tool_invocations.idempotency_key` UNIQUE constraint by raising
    `IntegrityError` on a second non-null key, checked against the
    `_FakeSessionmaker`-shared set (a real UNIQUE index is enforced across
    the whole table, not scoped to one session/transaction)."""

    def __init__(
        self,
        committed_idempotency_keys: set[str],
        *,
        force_integrity_error: Exception | None = None,
    ) -> None:
        self.added: list[Any] = []
        self.commit_count = 0
        self._committed_idempotency_keys = committed_idempotency_keys
        # Test-only escape hatch (security-review MEDIUM regression
        # coverage): lets a test simulate an IntegrityError that is NOT
        # the idempotency_key UNIQUE violation — e.g. a differently-typed
        # driver exception — to prove _audit's expect_duplicate path
        # only swallows the specific violation it's meant to.
        self._force_integrity_error = force_integrity_error

    async def __aenter__(self) -> _FakeSession:
        return self

    async def __aexit__(self, *exc_info: object) -> bool:
        return False

    def add(self, entity: Any) -> None:
        self.added.append(entity)

    async def flush(self) -> None:
        if self._force_integrity_error is not None:
            raise self._force_integrity_error
        for entity in self.added:
            key = getattr(entity, "idempotency_key", None)
            if key is not None and key in self._committed_idempotency_keys:
                # `.orig` is a real asyncpg.exceptions.UniqueViolationError,
                # not a generic Exception — tool_executor.py's
                # expect_duplicate path checks the driver-level exception
                # TYPE (SQLSTATE 23505), not just IntegrityError generically,
                # so the fake must mirror that shape to exercise it for real.
                raise IntegrityError(
                    "duplicate idempotency_key",
                    None,
                    asyncpg.exceptions.UniqueViolationError(
                        f"duplicate key value violates unique constraint "
                        f'"tool_invocations_idempotency_key_key": {key!r}'
                    ),
                )
            if getattr(entity, "invocation_id", None) is None:
                entity.invocation_id = uuid4()
            if key is not None:
                self._committed_idempotency_keys.add(key)

    async def commit(self) -> None:
        self.commit_count += 1


class _FakeSessionmaker:
    def __init__(self) -> None:
        self.sessions: list[_FakeSession] = []
        # Shared across every session this maker hands out — a real
        # UNIQUE index spans the whole table, not one transaction.
        self._committed_idempotency_keys: set[str] = set()
        self.force_integrity_error: Exception | None = None

    def __call__(self) -> _FakeSession:
        session = _FakeSession(
            self._committed_idempotency_keys, force_integrity_error=self.force_integrity_error
        )
        self.sessions.append(session)
        return session


class _FakeRepo:
    """Stands in for a SqlAlchemyRepository subclass — records the
    session it was constructed with so tests can prove same-transaction
    coupling."""

    def __init__(self, session: Any) -> None:
        self.session = session


class _EmptyIn(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _LooseIn(BaseModel):
    """No extra='forbid' — extra keys pass validation, so the authorize
    stage (not the schema) is what catches principal smuggling here."""


class _ValueOut(BaseModel):
    value: int


class _LimitIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    current_limit: int
    requested_limit: int


class _LimitOut(BaseModel):
    request_id: str
    status: str
    eta_hours: int


def _register_read_tool(
    name: str,
    *,
    value: int = 7,
    raises: Exception | None = None,
    sleep_s: float = 0.0,
    order_log: list[str] | None = None,
) -> None:
    @tool(name=name, tier=Tier.READ, latency_class="read-single", repo_type=_FakeRepo)  # type: ignore[type-var]
    async def _handler(principal: SessionUser, args: _EmptyIn, repo: _FakeRepo) -> _ValueOut:
        if order_log is not None:
            order_log.append(f"start:{name}")
        if sleep_s:
            await asyncio.sleep(sleep_s)
        if raises is not None:
            raise raises
        if order_log is not None:
            order_log.append(f"end:{name}")
        return _ValueOut(value=value)


def _register_confirm_tool(
    name: str,
    *,
    request_id: str = "LMT-2026-0724-0913",
    raises: Exception | None = None,
    call_log: list[dict[str, Any]] | None = None,
    handler_sessions: list[Any] | None = None,
) -> None:
    @tool(name=name, tier=Tier.CONFIRM_REQUIRED, latency_class="write", repo_type=_FakeRepo)  # type: ignore[type-var]
    async def _handler(principal: SessionUser, args: _LimitIn, repo: _FakeRepo) -> _LimitOut:
        if call_log is not None:
            call_log.append(args.model_dump())
        if handler_sessions is not None:
            handler_sessions.append(repo.session)
        if raises is not None:
            raise raises
        return _LimitOut(request_id=request_id, status="submitted", eta_hours=4)


def _call(name: str, arguments: dict[str, Any], call_id: str = "c1") -> ToolCall:
    return ToolCall(id=call_id, name=name, arguments=arguments)


def _audit_rows(sessionmaker: _FakeSessionmaker) -> list[ToolInvocation]:
    """Committed `tool_invocations` entities, in insertion order —
    uncommitted (rolled-back) sessions don't count, mirroring what the
    database would actually hold."""
    return [
        entity
        for session in sessionmaker.sessions
        if session.commit_count > 0
        for entity in session.added
        if isinstance(entity, ToolInvocation)
    ]


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _registry_isolation() -> Iterator[None]:
    """Snapshot/restore BOTH registry dicts plus the module-level
    sessionmaker global (ToolExecutor's constructor calls `configure`),
    so fake-tool registrations and executor construction never leak into
    other test modules (the tests/tools/test_registry.py convention,
    extended to `_RAW_HANDLERS`)."""
    registry_snapshot = dict(registry_module._REGISTRY)
    raw_snapshot = dict(registry_module._RAW_HANDLERS)
    sessionmaker_snapshot = registry_module._sessionmaker  # type: ignore[attr-defined]
    yield
    registry_module._REGISTRY.clear()
    registry_module._REGISTRY.update(registry_snapshot)
    registry_module._RAW_HANDLERS.clear()
    registry_module._RAW_HANDLERS.update(raw_snapshot)
    registry_module._sessionmaker = sessionmaker_snapshot  # type: ignore[attr-defined]


@pytest.fixture
def fake_redis() -> _FakeRedisClient:
    return _FakeRedisClient()


@pytest.fixture
def sessionmaker() -> _FakeSessionmaker:
    return _FakeSessionmaker()


@pytest.fixture
def principal() -> SessionUser:
    return SessionUser(user_id="usr_rajesh01")


@pytest.fixture
def executor(fake_redis: _FakeRedisClient, sessionmaker: _FakeSessionmaker) -> ToolExecutor:
    return ToolExecutor(
        registry=registry,
        safety=SafetyLayer(registry),
        redis=fake_redis,  # type: ignore[arg-type]
        sessionmaker=sessionmaker,  # type: ignore[arg-type]
    )


# --------------------------------------------------------------------------
# Pipeline stages: lookup, validation, authorization
# --------------------------------------------------------------------------


async def test_empty_batch_returns_empty_list(
    executor: ToolExecutor, principal: SessionUser
) -> None:
    assert (
        await executor.execute([], principal=principal, session_id=_SESSION_ID, turn_no=1) == []
    )


async def test_unknown_tool_returns_denied_result_and_denied_audit_row(
    executor: ToolExecutor, sessionmaker: _FakeSessionmaker, principal: SessionUser
) -> None:
    # Act
    results = await executor.execute(
        [_call("delete_everything", {"target": "all"})],
        principal=principal,
        session_id=_SESSION_ID,
        turn_no=2,
    )

    # Assert — typed refusal (docs/10 §2's denied stage), never a raise.
    result = results[0]
    assert result.ok is False
    assert result.status == ToolInvocationStatus.DENIED
    assert result.error is not None
    assert result.error["code"] == "TOOL_NOT_ALLOWED"
    rows = _audit_rows(sessionmaker)
    assert len(rows) == 1
    assert rows[0].status == "denied"
    assert rows[0].tool_name == "delete_everything"
    assert rows[0].input == {"target": "all"}
    assert rows[0].error_code is None  # DB CHECK pairs error_code with status='error' only


async def test_validation_failure_returns_validation_shape_with_no_audit_row(
    executor: ToolExecutor, sessionmaker: _FakeSessionmaker, principal: SessionUser
) -> None:
    """docs/10 §5 + §6 T5.1/T5.2: schema reject -> structured error the
    LLM retries in-turn; nothing executed, nothing audited."""
    results = await executor.execute(
        [
            _call(
                "request_limit_increase",
                {"current_limit": 25000, "requested_limit": "fifty thousand"},
            )
        ],
        principal=principal,
        session_id=_SESSION_ID,
        turn_no=5,
    )

    result = results[0]
    assert result.ok is False
    assert result.error is not None
    assert result.error["type"] == "validation"
    assert result.error["code"] == "SCHEMA_VALIDATION_FAILED"
    assert result.error["retryable"] is True
    locs = [field["loc"] for field in result.error["fields"]]
    assert "requested_limit" in locs
    hints = {field.get("hint") for field in result.error["fields"]}
    assert hints == {"amounts are integer rupees, e.g. 50000"}  # §6 T5.2's model-facing hint
    assert _audit_rows(sessionmaker) == []


async def test_principal_key_smuggling_is_denied_out_of_scope(
    executor: ToolExecutor, sessionmaker: _FakeSessionmaker, principal: SessionUser
) -> None:
    """Invariant 3 backstop at the executor: a permissive input model
    lets the smuggled key past validation, and the authorize stage
    catches it (docs/10 §2's authorization stage)."""

    @tool(name="loose_read", tier=Tier.READ, latency_class="read-single", repo_type=_FakeRepo)  # type: ignore[type-var]
    async def _loose(principal: SessionUser, args: _LooseIn, repo: _FakeRepo) -> _ValueOut:
        return _ValueOut(value=0)

    results = await executor.execute(
        [_call("loose_read", {"user_id": "usr_victim"})],
        principal=principal,
        session_id=_SESSION_ID,
        turn_no=3,
    )

    result = results[0]
    assert result.ok is False
    assert result.status == ToolInvocationStatus.DENIED
    assert result.error is not None
    assert result.error["code"] == "OUT_OF_SCOPE"
    rows = _audit_rows(sessionmaker)
    assert [row.status for row in rows] == ["denied"]


# --------------------------------------------------------------------------
# Read tier: execution, parallelism, serialization
# --------------------------------------------------------------------------


async def test_read_tool_success_returns_data_and_audits_ok(
    executor: ToolExecutor, sessionmaker: _FakeSessionmaker, principal: SessionUser
) -> None:
    _register_read_tool("fake_read", value=7)

    results = await executor.execute(
        [_call("fake_read", {})], principal=principal, session_id=_SESSION_ID, turn_no=3
    )

    result = results[0]
    assert result.ok is True
    assert result.data == {"value": 7}
    assert result.status == ToolInvocationStatus.OK
    assert result.idempotency_key is None  # reads carry no idempotency semantics
    rows = _audit_rows(sessionmaker)
    assert len(rows) == 1
    assert rows[0].status == "ok"
    assert rows[0].output == {"value": 7}
    assert rows[0].turn_no == 3
    assert rows[0].idempotency_key is None


async def test_read_tools_in_one_batch_execute_in_parallel(
    executor: ToolExecutor, principal: SessionUser
) -> None:
    """docs/10 §2: sibling reads run under asyncio.gather. Each handler
    waits for the *other* to have started — only concurrent execution
    lets both complete inside their wait windows."""
    started_a, started_b = asyncio.Event(), asyncio.Event()

    @tool(name="read_par_a", tier=Tier.READ, latency_class="read-single", repo_type=_FakeRepo)  # type: ignore[type-var]
    async def _a(principal: SessionUser, args: _EmptyIn, repo: _FakeRepo) -> _ValueOut:
        started_a.set()
        await asyncio.wait_for(started_b.wait(), timeout=1.0)
        return _ValueOut(value=1)

    @tool(name="read_par_b", tier=Tier.READ, latency_class="read-single", repo_type=_FakeRepo)  # type: ignore[type-var]
    async def _b(principal: SessionUser, args: _EmptyIn, repo: _FakeRepo) -> _ValueOut:
        started_b.set()
        await asyncio.wait_for(started_a.wait(), timeout=1.0)
        return _ValueOut(value=2)

    results = await executor.execute(
        [_call("read_par_a", {}, "c1"), _call("read_par_b", {}, "c2")],
        principal=principal,
        session_id=_SESSION_ID,
        turn_no=3,
    )

    assert [result.ok for result in results] == [True, True]
    assert results[0].data == {"value": 1}
    assert results[1].data == {"value": 2}


async def test_confirm_call_in_batch_serializes_the_whole_batch(
    executor: ToolExecutor, principal: SessionUser
) -> None:
    """docs/10 §2: "a mutating call in a batch serializes the whole
    batch" — reads run one after another (strictly nested start/end),
    and the confirm call gates."""
    order_log: list[str] = []
    _register_read_tool("read_ser_a", order_log=order_log, sleep_s=0.01)
    _register_read_tool("read_ser_b", order_log=order_log, sleep_s=0.01)
    _register_confirm_tool("confirm_ser")

    results = await executor.execute(
        [
            _call("read_ser_a", {}, "c1"),
            _call("read_ser_b", {}, "c2"),
            _call("confirm_ser", {"current_limit": 1, "requested_limit": 2}, "c3"),
        ],
        principal=principal,
        session_id=_SESSION_ID,
        turn_no=4,
    )

    assert order_log == ["start:read_ser_a", "end:read_ser_a", "start:read_ser_b", "end:read_ser_b"]
    assert results[2].status == ToolInvocationStatus.PENDING_CONFIRM


# --------------------------------------------------------------------------
# Confirm gate (docs/10 §4): hold, supersession, affirmation matching
# --------------------------------------------------------------------------


async def test_confirm_without_affirmation_holds_gate_pending_and_audits(
    executor: ToolExecutor,
    fake_redis: _FakeRedisClient,
    sessionmaker: _FakeSessionmaker,
    principal: SessionUser,
) -> None:
    call_log: list[dict[str, Any]] = []
    _register_confirm_tool("fake_limit", call_log=call_log)

    results = await executor.execute(
        [_call("fake_limit", {"current_limit": 25000, "requested_limit": 50000})],
        principal=principal,
        session_id=_SESSION_ID,
        turn_no=5,
    )

    result = results[0]
    assert result.ok is False
    assert result.status == ToolInvocationStatus.PENDING_CONFIRM
    assert result.gate is not None
    assert result.gate["status"] == "pending_confirm"
    assert result.gate["instruction"] == _GATE_INSTRUCTION
    assert result.gate["action"]["tool"] == "fake_limit"
    assert call_log == []  # nothing executed
    pending = fake_redis.pending[_SESSION_ID]
    assert pending.tool == "fake_limit"
    assert pending.args == {"current_limit": 25000, "requested_limit": 50000}
    assert pending.proposed_turn == 5
    rows = _audit_rows(sessionmaker)
    assert [row.status for row in rows] == ["pending_confirm"]
    assert rows[0].idempotency_key == f"{_SESSION_ID}:fake_limit:5"


async def test_new_proposal_supersedes_and_cancels_prior_pending(
    executor: ToolExecutor,
    fake_redis: _FakeRedisClient,
    sessionmaker: _FakeSessionmaker,
    principal: SessionUser,
) -> None:
    """docs/10 §4: exactly one pending at a time — the old proposal is
    audited `cancelled`, the new one takes its place."""
    _register_confirm_tool("fake_limit")
    fake_redis.pending[_SESSION_ID] = PendingConfirm(
        tool="fake_limit",
        args={"current_limit": 25000, "requested_limit": 50000},
        proposed_turn=5,
        invocation_id="inv_old",
    )

    await executor.execute(
        [_call("fake_limit", {"current_limit": 25000, "requested_limit": 100000})],
        principal=principal,
        session_id=_SESSION_ID,
        turn_no=6,
    )

    rows = _audit_rows(sessionmaker)
    assert [row.status for row in rows] == ["cancelled", "pending_confirm"]
    cancelled = rows[0]
    assert cancelled.tool_name == "fake_limit"
    assert cancelled.input == {"current_limit": 25000, "requested_limit": 50000}
    assert cancelled.idempotency_key is None  # the original pending row owns the turn-5 key
    pending = fake_redis.pending[_SESSION_ID]
    assert pending.args == {"current_limit": 25000, "requested_limit": 100000}
    assert pending.proposed_turn == 6


async def test_matching_reproposal_keeps_original_pending_anchor(
    executor: ToolExecutor,
    fake_redis: _FakeRedisClient,
    sessionmaker: _FakeSessionmaker,
    principal: SessionUser,
) -> None:
    """The §6 reconnect variant: a re-proposal of the SAME action is
    restated (new pending_confirm audit under the new turn's key), not
    reset — the Redis pending keeps the original proposed_turn."""
    _register_confirm_tool("fake_limit")
    args = {"current_limit": 25000, "requested_limit": 50000}
    await executor.execute(
        [_call("fake_limit", args)], principal=principal, session_id=_SESSION_ID, turn_no=5
    )

    await executor.execute(
        [_call("fake_limit", args)], principal=principal, session_id=_SESSION_ID, turn_no=6
    )

    assert fake_redis.pending[_SESSION_ID].proposed_turn == 5
    rows = _audit_rows(sessionmaker)
    assert [row.status for row in rows] == ["pending_confirm", "pending_confirm"]
    assert rows[1].idempotency_key == f"{_SESSION_ID}:fake_limit:6"


async def test_hold_gate_audit_only_swallows_the_unique_violation_not_other_integrity_errors(
    executor: ToolExecutor,
    fake_redis: _FakeRedisClient,
    sessionmaker: _FakeSessionmaker,
    principal: SessionUser,
) -> None:
    """Security review MEDIUM (found independently by two review passes on
    the fix below): `_audit`'s `expect_duplicate=True` path must only
    swallow the SPECIFIC `idempotency_key` UNIQUE violation
    (`asyncpg.exceptions.UniqueViolationError`), not any `IntegrityError`
    — a differently-typed driver exception (e.g. simulating a future
    constraint or a FK violation) must still take the loud
    `log.exception` path, never the silent info-level
    `tool_audit_duplicate_hold_skipped` one."""
    _register_confirm_tool("fake_limit")
    sessionmaker.force_integrity_error = IntegrityError(
        "simulated non-unique constraint violation",
        None,
        asyncpg.exceptions.ForeignKeyViolationError("simulated FK violation"),
    )

    with capture_logs() as logs:
        results = await executor.execute(
            [_call("fake_limit", {"current_limit": 25000, "requested_limit": 50000})],
            principal=principal,
            session_id=_SESSION_ID,
            turn_no=10,
        )

    # The pipeline still degrades gracefully (never raises to the
    # caller — `_audit` is best-effort by design) but the LOG must be
    # loud, not the silent "expected duplicate" path.
    assert results[0].status == ToolInvocationStatus.PENDING_CONFIRM
    duplicate_skipped = [e for e in logs if e["event"] == "tool_audit_duplicate_hold_skipped"]
    failed = [e for e in logs if e["event"] == "tool_audit_write_failed"]
    assert duplicate_skipped == []
    assert len(failed) == 1
    assert failed[0]["log_level"] == "error"


async def test_same_turn_reproposal_never_executes_and_writes_no_second_audit_row(
    executor: ToolExecutor,
    fake_redis: _FakeRedisClient,
    sessionmaker: _FakeSessionmaker,
    principal: SessionUser,
) -> None:
    """Security review HIGH, fixed (two facets of one root cause):

    1. A pending action proposed in THIS SAME turn must never execute on
       THIS turn even with affirmed=True — the user could not possibly
       have said "yes" to something the model proposed after they
       finished speaking. `matches` alone can't tell a same-turn
       re-emission apart from a genuine cross-turn "yes"; the
       `proposed_turn != turn_no` check in `_confirm_tier` is what does.
    2. Re-holding that same-turn re-emission must not surface a second
       PENDING_CONFIRM audit attempt under the SAME idempotency_key the
       first hold already wrote (idem_key is turn-scoped, not
       round-scoped) as a failure — the audit table's
       UNIQUE(idempotency_key) constraint rejects it, and `_audit`'s
       `expect_duplicate=True` path absorbs that as expected.

    Simulates ConversationManager's tool loop calling `execute()` twice
    within one turn (same turn_no): round 1 proposes, round 2 re-emits
    the identical call with the turn-opening affirmed=True now passed
    through unconditionally."""
    call_log: list[dict[str, Any]] = []
    _register_confirm_tool("fake_limit", call_log=call_log)
    args = {"current_limit": 25000, "requested_limit": 50000}

    first = await executor.execute(
        [_call("fake_limit", args)],
        principal=principal,
        session_id=_SESSION_ID,
        turn_no=8,
        affirmed=False,
    )
    second = await executor.execute(
        [_call("fake_limit", args)],
        principal=principal,
        session_id=_SESSION_ID,
        turn_no=8,
        affirmed=True,
    )

    assert first[0].status == ToolInvocationStatus.PENDING_CONFIRM
    assert second[0].status == ToolInvocationStatus.PENDING_CONFIRM
    assert call_log == []  # never executed
    assert fake_redis.pending[_SESSION_ID].proposed_turn == 8
    rows = _audit_rows(sessionmaker)
    assert [row.status for row in rows] == ["pending_confirm"]  # no second row
    assert rows[0].idempotency_key == f"{_SESSION_ID}:fake_limit:8"


async def test_cross_turn_pending_reheld_across_rounds_never_double_audits(
    executor: ToolExecutor,
    fake_redis: _FakeRedisClient,
    sessionmaker: _FakeSessionmaker,
    principal: SessionUser,
) -> None:
    """Security review HIGH (found in a second security pass on the fix
    above): a pending action from an EARLIER turn that gets matched but
    not executed (not affirmed) across MULTIPLE rounds of the CURRENT
    turn reuses the SAME idem_key on every round — `matches` is True on
    both rounds and `_hold_gate`'s `not matches` branch (which is the
    only place that ever rewrites `proposed_turn`) never runs, so a
    predictor keyed off `proposed_turn == turn_no` can't see this case at
    all. `_audit`'s IntegrityError handling is what actually makes this
    safe regardless of shape — this test proves the mechanism, not a
    specific predictor."""
    call_log: list[dict[str, Any]] = []
    _register_confirm_tool("fake_limit", call_log=call_log)
    args = {"current_limit": 25000, "requested_limit": 50000}
    fake_redis.pending[_SESSION_ID] = PendingConfirm(
        tool="fake_limit", args=args, proposed_turn=5, invocation_id="inv_old"
    )

    # Turn 6, "round 1": matched but not affirmed — held, audited under
    # the turn-6 key (the legitimate cross-turn restate).
    round_one = await executor.execute(
        [_call("fake_limit", args)],
        principal=principal,
        session_id=_SESSION_ID,
        turn_no=6,
        affirmed=False,
    )
    # Turn 6, "round 2": the SAME call re-emitted again within the same
    # turn — same idem_key as round 1, since idem_key depends only on
    # (session_id, tool_name, turn_no).
    round_two = await executor.execute(
        [_call("fake_limit", args)],
        principal=principal,
        session_id=_SESSION_ID,
        turn_no=6,
        affirmed=False,
    )

    assert round_one[0].status == ToolInvocationStatus.PENDING_CONFIRM
    assert round_two[0].status == ToolInvocationStatus.PENDING_CONFIRM
    assert call_log == []  # never executed
    assert fake_redis.pending[_SESSION_ID].proposed_turn == 5  # anchor untouched
    rows = _audit_rows(sessionmaker)
    assert [row.status for row in rows] == ["pending_confirm"]  # one row, not two
    assert rows[0].idempotency_key == f"{_SESSION_ID}:fake_limit:6"


async def test_same_turn_different_args_reproposal_never_double_audits(
    executor: ToolExecutor,
    fake_redis: _FakeRedisClient,
    sessionmaker: _FakeSessionmaker,
    principal: SessionUser,
) -> None:
    """A third shape of the same idem_key collision, found while fixing
    the two above: idem_key is `session:tool:turn`, scoped by TOOL, not
    by ARGS. Round 1 of a turn proposing tool A with args X, then round 2
    of the SAME turn proposing the SAME tool A with DIFFERENT args Y, are
    both `not matches` (args differ) — the branch that supersedes and
    re-audits PENDING_CONFIRM — yet land on the identical idem_key."""
    call_log: list[dict[str, Any]] = []
    _register_confirm_tool("fake_limit", call_log=call_log)

    round_one = await executor.execute(
        [_call("fake_limit", {"current_limit": 25000, "requested_limit": 50000})],
        principal=principal,
        session_id=_SESSION_ID,
        turn_no=9,
    )
    round_two = await executor.execute(
        [_call("fake_limit", {"current_limit": 25000, "requested_limit": 75000})],
        principal=principal,
        session_id=_SESSION_ID,
        turn_no=9,
    )

    assert round_one[0].status == ToolInvocationStatus.PENDING_CONFIRM
    assert round_two[0].status == ToolInvocationStatus.PENDING_CONFIRM
    assert call_log == []  # never executed
    rows = _audit_rows(sessionmaker)
    # Round 1 holds cleanly (nothing was pending before it): one
    # pending_confirm row under turn 9's key. Round 2 supersedes round
    # 1's now-pending proposal (different args -> not matches), auditing
    # round 1's proposal as cancelled — but round 2's OWN pending_confirm
    # attempt reuses that same turn-9 key, so the UNIQUE constraint
    # absorbs it: no second pending_confirm row, only the cancelled one
    # from the supersession.
    assert [row.status for row in rows] == ["pending_confirm", "cancelled"]
    assert rows[0].idempotency_key == f"{_SESSION_ID}:fake_limit:9"
    assert rows[1].idempotency_key is None  # the cancelled row never carries one
    pending = fake_redis.pending[_SESSION_ID]
    assert pending.args == {"current_limit": 25000, "requested_limit": 75000}


async def test_affirmed_with_mismatched_args_supersedes_instead_of_executing(
    executor: ToolExecutor,
    fake_redis: _FakeRedisClient,
    sessionmaker: _FakeSessionmaker,
    principal: SessionUser,
) -> None:
    """Strict-match rule (module docstring): even with affirmed=True, a
    call whose args differ from the pending proposal must not execute
    the old decision — "yes but make it a lakh" becomes a new
    proposal."""
    call_log: list[dict[str, Any]] = []
    _register_confirm_tool("fake_limit", call_log=call_log)
    fake_redis.pending[_SESSION_ID] = PendingConfirm(
        tool="fake_limit",
        args={"current_limit": 25000, "requested_limit": 50000},
        proposed_turn=5,
        invocation_id="inv_old",
    )

    results = await executor.execute(
        [_call("fake_limit", {"current_limit": 25000, "requested_limit": 100000})],
        principal=principal,
        session_id=_SESSION_ID,
        turn_no=7,
        affirmed=True,
    )

    assert results[0].status == ToolInvocationStatus.PENDING_CONFIRM
    assert call_log == []
    rows = _audit_rows(sessionmaker)
    assert [row.status for row in rows] == ["cancelled", "pending_confirm"]


async def test_affirmed_matching_pending_executes_sets_idempotency_clears_pending(
    executor: ToolExecutor,
    fake_redis: _FakeRedisClient,
    sessionmaker: _FakeSessionmaker,
    principal: SessionUser,
) -> None:
    call_log: list[dict[str, Any]] = []
    _register_confirm_tool("fake_limit", call_log=call_log)
    args = {"current_limit": 25000, "requested_limit": 50000}
    fake_redis.pending[_SESSION_ID] = PendingConfirm(
        tool="fake_limit", args=args, proposed_turn=5, invocation_id="inv_old"
    )

    results = await executor.execute(
        [_call("fake_limit", args)],
        principal=principal,
        session_id=_SESSION_ID,
        turn_no=7,
        affirmed=True,
    )

    result = results[0]
    assert result.ok is True
    assert result.data == {
        "request_id": "LMT-2026-0724-0913",
        "status": "submitted",
        "eta_hours": 4,
    }
    assert result.idempotency_key == f"{_SESSION_ID}:fake_limit:7"
    assert call_log == [args]
    assert _SESSION_ID not in fake_redis.pending  # consumed decision cleared
    envelope = json.loads(fake_redis.idempotency[f"{_SESSION_ID}:fake_limit:7"])
    assert envelope["ok"] is True
    assert envelope["status"] == "ok"
    assert envelope["data"]["request_id"] == "LMT-2026-0724-0913"
    rows = _audit_rows(sessionmaker)
    assert [row.status for row in rows] == ["ok"]
    assert rows[0].idempotency_key == f"{_SESSION_ID}:fake_limit:7"


async def test_idempotency_replay_returns_stored_result_without_reexecuting(
    executor: ToolExecutor,
    fake_redis: _FakeRedisClient,
    sessionmaker: _FakeSessionmaker,
    principal: SessionUser,
) -> None:
    """docs/10 §4.2: a same-turn replay hits the stored terminal result
    — the merchant hears the same reference id once, no second business
    write, no second audit row (the UNIQUE backstop's point)."""
    call_log: list[dict[str, Any]] = []
    _register_confirm_tool("fake_limit", call_log=call_log)
    args = {"current_limit": 25000, "requested_limit": 50000}
    fake_redis.pending[_SESSION_ID] = PendingConfirm(
        tool="fake_limit", args=args, proposed_turn=5, invocation_id="inv_old"
    )
    await executor.execute(
        [_call("fake_limit", args)],
        principal=principal,
        session_id=_SESSION_ID,
        turn_no=7,
        affirmed=True,
    )
    rows_before = len(_audit_rows(sessionmaker))

    replayed = await executor.execute(
        [_call("fake_limit", args)],
        principal=principal,
        session_id=_SESSION_ID,
        turn_no=7,
        affirmed=True,
    )

    result = replayed[0]
    assert result.ok is True
    assert result.data is not None
    assert result.data["request_id"] == "LMT-2026-0724-0913"
    assert result.data["idempotent_replay"] is True  # documented replay marker
    assert len(call_log) == 1  # handler did NOT run again
    assert len(_audit_rows(sessionmaker)) == rows_before  # no second audit row


# --------------------------------------------------------------------------
# Same-transaction audit coupling + failure outcomes (docs/10 §2, §5)
# --------------------------------------------------------------------------


async def test_mutating_success_commits_business_write_and_audit_in_one_transaction(
    executor: ToolExecutor,
    fake_redis: _FakeRedisClient,
    sessionmaker: _FakeSessionmaker,
    principal: SessionUser,
) -> None:
    """THE task-4.4 resolution: the handler's repo and the OK audit row
    share one session, committed exactly once — docs/10 §2's "same
    transaction as the business write"."""
    handler_sessions: list[Any] = []
    _register_confirm_tool("fake_limit", handler_sessions=handler_sessions)
    args = {"current_limit": 25000, "requested_limit": 50000}
    fake_redis.pending[_SESSION_ID] = PendingConfirm(
        tool="fake_limit", args=args, proposed_turn=5, invocation_id="inv_old"
    )

    await executor.execute(
        [_call("fake_limit", args)],
        principal=principal,
        session_id=_SESSION_ID,
        turn_no=7,
        affirmed=True,
    )

    assert len(sessionmaker.sessions) == 1  # ONE session for write + audit
    session = sessionmaker.sessions[0]
    assert handler_sessions == [session]  # handler's repo used that same session
    assert session.commit_count == 1
    audit_entities = [e for e in session.added if isinstance(e, ToolInvocation)]
    assert len(audit_entities) == 1
    assert audit_entities[0].status == "ok"


async def test_handler_app_error_returns_business_result_and_error_audit(
    executor: ToolExecutor,
    fake_redis: _FakeRedisClient,
    sessionmaker: _FakeSessionmaker,
    principal: SessionUser,
) -> None:
    """docs/10 §5's business class: the AppError becomes a voiceable
    result; the business transaction is discarded uncommitted and the
    ERROR audit lands in a fresh one."""
    _register_confirm_tool(
        "fake_limit",
        raises=LimitRequestAlreadyPendingError(
            "A limit request is already pending",
            details={"existing_request_id": "LMT-2026-0724-0913", "status": "submitted"},
        ),
    )
    args = {"current_limit": 25000, "requested_limit": 50000}
    fake_redis.pending[_SESSION_ID] = PendingConfirm(
        tool="fake_limit", args=args, proposed_turn=5, invocation_id="inv_old"
    )

    results = await executor.execute(
        [_call("fake_limit", args)],
        principal=principal,
        session_id=_SESSION_ID,
        turn_no=7,
        affirmed=True,
    )

    result = results[0]
    assert result.ok is False
    assert result.status == ToolInvocationStatus.ERROR
    assert result.error is not None
    assert result.error["type"] == "business"
    assert result.error["code"] == "LIMIT_REQUEST_ALREADY_PENDING"
    assert result.error["detail"]["existing_request_id"] == "LMT-2026-0724-0913"
    handler_session = sessionmaker.sessions[0]
    assert handler_session.commit_count == 0  # business write rolled back
    rows = _audit_rows(sessionmaker)
    assert [row.status for row in rows] == ["error"]
    assert rows[0].error_code == "LIMIT_REQUEST_ALREADY_PENDING"
    # Terminal error is replayable and the consumed decision is cleared.
    assert f"{_SESSION_ID}:fake_limit:7" in fake_redis.idempotency
    assert _SESSION_ID not in fake_redis.pending


async def test_timeout_returns_timeout_shape_and_error_audit(
    fake_redis: _FakeRedisClient, sessionmaker: _FakeSessionmaker, principal: SessionUser
) -> None:
    _register_read_tool("slow_read", sleep_s=0.5)
    executor = ToolExecutor(
        registry=registry,
        safety=SafetyLayer(registry),
        redis=fake_redis,  # type: ignore[arg-type]
        sessionmaker=sessionmaker,  # type: ignore[arg-type]
        execution_timeout_s=0.05,
    )

    results = await executor.execute(
        [_call("slow_read", {})], principal=principal, session_id=_SESSION_ID, turn_no=3
    )

    result = results[0]
    assert result.ok is False
    assert result.error is not None
    assert result.error == {
        "type": "timeout",
        "code": "TOOL_TIMEOUT",
        "elapsed_ms": result.error["elapsed_ms"],
        "retryable": True,
    }
    # A plausible positive duration, not an exact bound: Windows'
    # ~15.6 ms clock granularity can measure a 50 ms wait_for as
    # slightly under 50 on the monotonic clock — the contract under
    # test is the timeout SHAPE, not timer precision.
    assert isinstance(result.error["elapsed_ms"], int)
    assert result.error["elapsed_ms"] > 0
    rows = _audit_rows(sessionmaker)
    assert [row.status for row in rows] == ["error"]
    assert rows[0].error_code == "TOOL_TIMEOUT"


async def test_unexpected_exception_returns_internal_shape_not_the_raw_error(
    executor: ToolExecutor, sessionmaker: _FakeSessionmaker, principal: SessionUser
) -> None:
    """docs/10 §5 + app/tools/errors.py's internal_error contract: the
    real exception is logged server-side; the LLM-facing result carries
    only the fixed generic string."""
    _register_read_tool("broken_read", raises=RuntimeError("secret db dsn leaked?"))

    results = await executor.execute(
        [_call("broken_read", {})], principal=principal, session_id=_SESSION_ID, turn_no=3
    )

    result = results[0]
    assert result.ok is False
    assert result.error == {
        "type": "business",
        "code": "INTERNAL",
        "detail": {"message": "An internal error occurred"},
        "retryable": False,
    }
    assert "secret" not in json.dumps(result.error)
    rows = _audit_rows(sessionmaker)
    assert [row.status for row in rows] == ["error"]
    assert rows[0].error_code == "INTERNAL"


# --------------------------------------------------------------------------
# The canonical trace, end to end (docs/10 §6, T5.1–T7.2)
# --------------------------------------------------------------------------


async def test_canonical_trace_t5_through_t7(
    executor: ToolExecutor,
    fake_redis: _FakeRedisClient,
    sessionmaker: _FakeSessionmaker,
    principal: SessionUser,
) -> None:
    """Reproduces docs/10 §6 against a fake `request_limit_increase`
    registered under the real name (the genuine registration is swapped
    out; the autouse fixture restores it): T5.1 invalid call -> T5.2
    validation shape, T5.3/T5.4 valid call -> gate held, T6 affirmation
    classified, T7.1 execute -> T7.2 result, then the reconnect replay."""
    del registry_module._REGISTRY["request_limit_increase"]
    del registry_module._RAW_HANDLERS["request_limit_increase"]
    call_log: list[dict[str, Any]] = []
    _register_confirm_tool("request_limit_increase", call_log=call_log)
    safety = SafetyLayer(registry)

    # T5.1/T5.2 — spelled-out amount -> schema reject, nothing audited.
    t5_bad = await executor.execute(
        [
            _call(
                "request_limit_increase",
                {"current_limit": 25000, "requested_limit": "fifty thousand"},
            )
        ],
        principal=principal,
        session_id=_SESSION_ID,
        turn_no=5,
    )
    assert t5_bad[0].error is not None
    assert t5_bad[0].error["code"] == "SCHEMA_VALIDATION_FAILED"
    assert _audit_rows(sessionmaker) == []

    # T5.3/T5.4 — valid retry, gate holds with the verbatim shape.
    t5 = await executor.execute(
        [_call("request_limit_increase", {"current_limit": 25000, "requested_limit": 50000})],
        principal=principal,
        session_id=_SESSION_ID,
        turn_no=5,
    )
    assert t5[0].ok is False
    assert t5[0].gate == {
        "status": "pending_confirm",
        "instruction": _GATE_INSTRUCTION,
        "action": {
            "tool": "request_limit_increase",
            "summary": "raise daily limit from ₹25,000 to ₹50,000",
        },
    }
    pending = fake_redis.pending[_SESSION_ID]
    assert pending.args == {"current_limit": 25000, "requested_limit": 50000}
    assert pending.proposed_turn == 5
    rows = _audit_rows(sessionmaker)
    assert [row.status for row in rows] == ["pending_confirm"]
    assert rows[0].idempotency_key == f"{_SESSION_ID}:request_limit_increase:5"
    assert call_log == []

    # T6 — "Yes, do it." is a clean affirmation of the pending action.
    assert safety.classify_affirmation("Yes, do it.", pending) is True

    # T7.1/T7.2 — affirmed re-emit executes once, atomically audited.
    t7 = await executor.execute(
        [_call("request_limit_increase", {"current_limit": 25000, "requested_limit": 50000})],
        principal=principal,
        session_id=_SESSION_ID,
        turn_no=7,
        affirmed=True,
    )
    assert t7[0].ok is True
    assert t7[0].data == {"request_id": "LMT-2026-0724-0913", "status": "submitted", "eta_hours": 4}
    assert t7[0].idempotency_key == f"{_SESSION_ID}:request_limit_increase:7"
    assert call_log == [{"current_limit": 25000, "requested_limit": 50000}]
    assert _SESSION_ID not in fake_redis.pending
    rows = _audit_rows(sessionmaker)
    assert [row.status for row in rows] == ["pending_confirm", "ok"]
    assert rows[1].idempotency_key == f"{_SESSION_ID}:request_limit_increase:7"

    # Reconnect variant — the turn-7 key replay: same reference id, once.
    replay = await executor.execute(
        [_call("request_limit_increase", {"current_limit": 25000, "requested_limit": 50000})],
        principal=principal,
        session_id=_SESSION_ID,
        turn_no=7,
        affirmed=True,
    )
    assert replay[0].data is not None
    assert replay[0].data["request_id"] == "LMT-2026-0724-0913"
    assert replay[0].data["idempotent_replay"] is True
    assert len(call_log) == 1
    assert len(_audit_rows(sessionmaker)) == 2


# --------------------------------------------------------------------------
# _truncate_data — docs/10 §2 format stage. Code-review MEDIUM: this had
# zero test coverage despite the module docstring's detailed JSON-safety
# claim.
# --------------------------------------------------------------------------


def test_truncate_data_leaves_small_payloads_unchanged() -> None:
    from app.agent.tool_executor import _truncate_data

    data = {"balance": 18450, "currency": "INR"}

    assert _truncate_data(data) == data


def test_truncate_data_cuts_long_lists_to_five_rows_with_truncation_markers() -> None:
    from app.agent.tool_executor import _MAX_LIST_ROWS, _truncate_data

    items = [{"id": i} for i in range(12)]

    result = _truncate_data({"items": items})

    assert len(result["items"]) == _MAX_LIST_ROWS
    assert result["truncated"] is True
    assert result["total_available"] == 12


def test_truncate_data_never_mutates_the_input_dict() -> None:
    from app.agent.tool_executor import _truncate_data

    original = {"items": list(range(20))}
    original_copy = dict(original)

    _truncate_data(original)

    assert original == original_copy


def test_truncate_data_falls_back_to_digest_when_still_oversized_and_emits_valid_json() -> None:
    """The claim this test pins: even when the digest fallback cuts the
    rendered JSON mid-string, the OUTER structure it returns is still
    valid JSON — the cut text is a Python str value json.dumps escapes
    correctly regardless of where it lands."""
    from app.agent.tool_executor import _MAX_RESULT_CHARS, _truncate_data

    # A single oversized string value the 5-row list truncation can't
    # help with — forces the digest fallback path.
    data = {"summary": "x" * (_MAX_RESULT_CHARS * 2)}

    result = _truncate_data(data)

    assert result["truncated"] is True
    assert "digest" in result
    # Round-trips through json.dumps/json.loads without error — the
    # actual "never emits corrupt JSON" claim, verified, not assumed.
    reparsed = json.loads(json.dumps(result))
    assert reparsed == result


def test_truncate_data_digest_fallback_survives_a_cut_mid_escape_sequence() -> None:
    """Adversarial case: the digest cut point lands inside what would be
    a JSON escape sequence in the ALREADY-rendered inner JSON — since the
    digest is a fresh Python str re-encoded by the OUTER json.dumps, this
    can never produce invalid JSON, unlike naively slicing a JSON string
    at the byte level would."""
    from app.agent.tool_executor import _MAX_RESULT_CHARS, _truncate_data

    # Backslashes and quotes near where the cut is likely to land.
    poisoned = ('\\"' * 400) + ("y" * _MAX_RESULT_CHARS)
    data = {"raw": poisoned}

    result = _truncate_data(data)

    reparsed = json.loads(json.dumps(result))
    assert reparsed == result
    assert result["truncated"] is True
