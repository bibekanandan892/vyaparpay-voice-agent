"""Unit tests for `app/tools/get_wallet_balance.py` (docs/10-tool-
calling.md §3). No Docker/Postgres available in this environment
(matches `backend/tests/data/repositories/test_repositories.py`'s own
note) — `WalletRepo` is mocked with `unittest.mock.AsyncMock(spec=
WalletRepo)`, never a real `AsyncSession`, since this handler is called
directly with a repo instance (the `@tool` decorator's job of actually
opening a session is exercised by wiring, not by these tests).
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from app.api.errors import ResourceNotFoundError
from app.data.repositories import WalletRepo
from app.domain.types import SessionUser
from app.models import WalletAccount
from app.tools import errors
from app.tools.get_wallet_balance import GetWalletBalanceIn, get_wallet_balance

_PRINCIPAL = SessionUser(user_id="usr_rajesh01", name="Rajesh Kumar")


async def test_get_wallet_balance_converts_paise_to_rupees() -> None:
    """docs/13 §3.1's own worked example: `balance_paise` 1_845_000 ->
    the canonical ₹18,450 Asha voices in turn 3."""
    wallet = WalletAccount(
        wallet_id="wal_rajesh01",
        merchant_id="usr_rajesh01",
        balance_paise=1_845_000,
        currency="INR",
        updated_at=datetime(2026, 7, 24, 14, 2, 11, tzinfo=UTC),
    )
    repo = AsyncMock(spec=WalletRepo)
    repo.get_by_merchant.return_value = wallet

    result = await get_wallet_balance(_PRINCIPAL, GetWalletBalanceIn(), repo)

    assert result.balance == 18_450
    assert result.currency == "INR"
    assert result.updated_at == "2026-07-24T14:02:11+00:00"
    repo.get_by_merchant.assert_awaited_once_with("usr_rajesh01")


async def test_get_wallet_balance_raises_resource_not_found_when_no_wallet() -> None:
    """Defensive path: Phase-2 seed data always gives a merchant a
    wallet, but the handler must not raise an unclassified error if it
    somehow didn't (docs/13 §1.1's RESOURCE_NOT_FOUND convention)."""
    repo = AsyncMock(spec=WalletRepo)
    repo.get_by_merchant.return_value = None

    with pytest.raises(ResourceNotFoundError):
        await get_wallet_balance(_PRINCIPAL, GetWalletBalanceIn(), repo)


def test_get_wallet_balance_input_has_no_principal_smuggling_fields() -> None:
    """Invariant 3 (docs/10 §1): the input schema has no field an LLM
    could use to pass a different merchant id -- the empty field set is
    the whole point of `get_wallet_balance` taking "literally nothing"
    (docs/10 §3)."""
    assert GetWalletBalanceIn.model_fields == {}


def test_get_wallet_balance_input_rejects_unexpected_fields_and_maps_to_wire_shape() -> None:
    """`extra="forbid"` is the schema-level backstop for invariant 3.
    Also exercises `app/tools/errors.py:validation_error()` against a
    real `pydantic.ValidationError` (that module has no dedicated test
    file in this task's scope) to confirm the wire shape docs/10 §5
    specifies."""
    with pytest.raises(ValidationError) as exc_info:
        GetWalletBalanceIn(user_id="usr_someone_else")  # type: ignore[call-arg]

    wire = errors.validation_error(exc_info.value)
    assert wire["type"] == "validation"
    assert wire["code"] == "SCHEMA_VALIDATION_FAILED"
    assert wire["retryable"] is True
    assert wire["fields"]
