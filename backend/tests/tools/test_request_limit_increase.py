"""Unit tests for `app/tools/request_limit_increase.py` (docs/10-tool-
calling.md §3, §3.1, §4). Only the execution step itself -- the confirm
gate in front of it is `ToolExecutor`'s job (a later batch), per the
module's own docstring. Repo mocked with `unittest.mock.AsyncMock(spec=
LimitRepo)` -- no Docker/Postgres available.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from app.api.errors import (
    AppError,
    LimitRequestAlreadyPendingError,
    ResourceNotFoundError,
    StaleLimitViewError,
    ValidationSchemaError,
)
from app.data.repositories import LimitRepo
from app.data.repositories.limit_repo import LimitIncreaseRequest
from app.domain.types import SessionUser
from app.tools.request_limit_increase import RequestLimitIncreaseIn, request_limit_increase

_PRINCIPAL = SessionUser(user_id="usr_rajesh01", name="Rajesh Kumar")


async def test_request_limit_increase_converts_rupees_to_paise_and_submits() -> None:
    """docs/10 §6 T7.2's worked example: ₹25,000 -> ₹50,000, reference
    LMT-2026-0724-0913, 4-hour SLA."""
    repo = AsyncMock(spec=LimitRepo)
    repo.submit_increase_request.return_value = LimitIncreaseRequest(
        request_id="LMT-2026-0724-0913", status="submitted", eta_hours=4
    )

    result = await request_limit_increase(
        _PRINCIPAL,
        RequestLimitIncreaseIn(current_limit=25_000, requested_limit=50_000),
        repo,
    )

    assert result.request_id == "LMT-2026-0724-0913"
    assert result.status == "submitted"
    assert result.eta_hours == 4
    repo.submit_increase_request.assert_awaited_once_with(
        "usr_rajesh01", "daily_txn", 2_500_000, 5_000_000
    )


def test_request_limit_increase_input_rejects_sub_minimum_amounts() -> None:
    with pytest.raises(ValidationError):
        RequestLimitIncreaseIn(current_limit=0, requested_limit=50_000)


def test_request_limit_increase_input_rejects_unexpected_fields() -> None:
    with pytest.raises(ValidationError):
        RequestLimitIncreaseIn(
            current_limit=25_000,
            requested_limit=50_000,
            merchant_id="usr_someone_else",  # type: ignore[call-arg]
        )


@pytest.mark.parametrize(
    "app_error",
    [
        ResourceNotFoundError("no limit row"),
        StaleLimitViewError("stale"),
        LimitRequestAlreadyPendingError("already pending"),
        ValidationSchemaError("must exceed current limit"),
    ],
)
async def test_request_limit_increase_propagates_apperror_unmapped(app_error: AppError) -> None:
    """Task brief: the `AppError` subclasses `LimitRepo.
    submit_increase_request` raises propagate un-mapped out of this
    handler -- `ToolExecutor` (a later batch) is what turns them into a
    `ToolResult` via `app/tools/errors.py:business_error()`, not this
    handler re-mapping them under a second name."""
    repo = AsyncMock(spec=LimitRepo)
    repo.submit_increase_request.side_effect = app_error

    with pytest.raises(type(app_error)):
        await request_limit_increase(
            _PRINCIPAL,
            RequestLimitIncreaseIn(current_limit=25_000, requested_limit=50_000),
            repo,
        )
