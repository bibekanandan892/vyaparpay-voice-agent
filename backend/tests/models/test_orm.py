"""ORM round-trip and CHECK-constraint tests for `app/models/orm.py`.

Fixture pattern: one session-scoped Postgres Testcontainer
(`pgvector/pgvector:pg16`, matching docs/12 §10's image decision),
migrated once via `alembic upgrade head` before any test runs. This
fixture set is deliberately self-contained for task 1.3 — it is expected
to be superseded by a shared `tests/conftest.py` fixture from a later
batch (Batch 2 task 2.3 owns that file); duplicating the container setup
here for now is fine because this file has no dependency outside
`app/models/orm.py`.

Per the Phase-2 plan (batch review gate), this file is authored but not
executed as part of task 1.3 — running it requires Docker Desktop
(Linux engine) and happens at the batch boundary.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import AsyncGenerator, Generator
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from testcontainers.postgres import PostgresContainer

from app.models.orm import (
    CallCost,
    Conversation,
    ConversationTurn,
    Merchant,
    MerchantLimit,
    ToolInvocation,
    Transaction,
    WalletAccount,
)

# backend/tests/models/test_orm.py -> backend/
BACKEND_ROOT = Path(__file__).resolve().parents[2]


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


@pytest_asyncio.fixture
async def engine(database_url: str) -> AsyncGenerator[AsyncEngine, None]:
    """Function-scoped ON PURPOSE (was session-scoped until CI's first real
    run of this suite): pytest-asyncio gives every test its own event loop,
    and a session-scoped engine pools asyncpg connections bound to the
    FIRST test's loop — every later test then dies with "attached to a
    different loop"/"Event loop is closed". A per-test engine keeps pool
    and loop lifetimes aligned; the container + migration stay
    session-scoped (sync fixtures, loop-agnostic)."""
    eng = create_async_engine(database_url, future=True)
    yield eng
    await eng.dispose()


@pytest_asyncio.fixture
async def session(engine: AsyncEngine) -> AsyncGenerator[AsyncSession, None]:
    """One `AsyncSession` per test, wrapped in a connection-level
    transaction that's rolled back afterward so fixture rows never leak
    between tests sharing the session-scoped container."""
    async with engine.connect() as conn:
        outer_txn = await conn.begin()
        maker = async_sessionmaker(bind=conn, expire_on_commit=False)
        async with maker() as sess:
            yield sess
        await outer_txn.rollback()


# --------------------------------------------------------------------------
# Round-trip tests
# --------------------------------------------------------------------------


async def test_merchant_round_trip(session: AsyncSession) -> None:
    merchant = Merchant(
        merchant_id="usr_test_merchant",
        business_name="Kumar General Store",
        city="Jaipur",
        account_type="Merchant Pro",
        merchant_since=date(2022, 1, 1),
    )
    session.add(merchant)
    await session.flush()

    fetched = await session.get(Merchant, "usr_test_merchant")

    assert fetched is not None
    assert fetched.business_name == "Kumar General Store"
    assert fetched.preferred_language == "English"  # server_default round-trips
    assert fetched.kyc_status == "verified"  # server_default round-trips
    assert fetched.created_at is not None  # server_default now() populated


async def test_wallet_account_round_trip(session: AsyncSession) -> None:
    merchant = Merchant(
        merchant_id="usr_test_wallet",
        business_name="Kumar General Store",
        city="Jaipur",
        account_type="Merchant Basic",
        merchant_since=date(2022, 1, 1),
    )
    session.add(merchant)
    await session.flush()

    wallet = WalletAccount(
        wallet_id="wal_test_wallet", merchant_id="usr_test_wallet", balance_paise=1_845_000
    )
    session.add(wallet)
    await session.flush()

    fetched = await session.get(WalletAccount, "wal_test_wallet")

    assert fetched is not None
    assert fetched.balance_paise == 1_845_000
    assert fetched.currency == "INR"


async def test_transaction_round_trip(session: AsyncSession) -> None:
    merchant = Merchant(
        merchant_id="usr_test_txn",
        business_name="Kumar General Store",
        city="Jaipur",
        account_type="Merchant Pro",
        merchant_since=date(2022, 1, 1),
    )
    session.add(merchant)
    await session.flush()
    wallet = WalletAccount(wallet_id="wal_test_txn", merchant_id="usr_test_txn")
    session.add(wallet)
    await session.flush()

    txn = Transaction(
        txn_id="txn_test_0001",
        merchant_id="usr_test_txn",
        wallet_id="wal_test_txn",
        type="vendor_payment",
        amount_paise=24_500,
        counterparty="Amazon Business",
        status="succeeded",
    )
    session.add(txn)
    await session.flush()

    fetched = await session.get(Transaction, "txn_test_0001")

    assert fetched is not None
    assert fetched.amount_paise == 24_500
    assert fetched.decline_code is None


# --------------------------------------------------------------------------
# CHECK-constraint tests
# --------------------------------------------------------------------------


async def test_declined_transaction_without_decline_code_raises(session: AsyncSession) -> None:
    """`CHECK ((status = 'declined') = (decline_code IS NOT NULL))` —
    a declined row with no reason is unrepresentable (docs/12 §3.3)."""
    merchant = Merchant(
        merchant_id="usr_test_decline",
        business_name="Kumar General Store",
        city="Jaipur",
        account_type="Merchant Pro",
        merchant_since=date(2022, 1, 1),
    )
    session.add(merchant)
    await session.flush()
    wallet = WalletAccount(wallet_id="wal_test_decline", merchant_id="usr_test_decline")
    session.add(wallet)
    await session.flush()

    bad_txn = Transaction(
        txn_id="txn_test_bad",
        merchant_id="usr_test_decline",
        wallet_id="wal_test_decline",
        type="vendor_payment",
        amount_paise=24_500,
        counterparty="Amazon Business",
        status="declined",
        decline_code=None,
    )
    session.add(bad_txn)

    with pytest.raises(IntegrityError):
        await session.flush()


async def test_merchant_limit_requested_paise_must_exceed_limit_paise(
    session: AsyncSession,
) -> None:
    """`CHECK (requested_paise > limit_paise)` — a limit-increase
    request that doesn't actually raise the limit is nonsensical
    (docs/12 §3.2)."""
    merchant = Merchant(
        merchant_id="usr_test_limit",
        business_name="Kumar General Store",
        city="Jaipur",
        account_type="Merchant Pro",
        merchant_since=date(2022, 1, 1),
    )
    session.add(merchant)
    await session.flush()

    bad_limit = MerchantLimit(
        merchant_id="usr_test_limit",
        limit_type="daily_txn",
        limit_paise=2_500_000,
        window_date=date(2026, 7, 24),
        request_id="LMT-test-0001",
        requested_paise=2_000_000,  # <= limit_paise: must violate the CHECK
        request_status="submitted",
        requested_at=datetime.now(UTC),
    )
    session.add(bad_limit)

    with pytest.raises(IntegrityError):
        await session.flush()


async def test_merchant_limit_request_pair_requires_all_four_fields(
    session: AsyncSession,
) -> None:
    """`ck_merchant_limits_request_pair` is a 4-way pairing, not just
    request_id/request_status — a "submitted" request with no
    requested_paise/requested_at is a phantom request and must be
    rejected (this is the exact gap the PR review flagged: Postgres
    CHECKs pass vacuously on NULL, so a 2-way pairing alone would let
    this row through)."""
    merchant = Merchant(
        merchant_id="usr_test_pair",
        business_name="Kumar General Store",
        city="Jaipur",
        account_type="Merchant Pro",
        merchant_since=date(2022, 1, 1),
    )
    session.add(merchant)
    await session.flush()

    phantom_request = MerchantLimit(
        merchant_id="usr_test_pair",
        limit_type="daily_txn",
        limit_paise=2_500_000,
        window_date=date(2026, 7, 24),
        request_id="LMT-test-0002",
        request_status="submitted",
        requested_paise=None,  # phantom: request "exists" with no amount
        requested_at=None,
    )
    session.add(phantom_request)

    with pytest.raises(IntegrityError):
        await session.flush()


async def test_conversation_round_trip(session: AsyncSession) -> None:
    merchant = Merchant(
        merchant_id="usr_test_conv",
        business_name="Kumar General Store",
        city="Jaipur",
        account_type="Merchant Pro",
        merchant_since=date(2022, 1, 1),
    )
    session.add(merchant)
    await session.flush()

    conversation = Conversation(
        session_id="sess_test_0001",
        user_id="usr_test_conv",
        signaling_token_hash="deadbeef",
    )
    session.add(conversation)
    await session.flush()

    fetched = await session.get(Conversation, "sess_test_0001")

    assert fetched is not None
    assert fetched.state == "created"  # server_default round-trips
    assert fetched.started_at is not None
    assert fetched.ended_at is None


async def test_conversation_turn_round_trip(session: AsyncSession) -> None:
    merchant = Merchant(
        merchant_id="usr_test_turn",
        business_name="Kumar General Store",
        city="Jaipur",
        account_type="Merchant Pro",
        merchant_since=date(2022, 1, 1),
    )
    session.add(merchant)
    await session.flush()
    conversation = Conversation(
        session_id="sess_test_turn",
        user_id="usr_test_turn",
        signaling_token_hash="deadbeef",
    )
    session.add(conversation)
    await session.flush()

    turn = ConversationTurn(
        session_id="sess_test_turn",
        turn_no=1,
        role="agent",
        started_at=datetime.now(UTC),
    )
    session.add(turn)
    await session.flush()

    fetched = await session.get(ConversationTurn, ("sess_test_turn", 1))

    assert fetched is not None
    assert fetched.tool_calls == []  # server_default '{}' round-trips as empty list
    assert fetched.text is None  # regression check: the `text` column name
    # doesn't collide with the sqlalchemy.text() import (renamed to
    # sa_text in orm.py specifically to remove that landmine)


async def test_tool_invocation_round_trip(session: AsyncSession) -> None:
    merchant = Merchant(
        merchant_id="usr_test_tool",
        business_name="Kumar General Store",
        city="Jaipur",
        account_type="Merchant Pro",
        merchant_since=date(2022, 1, 1),
    )
    session.add(merchant)
    await session.flush()
    conversation = Conversation(
        session_id="sess_test_tool",
        user_id="usr_test_tool",
        signaling_token_hash="deadbeef",
    )
    session.add(conversation)
    await session.flush()

    invocation = ToolInvocation(
        session_id="sess_test_tool",
        turn_no=3,
        tool_name="get_wallet_balance",
        input={},
        output={"balance": 18450, "currency": "INR"},
        status="ok",
        latency_ms=12,
        idempotency_key="sess_test_tool:get_wallet_balance:3",
    )
    session.add(invocation)
    await session.flush()

    fetched = await session.get(ToolInvocation, invocation.invocation_id)

    assert fetched is not None
    assert fetched.invocation_id is not None  # server_default gen_random_uuid() populated
    assert fetched.output == {"balance": 18450, "currency": "INR"}
    assert fetched.status == "ok"


async def test_tool_invocation_status_check(session: AsyncSession) -> None:
    """`ck_tool_invocations_status` — only the 5 canon statuses are
    representable (docs/12 §4.4, ToolInvocationStatus in app/domain/types.py)."""
    merchant = Merchant(
        merchant_id="usr_test_status",
        business_name="Kumar General Store",
        city="Jaipur",
        account_type="Merchant Pro",
        merchant_since=date(2022, 1, 1),
    )
    session.add(merchant)
    await session.flush()
    conversation = Conversation(
        session_id="sess_test_status",
        user_id="usr_test_status",
        signaling_token_hash="deadbeef",
    )
    session.add(conversation)
    await session.flush()

    bad_invocation = ToolInvocation(
        session_id="sess_test_status",
        tool_name="get_wallet_balance",
        input={},
        status="not_a_real_status",
        latency_ms=5,
    )
    session.add(bad_invocation)

    with pytest.raises(IntegrityError):
        await session.flush()


async def test_tool_invocation_error_code_pair_check(session: AsyncSession) -> None:
    """`ck_tool_invocations_error_code_pair` — a status='error' row with
    no error_code weakens the audit trail's diagnostic value; mirrors
    `ck_transactions_decline_code_pair`'s existing pattern."""
    merchant = Merchant(
        merchant_id="usr_test_errpair",
        business_name="Kumar General Store",
        city="Jaipur",
        account_type="Merchant Pro",
        merchant_since=date(2022, 1, 1),
    )
    session.add(merchant)
    await session.flush()
    conversation = Conversation(
        session_id="sess_test_errpair",
        user_id="usr_test_errpair",
        signaling_token_hash="deadbeef",
    )
    session.add(conversation)
    await session.flush()

    bad_invocation = ToolInvocation(
        session_id="sess_test_errpair",
        tool_name="get_wallet_balance",
        input={},
        status="error",
        error_code=None,  # must violate: status='error' requires error_code
        latency_ms=5,
    )
    session.add(bad_invocation)

    with pytest.raises(IntegrityError):
        await session.flush()


async def test_call_cost_round_trip(session: AsyncSession) -> None:
    merchant = Merchant(
        merchant_id="usr_test_cost",
        business_name="Kumar General Store",
        city="Jaipur",
        account_type="Merchant Pro",
        merchant_since=date(2022, 1, 1),
    )
    session.add(merchant)
    await session.flush()
    conversation = Conversation(
        session_id="sess_test_cost",
        user_id="usr_test_cost",
        signaling_token_hash="deadbeef",
    )
    session.add(conversation)
    await session.flush()

    cost = CallCost(
        session_id="sess_test_cost",
        stt_usd=Decimal("0.04"),
        llm_dialogue_usd=Decimal("0.10"),
        llm_utility_usd=Decimal("0.00"),
        embeddings_usd=Decimal("0.00"),
        tts_usd=Decimal("0.15"),
        total_usd=Decimal("0.29"),
        stt_seconds=180,
        input_tokens=2000,
        cached_input_tokens=800,
        output_tokens=120,
        tts_chars=900,
    )
    session.add(cost)
    await session.flush()

    fetched = await session.get(CallCost, "sess_test_cost")

    assert fetched is not None
    assert fetched.total_usd == Decimal("0.29")
    assert fetched.turn_infra_usd == Decimal("0")  # server_default round-trips


async def test_call_cost_total_must_equal_component_sum(session: AsyncSession) -> None:
    """`ck_call_costs_total_usd` — the aggregate and the per-component
    drill-down can never disagree (docs/12 §4.5)."""
    merchant = Merchant(
        merchant_id="usr_test_costcheck",
        business_name="Kumar General Store",
        city="Jaipur",
        account_type="Merchant Pro",
        merchant_since=date(2022, 1, 1),
    )
    session.add(merchant)
    await session.flush()
    conversation = Conversation(
        session_id="sess_test_costcheck",
        user_id="usr_test_costcheck",
        signaling_token_hash="deadbeef",
    )
    session.add(conversation)
    await session.flush()

    bad_cost = CallCost(
        session_id="sess_test_costcheck",
        stt_usd=Decimal("0.04"),
        llm_dialogue_usd=Decimal("0.10"),
        llm_utility_usd=Decimal("0.00"),
        embeddings_usd=Decimal("0.00"),
        tts_usd=Decimal("0.15"),
        total_usd=Decimal("999.00"),  # deliberately wrong sum
        stt_seconds=180,
        input_tokens=2000,
        cached_input_tokens=800,
        output_tokens=120,
        tts_chars=900,
    )
    session.add(bad_cost)

    with pytest.raises(IntegrityError):
        await session.flush()
