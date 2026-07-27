"""Unit tests for `app/tools/get_payment_status.py` (docs/10-tool-
calling.md §3, §3.1). Repo mocked with `unittest.mock.AsyncMock(spec=
PaymentRepo)` -- no Docker/Postgres available, same pattern as
`backend/tests/data/repositories/test_repositories.py`.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from app.api.errors import ResourceNotFoundError
from app.data.repositories import PaymentRepo
from app.data.repositories.payment_repo import LimitContext, PaymentStatus
from app.domain.types import SessionUser
from app.tools import errors
from app.tools.get_payment_status import GetPaymentStatusIn, get_payment_status

_PRINCIPAL = SessionUser(user_id="usr_rajesh01", name="Rajesh Kumar")
_IST = timezone(timedelta(hours=5, minutes=30))


async def test_get_payment_status_declined_with_limit_context() -> None:
    """docs/10 §3.1's worked example: ₹25,000 limit, ₹24,890 used --
    turn 3 of the canonical call."""
    status = PaymentStatus(
        payment_id="txn_0724_1414a",
        status="declined",
        amount_paise=24_500,
        counterparty="Amazon Business",
        decline_code="DAILY_LIMIT_EXCEEDED",
        limit_context=LimitContext(
            limit_paise=2_500_000,
            used_today_paise=2_489_000,
            resets_at=datetime(2026, 7, 25, 0, 0, tzinfo=_IST),
        ),
    )
    repo = AsyncMock(spec=PaymentRepo)
    repo.get_payment_status.return_value = status

    result = await get_payment_status(
        _PRINCIPAL, GetPaymentStatusIn(payment_id="txn_0724_1414a"), repo
    )

    assert result.payment_id == "txn_0724_1414a"
    assert result.status == "declined"
    assert result.amount == 245
    assert result.counterparty == "Amazon Business"
    assert result.decline_code == "DAILY_LIMIT_EXCEEDED"
    assert result.limit_context is not None
    assert result.limit_context.limit == 25_000
    assert result.limit_context.used_today == 24_890
    assert result.limit_context.resets_at == "2026-07-25T00:00:00+05:30"
    repo.get_payment_status.assert_awaited_once_with("txn_0724_1414a", merchant_id="usr_rajesh01")


async def test_get_payment_status_succeeded_has_no_limit_context() -> None:
    status = PaymentStatus(
        payment_id="txn_ok",
        status="succeeded",
        amount_paise=9_500,
        counterparty="Amazon Business",
        decline_code=None,
        limit_context=None,
    )
    repo = AsyncMock(spec=PaymentRepo)
    repo.get_payment_status.return_value = status

    result = await get_payment_status(_PRINCIPAL, GetPaymentStatusIn(payment_id="txn_ok"), repo)

    assert result.status == "succeeded"
    assert result.decline_code is None
    assert result.limit_context is None


async def test_get_payment_status_unknown_or_wrong_owner_raises_resource_not_found() -> None:
    """docs/13 §1.1: unknown id and wrong-owner id are deliberately
    indistinguishable -- both are a `None` repo return (the repo's own
    `merchant_id`-scoped WHERE clause is what makes this true), and both
    become `ResourceNotFoundError` here, never another merchant's
    payment data."""
    repo = AsyncMock(spec=PaymentRepo)
    repo.get_payment_status.return_value = None

    with pytest.raises(ResourceNotFoundError):
        await get_payment_status(_PRINCIPAL, GetPaymentStatusIn(payment_id="txn_missing"), repo)


def test_get_payment_status_input_rejects_malformed_payment_id() -> None:
    """docs/10 §3.1's exact pattern: `^txn_[a-z0-9_]+$`."""
    with pytest.raises(ValidationError) as exc_info:
        GetPaymentStatusIn(payment_id="not-a-valid-id")

    wire = errors.validation_error(exc_info.value, hint="payment_id must look like txn_...")
    assert wire["type"] == "validation"
    assert wire["code"] == "SCHEMA_VALIDATION_FAILED"
    assert wire["fields"][0]["loc"] == "payment_id"
    assert wire["fields"][0]["hint"] == "payment_id must look like txn_..."


def test_get_payment_status_input_rejects_unexpected_fields() -> None:
    with pytest.raises(ValidationError):
        GetPaymentStatusIn(payment_id="txn_ok", merchant_id="usr_someone_else")  # type: ignore[call-arg]
