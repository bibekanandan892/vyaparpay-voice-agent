"""`GET /v1/payments/{id}` and `POST /v1/payments`
(docs/13-api-contracts.md §3.2).

`POST /v1/payments` is the seeded demo payment endpoint the plan's task
brief describes: it is the canonical 402 origin — the app's pay flow, not
a tool (docs/13 §3: "app only" fronts this endpoint; `get_payment_status`
is the tool that later *reads* what this write produced). Rate-limited
(`require_rate_limit`, app/api/deps.py) since it mutates state, per the
plan's decision #7.

Scope note (flagged in review, not fixed here — see
`app/api/routes/limits.py`'s identical note): no `Idempotency-Key`
header handling. A client retry mints a fresh `txn_id` via
`_generate_txn_id` rather than being deduped against an earlier
attempt's terminal result. Deferred as a real, larger feature, not
silently missed.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal
from uuid import uuid4

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db, get_principal, require_rate_limit
from app.api.errors import (
    DailyLimitExceededError,
    InsufficientBalanceError,
    ResourceNotFoundError,
    success_envelope,
)
from app.data.repositories import DailyLimitCheck, PaymentRepo, PaymentStatus, WalletRepo
from app.domain.types import SessionUser

router = APIRouter(prefix="/v1/payments", tags=["payments"])

# Transaction.type's CHECK constraint (app/models/orm.py) — validated
# here, at the REST boundary, so a bad value is a clean `400
# VALIDATION_SCHEMA` (docs/13 §1.1) instead of an unclassified
# IntegrityError surfacing as a 500 out of PaymentRepo.create().
_TxnType = Literal["vendor_payment", "qr_collection", "settlement_credit", "refund"]


class CreatePaymentRequest(BaseModel):
    """docs/13 §3.2's request body, verbatim field set. `source` and
    `client_ref` are accepted for wire-shape parity with the documented
    contract but are not persisted — `transactions` (app/models/orm.py)
    has no matching column for either; Phase 2's `transactions` table is
    the 8-table scope docs/17 §2.2 froze, and neither field has a
    business use yet within that scope.
    """

    type: _TxnType
    amount_paise: int = Field(gt=0)
    counterparty: str = Field(min_length=1)
    source: str = Field(min_length=1)
    client_ref: str = Field(min_length=1)


def _format_rupees(paise: int) -> str:
    """`"₹25,000"` from `2500000` paise — matches docs/13 §3.2's worked
    example formatting exactly (thousands separator, no decimal). Amounts
    in this demo are always whole rupees (every seeded/derived paise
    value is a multiple of 100), so floor division never truncates a
    fractional rupee.
    """
    return f"₹{paise // 100:,}"


def _generate_txn_id(now: datetime) -> str:
    """Not a schema-pinned format (unlike `LimitRepo`'s `LMT-\\d{4}-...`
    ids, which docs/10 §3.1 regexes against) — docs/13 §3.2's
    `"txn_0724_1414a"` is a worked example, not a contract. This keeps
    the same readable `txn_<mmdd>_<hhmm>` prefix and adds a short random
    suffix (rather than a single disambiguator letter) so two payments in
    the same minute can't collide on `transactions.txn_id`'s primary key.
    """
    return f"txn_{now:%m%d}_{now:%H%M}{uuid4().hex[:6]}"


def _payment_status_payload(status: PaymentStatus) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "payment_id": status.payment_id,
        "status": status.status,
        "amount_paise": status.amount_paise,
        "counterparty": status.counterparty,
        "decline_code": status.decline_code,
    }
    if status.limit_context is not None:
        payload["limit_context"] = {
            "limit_paise": status.limit_context.limit_paise,
            "used_today_paise": status.limit_context.used_today_paise,
            "resets_at": status.limit_context.resets_at.isoformat(),
        }
    return payload


def _daily_limit_exceeded_error(
    txn_id: str, attempted_paise: int, check: DailyLimitCheck
) -> DailyLimitExceededError:
    """docs/13 §3.2's exact worked-example `error` shape — same field
    names (`txn_id, attempted_paise, limit_paise, used_today_paise,
    resets_at`) and the same human-readable message template.
    """
    return DailyLimitExceededError(
        f"Daily transaction limit exceeded: limit {_format_rupees(check.limit_paise)}, "
        f"{_format_rupees(check.used_paise)} already used today.",
        details={
            "txn_id": txn_id,
            "attempted_paise": attempted_paise,
            "limit_paise": check.limit_paise,
            "used_today_paise": check.used_paise,
            "resets_at": check.resets_at.isoformat(),
        },
    )


def _insufficient_balance_error(
    txn_id: str, attempted_paise: int, balance_paise: int
) -> InsufficientBalanceError:
    """Added after review (CRITICAL): `INSUFFICIENT_BALANCE` is a real,
    documented decline code (docs/13 §1.1: "402 — Wallet balance below
    payment amount") that was never raised anywhere before this fix — a
    payment could be marked `succeeded` regardless of wallet balance.
    docs/13 has no worked JSON example for this code the way it does for
    `DAILY_LIMIT_EXCEEDED`; this mirrors that sibling's field-naming
    style (`txn_id`/`attempted_paise` shared verbatim, `balance_paise`
    the one field specific to this decline reason) for consistency
    rather than inventing an unrelated shape.
    """
    return InsufficientBalanceError(
        f"Wallet balance {_format_rupees(balance_paise)} is below the "
        f"payment amount {_format_rupees(attempted_paise)}.",
        details={
            "txn_id": txn_id,
            "attempted_paise": attempted_paise,
            "balance_paise": balance_paise,
        },
    )


@router.get("/{txn_id}")
async def get_payment(
    txn_id: str,
    principal: SessionUser = Depends(get_principal),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> dict[str, Any]:
    status = await PaymentRepo(db).get_payment_status(txn_id, principal.user_id)
    if status is None:
        raise ResourceNotFoundError(
            "No payment found for this id", details={"txn_id": txn_id}
        )
    return success_envelope(_payment_status_payload(status))


@router.post("", status_code=201, dependencies=[Depends(require_rate_limit)])
async def create_payment(
    body: CreatePaymentRequest,
    principal: SessionUser = Depends(get_principal),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> dict[str, Any]:
    wallet_repo = WalletRepo(db)
    payment_repo = PaymentRepo(db)

    wallet = await wallet_repo.get_by_merchant(principal.user_id)
    if wallet is None:
        raise ResourceNotFoundError(
            "No wallet found for this merchant",
            details={"merchant_id": principal.user_id},
        )

    # with_for_update=True per PaymentRepo.check_daily_limit's own
    # docstring warning: a handler that decides succeeded-vs-declined off
    # this read must lock the row, or two concurrent payments can both
    # read "under limit" before either commits.
    check = await payment_repo.check_daily_limit(
        principal.user_id, body.amount_paise, with_for_update=True
    )
    now = datetime.now(UTC)
    txn_id = _generate_txn_id(now)

    if check is not None and check.exceeded:
        txn = await payment_repo.create(
            txn_id=txn_id,
            merchant_id=principal.user_id,
            wallet_id=wallet.wallet_id,
            txn_type=body.type,
            amount_paise=body.amount_paise,
            counterparty=body.counterparty,
            status="declined",
            decline_code="DAILY_LIMIT_EXCEEDED",
            http_status=402,
        )
        # Explicit commit before raising: `get_db` (app/api/deps.py)
        # rolls back on any exception leaving this handler, which would
        # otherwise undo the declined transaction row this endpoint's
        # whole contract exists to persist (docs/13 §3.2: "the declined
        # transactions row... is what get_payment_status later reads").
        # A 402 decline is a real, deliberate business outcome here, not
        # a system failure — it must survive the exception that reports
        # it.
        await db.commit()
        raise _daily_limit_exceeded_error(txn.txn_id, body.amount_paise, check)

    # Added after review (CRITICAL): the daily-limit check passing is
    # NOT sufficient to mark a payment succeeded — the wallet must
    # actually hold enough balance. Locked (with_for_update=True) for
    # the identical TOCTOU reason the daily-limit check above is locked:
    # two concurrent payments must not both read "sufficient balance"
    # before either commits.
    debit = await wallet_repo.debit_if_sufficient(
        principal.user_id, body.amount_paise, with_for_update=True
    )
    if debit is not None and not debit.sufficient:
        txn = await payment_repo.create(
            txn_id=txn_id,
            merchant_id=principal.user_id,
            wallet_id=wallet.wallet_id,
            txn_type=body.type,
            amount_paise=body.amount_paise,
            counterparty=body.counterparty,
            status="declined",
            decline_code="INSUFFICIENT_BALANCE",
            http_status=402,
        )
        # Same reasoning as the daily-limit decline above: this decline
        # is a real business outcome and must survive past the raise.
        # Nothing here touched merchant_limits.used_paise (that only
        # ever happens below, once a payment is confirmed to succeed),
        # so this decline correctly leaves the daily-limit counter
        # untouched — the merchant used none of their daily limit for a
        # payment that never actually moved money.
        await db.commit()
        raise _insufficient_balance_error(txn.txn_id, body.amount_paise, debit.balance_paise)

    # Both checks passed — safe to charge the daily limit now, and only
    # now (docs/12 §3.2's invariant: SUM(successful payouts today) ==
    # used_paise, not "attempted"). No new lock: record_daily_usage
    # reuses the row check_daily_limit already locked above.
    if check is not None:
        await payment_repo.record_daily_usage(principal.user_id, body.amount_paise)

    txn = await payment_repo.create(
        txn_id=txn_id,
        merchant_id=principal.user_id,
        wallet_id=wallet.wallet_id,
        txn_type=body.type,
        amount_paise=body.amount_paise,
        counterparty=body.counterparty,
        status="succeeded",
        http_status=201,
    )
    return success_envelope(
        {
            "txn_id": txn.txn_id,
            "status": txn.status,
            "amount_paise": txn.amount_paise,
            "counterparty": txn.counterparty,
        }
    )


__all__ = ["router"]
