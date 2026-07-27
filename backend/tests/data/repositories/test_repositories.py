"""Unit tests for `app/data/repositories/*.py` (docs/04-backend-
architecture.md §5).

No Docker Desktop is available in this environment (verified: `docker
info` fails with "command not found" both in the bash and PowerShell
tools used to write this file), so these tests do **not** use
testcontainers — unlike `backend/tests/models/test_orm.py`, which has a
working Postgres-testcontainer fixture for a *later* batch boundary run.
`app/models/orm.py` also leans on Postgres-only types (`JSONB`, `ARRAY`,
`gen_random_uuid()`) that plain SQLite can't represent, so a SQLite
fallback was rejected too.

Instead, every test drives each repository against a `unittest.mock`
fake of `AsyncSession` (`spec=AsyncSession`, so sync methods like `.add`
and async methods like `.get`/`.execute`/`.flush`/`.merge` are mocked
with the correct sync/async shape automatically). This verifies each
repo's own logic — what entity it builds, which branch it takes, what it
returns/raises — without verifying the SQL a real Postgres would execute;
that verification is deferred to the `testcontainers`-backed pass at the
Batch-2 boundary once Docker Desktop is running, using the same fixture
pattern as `tests/models/test_orm.py`.

These tests were executed locally against a throwaway venv (Docker was
not required) — see the task report for the pytest run output.
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.errors import (
    AppError,
    LimitRequestAlreadyPendingError,
    ResourceNotFoundError,
    StaleLimitViewError,
    ValidationSchemaError,
)
from app.data.repositories.base import SqlAlchemyRepository
from app.data.repositories.conversation_repo import ConversationRepo
from app.data.repositories.cost_repo import CostRepo
from app.data.repositories.limit_repo import LimitRepo, _generate_request_id
from app.data.repositories.merchant_repo import MerchantRepo
from app.data.repositories.payment_repo import PaymentRepo, _midnight_ist_after
from app.data.repositories.tool_audit_repo import ToolAuditRepo
from app.data.repositories.wallet_repo import WalletRepo
from app.models.orm import (
    Conversation,
    ConversationTurn,
    Merchant,
    MerchantLimit,
    ToolInvocation,
    Transaction,
    WalletAccount,
)

_REQUEST_ID_RE = re.compile(r"^LMT-\d{4}-\d{4}-\d{4}$")


def make_session() -> AsyncSession:
    """A `spec=AsyncSession` mock: `.add` becomes a plain `MagicMock`
    (the real method is sync) while `.get`/`.execute`/`.flush`/`.merge`
    become `AsyncMock`s (the real methods are coroutines) — `spec`
    inspects the real class to get this split right automatically."""
    return AsyncMock(spec=AsyncSession)


def execute_result(*, scalar: object = None, row: object = None) -> MagicMock:
    """A fake `Result` for `session.execute(...)` to return: configure
    whichever accessor the method under test actually calls."""
    result = MagicMock()
    result.scalar_one_or_none.return_value = scalar
    result.one_or_none.return_value = row
    return result


# --------------------------------------------------------------------------
# base.SqlAlchemyRepository
# --------------------------------------------------------------------------


class _FakeRepo(SqlAlchemyRepository[Merchant]):
    model = Merchant


async def test_base_get_delegates_to_session_get() -> None:
    session = make_session()
    merchant = Merchant(
        merchant_id="usr_1",
        business_name="x",
        city="y",
        account_type="Merchant Pro",
        merchant_since=date(2022, 1, 1),
    )
    session.get.return_value = merchant
    repo = _FakeRepo(session)

    result = await repo.get("usr_1")

    session.get.assert_awaited_once_with(Merchant, "usr_1")
    assert result is merchant


async def test_base_add_adds_and_flushes() -> None:
    session = make_session()
    repo = _FakeRepo(session)
    merchant = Merchant(
        merchant_id="usr_1",
        business_name="x",
        city="y",
        account_type="Merchant Pro",
        merchant_since=date(2022, 1, 1),
    )

    result = await repo.add(merchant)

    session.add.assert_called_once_with(merchant)
    session.flush.assert_awaited_once()
    assert result is merchant


async def test_base_update_merges_and_flushes() -> None:
    session = make_session()
    merchant = Merchant(
        merchant_id="usr_1",
        business_name="x",
        city="y",
        account_type="Merchant Pro",
        merchant_since=date(2022, 1, 1),
    )
    merged = Merchant(
        merchant_id="usr_1",
        business_name="x2",
        city="y",
        account_type="Merchant Pro",
        merchant_since=date(2022, 1, 1),
    )
    session.merge.return_value = merged
    repo = _FakeRepo(session)

    result = await repo.update(merchant)

    session.merge.assert_awaited_once_with(merchant)
    session.flush.assert_awaited_once()
    assert result is merged


# --------------------------------------------------------------------------
# MerchantRepo
# --------------------------------------------------------------------------


async def test_merchant_repo_get_returns_none_when_absent() -> None:
    session = make_session()
    session.get.return_value = None
    repo = MerchantRepo(session)

    assert await repo.get("usr_missing") is None
    session.get.assert_awaited_once_with(Merchant, "usr_missing")


# --------------------------------------------------------------------------
# WalletRepo
# --------------------------------------------------------------------------


async def test_wallet_repo_get_by_merchant_found() -> None:
    session = make_session()
    wallet = WalletAccount(wallet_id="wal_1", merchant_id="usr_1", balance_paise=1_845_000)
    session.execute.return_value = execute_result(scalar=wallet)
    repo = WalletRepo(session)

    result = await repo.get_by_merchant("usr_1")

    assert result is wallet
    session.execute.assert_awaited_once()


async def test_wallet_repo_get_by_merchant_not_found() -> None:
    session = make_session()
    session.execute.return_value = execute_result(scalar=None)
    repo = WalletRepo(session)

    assert await repo.get_by_merchant("usr_missing") is None


# --------------------------------------------------------------------------
# PaymentRepo
# --------------------------------------------------------------------------


def _limit_row(
    *,
    limit_paise: int = 2_500_000,
    used_paise: int = 2_489_000,
    window_date: date = date(2026, 7, 24),
) -> MerchantLimit:
    return MerchantLimit(
        merchant_id="usr_rajesh01",
        limit_type="daily_txn",
        limit_paise=limit_paise,
        used_paise=used_paise,
        window_date=window_date,
    )


async def test_payment_repo_create_builds_transaction_and_adds() -> None:
    session = make_session()
    repo = PaymentRepo(session)

    txn = await repo.create(
        txn_id="txn_0724_1414a",
        merchant_id="usr_rajesh01",
        wallet_id="wal_rajesh01",
        txn_type="vendor_payment",
        amount_paise=24_500,
        counterparty="Amazon Business",
        status="declined",
        decline_code="DAILY_LIMIT_EXCEEDED",
        http_status=402,
    )

    assert isinstance(txn, Transaction)
    assert txn.txn_id == "txn_0724_1414a"
    assert txn.type == "vendor_payment"
    assert txn.amount_paise == 24_500
    assert txn.decline_code == "DAILY_LIMIT_EXCEEDED"
    session.add.assert_called_once_with(txn)
    session.flush.assert_awaited_once()


async def test_payment_repo_check_daily_limit_exceeded() -> None:
    session = make_session()
    session.get.return_value = _limit_row(limit_paise=2_500_000, used_paise=2_489_000)
    repo = PaymentRepo(session)

    check = await repo.check_daily_limit("usr_rajesh01", 24_500)

    assert check is not None
    assert check.exceeded is True  # 2_489_000 + 24_500 > 2_500_000
    assert check.limit_paise == 2_500_000
    assert check.used_paise == 2_489_000
    assert check.resets_at == datetime(
        2026, 7, 25, 0, 0, tzinfo=timezone(timedelta(hours=5, minutes=30))
    )


async def test_payment_repo_check_daily_limit_not_exceeded() -> None:
    session = make_session()
    session.get.return_value = _limit_row(limit_paise=2_500_000, used_paise=1_000_000)
    repo = PaymentRepo(session)

    check = await repo.check_daily_limit("usr_rajesh01", 24_500)

    assert check is not None
    assert check.exceeded is False  # 1_000_000 + 24_500 <= 2_500_000


async def test_payment_repo_check_daily_limit_exactly_at_limit_is_not_exceeded() -> None:
    """Boundary case (review finding: this exact edge had no regression
    guard): `used_paise + amount_paise == limit_paise` is the strict `>`
    check's false case, matching docs/12 §3.2's own wording verbatim."""
    session = make_session()
    session.get.return_value = _limit_row(limit_paise=2_500_000, used_paise=2_475_500)
    repo = PaymentRepo(session)

    check = await repo.check_daily_limit("usr_rajesh01", 24_500)

    assert check is not None
    assert check.exceeded is False  # 2_475_500 + 24_500 == 2_500_000, not >


async def test_payment_repo_check_daily_limit_no_row_returns_none() -> None:
    session = make_session()
    session.get.return_value = None
    repo = PaymentRepo(session)

    assert await repo.check_daily_limit("usr_no_limit_row", 100) is None


async def test_payment_repo_check_daily_limit_defaults_to_no_locking() -> None:
    session = make_session()
    session.get.return_value = _limit_row()
    repo = PaymentRepo(session)

    await repo.check_daily_limit("usr_rajesh01", 100)

    session.get.assert_awaited_once_with(
        MerchantLimit, ("usr_rajesh01", "daily_txn"), with_for_update=False
    )


async def test_payment_repo_check_daily_limit_passes_through_with_for_update() -> None:
    """The locking seam a future payment-write handler needs (review
    finding) — opt-in, not the default (see the method's own docstring
    for why)."""
    session = make_session()
    session.get.return_value = _limit_row()
    repo = PaymentRepo(session)

    await repo.check_daily_limit("usr_rajesh01", 100, with_for_update=True)

    session.get.assert_awaited_once_with(
        MerchantLimit, ("usr_rajesh01", "daily_txn"), with_for_update=True
    )


async def test_payment_repo_get_payment_status_not_found() -> None:
    session = make_session()
    session.execute.return_value = execute_result(row=None)
    repo = PaymentRepo(session)

    assert await repo.get_payment_status("txn_missing", merchant_id="usr_rajesh01") is None


async def test_payment_repo_get_payment_status_succeeded_no_limit_context() -> None:
    session = make_session()
    txn = Transaction(
        txn_id="txn_ok",
        merchant_id="usr_rajesh01",
        wallet_id="wal_rajesh01",
        type="vendor_payment",
        amount_paise=9_500,
        counterparty="Amazon Business",
        status="succeeded",
        decline_code=None,
    )
    session.execute.return_value = execute_result(row=(txn, _limit_row()))
    repo = PaymentRepo(session)

    status = await repo.get_payment_status("txn_ok", merchant_id="usr_rajesh01")

    assert status is not None
    assert status.status == "succeeded"
    assert status.decline_code is None
    assert status.limit_context is None  # not limit-related, even though a limit row joined


async def test_payment_repo_get_payment_status_scopes_the_query_to_merchant_id() -> None:
    """Review finding (HIGH, security): the WHERE clause must filter on
    merchant_id, not just txn_id — otherwise a wrong-owner txn_id would
    return another merchant's payment amount/counterparty/decline
    reason instead of the None a caller expects to map to
    RESOURCE_NOT_FOUND (docs/13 §1.1: "deliberately indistinguishable").
    This inspects the actual compiled statement, not just the return
    value, so a regression that drops the filter but coincidentally
    still passes the mocked result would still be caught."""
    session = make_session()
    session.execute.return_value = execute_result(row=None)
    repo = PaymentRepo(session)

    await repo.get_payment_status("txn_0724_1414a", merchant_id="usr_rajesh01")

    stmt = session.execute.await_args.args[0]
    compiled = stmt.compile(dialect=postgresql.dialect())
    assert "transactions.merchant_id" in str(compiled)
    # merchant_id_1 is the join condition's bind param (MerchantLimit.
    # merchant_id == Transaction.merchant_id); merchant_id_2 is this
    # method's own WHERE-clause filter — the one this test exists to pin.
    assert compiled.params["merchant_id_2"] == "usr_rajesh01"


async def test_payment_repo_get_payment_status_daily_limit_exceeded_populates_context() -> None:
    session = make_session()
    txn = Transaction(
        txn_id="txn_0724_1414a",
        merchant_id="usr_rajesh01",
        wallet_id="wal_rajesh01",
        type="vendor_payment",
        amount_paise=24_500,
        counterparty="Amazon Business",
        status="declined",
        decline_code="DAILY_LIMIT_EXCEEDED",
        http_status=402,
    )
    limit_row = _limit_row(
        limit_paise=2_500_000, used_paise=2_489_000, window_date=date(2026, 7, 24)
    )
    session.execute.return_value = execute_result(row=(txn, limit_row))
    repo = PaymentRepo(session)

    status = await repo.get_payment_status("txn_0724_1414a", merchant_id="usr_rajesh01")

    assert status is not None
    assert status.payment_id == "txn_0724_1414a"
    assert status.status == "declined"
    assert status.decline_code == "DAILY_LIMIT_EXCEEDED"
    assert status.limit_context is not None
    assert status.limit_context.limit_paise == 2_500_000
    assert status.limit_context.used_today_paise == 2_489_000
    assert status.limit_context.resets_at.isoformat() == "2026-07-25T00:00:00+05:30"


async def test_payment_repo_get_payment_status_limit_related_but_no_joined_row() -> None:
    """Defensive path: decline_code is limit-related but the outer join
    found no merchant_limits row (shouldn't happen in seeded data, but
    the join is a LEFT JOIN precisely so this can't raise)."""
    session = make_session()
    txn = Transaction(
        txn_id="txn_weird",
        merchant_id="usr_rajesh01",
        wallet_id="wal_rajesh01",
        type="vendor_payment",
        amount_paise=100,
        counterparty="X",
        status="declined",
        decline_code="DAILY_LIMIT_EXCEEDED",
    )
    session.execute.return_value = execute_result(row=(txn, None))
    repo = PaymentRepo(session)

    status = await repo.get_payment_status("txn_weird", merchant_id="usr_rajesh01")

    assert status is not None
    assert status.limit_context is None


def test_midnight_ist_after_matches_docs_worked_example() -> None:
    """docs/13-api-contracts.md §3.2's worked example: window_date
    2026-07-24 -> resets_at 2026-07-25T00:00:00+05:30."""
    result = _midnight_ist_after(date(2026, 7, 24))
    assert result.isoformat() == "2026-07-25T00:00:00+05:30"


# --------------------------------------------------------------------------
# LimitRepo
# --------------------------------------------------------------------------


async def test_limit_repo_get_by_key_uses_composite_key() -> None:
    session = make_session()
    row = _limit_row()
    session.get.return_value = row
    repo = LimitRepo(session)

    result = await repo.get_by_key("usr_rajesh01", "daily_txn")

    session.get.assert_awaited_once_with(MerchantLimit, ("usr_rajesh01", "daily_txn"))
    assert result is row


async def test_limit_repo_does_not_break_the_repository_protocol_get() -> None:
    """Review finding (MEDIUM): `get_by_key` is a distinctly-named method,
    not an override of the inherited `Repository[T].get(id)` floor — a
    `LimitRepo` instance must still type- and behavior-check as satisfying
    `Repository[MerchantLimit]` (docs/04 §5's frozen contract), even
    though a single-`id` `get` can't meaningfully address a composite-PK
    row. `LimitRepo` deliberately doesn't override `get`; nothing in this
    class need be called to prove that beyond `get_by_key` existing as
    its own method (mypy is the enforcement mechanism for the Protocol
    conformance itself, not this test)."""
    session = make_session()
    repo = LimitRepo(session)

    assert hasattr(repo, "get_by_key")
    assert hasattr(repo, "get")  # inherited, unmodified, from SqlAlchemyRepository


async def test_limit_repo_submit_increase_request_no_row_raises_not_found() -> None:
    session = make_session()
    session.get.return_value = None
    repo = LimitRepo(session)

    with pytest.raises(ResourceNotFoundError):
        await repo.submit_increase_request("usr_missing", "daily_txn", 2_500_000, 5_000_000)


async def test_limit_repo_submit_increase_request_locks_the_row_for_update() -> None:
    """Review finding (HIGH, security): the initial read must use
    `with_for_update=True` — without it, two concurrent submissions for
    the same (merchant_id, limit_type) can both observe
    request_status IS NULL before either commits, and the second write
    silently overwrites the first instead of raising
    LimitRequestAlreadyPendingError."""
    session = make_session()
    session.get.return_value = _limit_row(limit_paise=2_500_000)
    repo = LimitRepo(session)

    await repo.submit_increase_request("usr_rajesh01", "daily_txn", 2_500_000, 5_000_000)

    session.get.assert_awaited_once_with(
        MerchantLimit, ("usr_rajesh01", "daily_txn"), with_for_update=True
    )


async def test_limit_repo_submit_increase_request_stale_view_raises() -> None:
    session = make_session()
    session.get.return_value = _limit_row(limit_paise=2_500_000)
    repo = LimitRepo(session)

    with pytest.raises(StaleLimitViewError):
        # caller believes the limit is 2_000_000 but the live row says 2_500_000
        await repo.submit_increase_request("usr_rajesh01", "daily_txn", 2_000_000, 5_000_000)


async def test_limit_repo_submit_increase_request_already_pending_raises() -> None:
    session = make_session()
    row = _limit_row(limit_paise=2_500_000)
    row.request_id = "LMT-2026-0724-0913"
    row.request_status = "submitted"
    row.requested_paise = 5_000_000
    row.requested_at = datetime(2026, 7, 24, 9, 16, 4, tzinfo=UTC)
    session.get.return_value = row
    repo = LimitRepo(session)

    with pytest.raises(LimitRequestAlreadyPendingError) as exc_info:
        await repo.submit_increase_request("usr_rajesh01", "daily_txn", 2_500_000, 5_000_000)

    assert exc_info.value.details is not None
    assert exc_info.value.details["existing_request_id"] == "LMT-2026-0724-0913"
    assert exc_info.value.details["status"] == "submitted"


@pytest.mark.parametrize("terminal_status", ["approved", "rejected"])
async def test_limit_repo_submit_increase_request_terminal_status_does_not_block(
    terminal_status: str,
) -> None:
    """Review finding (MEDIUM): a resolved (terminal) prior request must
    NOT block a new one — only `request_status == "submitted"` is
    actually pending (docs/12 §3.2's auto-approve job filters on exactly
    that value). The original check (`is not None`) would have
    permanently rejected every future request for this merchant/
    limit_type once the first one ever resolved."""
    session = make_session()
    row = _limit_row(limit_paise=2_500_000)
    row.request_id = "LMT-2026-0723-1000"
    row.request_status = terminal_status
    row.requested_paise = 3_000_000
    row.requested_at = datetime(2026, 7, 23, 10, 0, 0, tzinfo=UTC)
    session.get.return_value = row
    repo = LimitRepo(session)

    result = await repo.submit_increase_request("usr_rajesh01", "daily_txn", 2_500_000, 5_000_000)

    assert result.status == "submitted"
    assert row.requested_paise == 5_000_000  # overwritten by the new request


async def test_limit_repo_submit_increase_request_rejects_a_non_increasing_amount() -> None:
    """Review finding (MEDIUM + security): must be checked here, not left
    to ck_merchant_limits_requested_paise alone — a violation is a typed
    ValidationSchemaError, never a raw IntegrityError out of this method
    (docs/10 §5: "errors are results, not exceptions")."""
    session = make_session()
    session.get.return_value = _limit_row(limit_paise=2_500_000)
    repo = LimitRepo(session)

    with pytest.raises(ValidationSchemaError) as exc_info:
        # 2_500_000 doesn't exceed the current 2_500_000 limit
        await repo.submit_increase_request("usr_rajesh01", "daily_txn", 2_500_000, 2_500_000)

    assert exc_info.value.details is not None
    assert exc_info.value.details["requested_limit_paise"] == 2_500_000
    session.flush.assert_not_awaited()


async def test_limit_repo_submit_increase_request_wraps_integrity_error() -> None:
    """Review finding (MEDIUM + security): the one remaining IntegrityError
    path (a same-UTC-minute request_id collision between two merchants,
    per _generate_request_id's own docstring) must surface as a typed,
    retryable AppError — never an unclassified 500."""
    session = make_session()
    session.get.return_value = _limit_row(limit_paise=2_500_000)
    session.flush.side_effect = IntegrityError("stmt", {}, Exception("duplicate key"))
    repo = LimitRepo(session)

    with pytest.raises(AppError) as exc_info:
        await repo.submit_increase_request("usr_rajesh01", "daily_txn", 2_500_000, 5_000_000)

    assert exc_info.value.retryable is True


async def test_limit_repo_submit_increase_request_success_sets_all_four_fields_together() -> None:
    session = make_session()
    row = _limit_row(limit_paise=2_500_000)
    assert row.request_id is None
    assert row.requested_paise is None
    assert row.request_status is None
    assert row.requested_at is None
    session.get.return_value = row
    repo = LimitRepo(session)

    result = await repo.submit_increase_request("usr_rajesh01", "daily_txn", 2_500_000, 5_000_000)

    # All four fields land together on the row (ck_merchant_limits_request_pair).
    assert row.request_id is not None
    assert row.requested_paise == 5_000_000
    assert row.request_status == "submitted"
    assert row.requested_at is not None

    assert _REQUEST_ID_RE.match(result.request_id)
    assert result.status == "submitted"
    assert result.eta_hours == 4
    assert row.request_id == result.request_id
    session.flush.assert_awaited_once()


def test_generate_request_id_matches_docs_pattern_and_worked_example_shape() -> None:
    now = datetime(2026, 7, 24, 9, 13, 0, tzinfo=UTC)
    request_id = _generate_request_id(now)

    assert request_id == "LMT-2026-0724-0913"
    assert _REQUEST_ID_RE.match(request_id)


# --------------------------------------------------------------------------
# ConversationRepo
# --------------------------------------------------------------------------


async def test_conversation_repo_create_builds_entity_without_setting_state() -> None:
    session = make_session()
    repo = ConversationRepo(session)

    conversation = await repo.create("sess_1", "usr_rajesh01", "deadbeef")

    assert isinstance(conversation, Conversation)
    assert conversation.session_id == "sess_1"
    assert conversation.user_id == "usr_rajesh01"
    assert conversation.signaling_token_hash == "deadbeef"
    # state is left unset here deliberately -> the column's server_default owns it.
    session.add.assert_called_once_with(conversation)
    session.flush.assert_awaited_once()


async def test_conversation_repo_end_not_found_returns_none() -> None:
    session = make_session()
    session.get.return_value = None
    repo = ConversationRepo(session)

    assert await repo.end("sess_missing") is None
    session.flush.assert_not_awaited()


async def test_conversation_repo_end_sets_state_and_ended_at() -> None:
    session = make_session()
    conversation = Conversation(
        session_id="sess_1",
        user_id="usr_rajesh01",
        signaling_token_hash="deadbeef",
        state="in_call",
    )
    session.get.return_value = conversation
    repo = ConversationRepo(session)

    result = await repo.end("sess_1")

    assert result is conversation
    assert conversation.state == "ended"
    assert conversation.ended_at is not None
    session.flush.assert_awaited_once()


async def test_conversation_repo_append_turn_defaults_tool_calls_and_started_at() -> None:
    session = make_session()
    repo = ConversationRepo(session)

    turn = await repo.append_turn("sess_1", 1, "agent")

    assert isinstance(turn, ConversationTurn)
    assert turn.session_id == "sess_1"
    assert turn.turn_no == 1
    assert turn.role == "agent"
    assert turn.tool_calls == []
    assert turn.text is None
    assert turn.started_at is not None
    session.add.assert_called_once_with(turn)
    session.flush.assert_awaited_once()


async def test_conversation_repo_append_turn_passes_through_explicit_fields() -> None:
    session = make_session()
    repo = ConversationRepo(session)
    started = datetime(2026, 7, 24, 8, 44, tzinfo=UTC)

    turn = await repo.append_turn(
        "sess_1",
        3,
        "agent",
        latency_ms=420,
        input_tokens=2000,
        output_tokens=120,
        tool_calls=["get_wallet_balance", "get_payment_status"],
        cost_usd=Decimal("0.0123"),
        trace_id="trace-abc",
        started_at=started,
    )

    assert turn.latency_ms == 420
    assert turn.tool_calls == ["get_wallet_balance", "get_payment_status"]
    assert turn.cost_usd == Decimal("0.0123")
    assert turn.trace_id == "trace-abc"
    assert turn.started_at == started


# --------------------------------------------------------------------------
# ToolAuditRepo
# --------------------------------------------------------------------------


async def test_tool_audit_repo_insert_builds_full_row() -> None:
    session = make_session()
    repo = ToolAuditRepo(session)

    invocation = await repo.insert(
        session_id="sess_1",
        turn_no=3,
        tool_name="get_wallet_balance",
        input={},
        output={"balance": 18450, "currency": "INR"},
        status="ok",
        latency_ms=12,
        idempotency_key="sess_1:get_wallet_balance:3",
    )

    assert isinstance(invocation, ToolInvocation)
    assert invocation.tool_name == "get_wallet_balance"
    assert invocation.input == {}
    assert invocation.output == {"balance": 18450, "currency": "INR"}
    assert invocation.status == "ok"
    assert invocation.idempotency_key == "sess_1:get_wallet_balance:3"
    session.add.assert_called_once_with(invocation)
    session.flush.assert_awaited_once()


async def test_tool_audit_repo_insert_error_row_carries_error_code() -> None:
    session = make_session()
    repo = ToolAuditRepo(session)

    invocation = await repo.insert(
        session_id="sess_1",
        tool_name="request_limit_increase",
        input={"current_limit": 25000, "requested_limit": 50000},
        status="error",
        latency_ms=5,
        error_code="STALE_LIMIT_VIEW",
    )

    assert invocation.status == "error"
    assert invocation.error_code == "STALE_LIMIT_VIEW"
    assert invocation.turn_no is None
    assert invocation.output is None


# --------------------------------------------------------------------------
# CostRepo
# --------------------------------------------------------------------------


def _cost_kwargs() -> dict[str, object]:
    return dict(
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


async def test_cost_repo_upsert_executes_insert_on_conflict_for_call_costs() -> None:
    session = make_session()
    repo = CostRepo(session)

    await repo.upsert("sess_1", **_cost_kwargs())

    session.execute.assert_awaited_once()
    stmt = session.execute.await_args.args[0]
    compiled = str(stmt.compile(dialect=postgresql.dialect()))
    assert "INSERT INTO call_costs" in compiled
    assert "ON CONFLICT (session_id) DO UPDATE SET" in compiled
    session.flush.assert_awaited_once()


async def test_cost_repo_upsert_defaults_turn_infra_usd_to_zero() -> None:
    session = make_session()
    repo = CostRepo(session)

    await repo.upsert("sess_1", **_cost_kwargs())

    stmt = session.execute.await_args.args[0]
    compiled_params = stmt.compile(dialect=postgresql.dialect())
    assert compiled_params.params["turn_infra_usd"] == Decimal("0")
    assert compiled_params.params["session_id"] == "sess_1"


# --------------------------------------------------------------------------
# Sanity: every repo only ever receives a pre-built AsyncSession — none
# constructs its own engine/session (docs/04 §4 DI split).
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "repo_cls",
    [MerchantRepo, WalletRepo, PaymentRepo, LimitRepo, ConversationRepo, ToolAuditRepo, CostRepo],
)
def test_repo_constructor_takes_only_a_session(repo_cls: type) -> None:
    session = make_session()
    repo = repo_cls(session)
    assert repo._session is session  # noqa: SLF001 — whitebox check of the DI contract
