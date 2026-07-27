"""`get_payment_status` tool (docs/10-tool-calling.md §3, §3.1) — the
tool that resolves the wallet-vs-limit contradiction in the canonical
call's turn 3: one payment's status, plus a `merchant_limits` join when
the decline is limit-related. Fronts `GET /v1/payments/{id}`
(docs/13-api-contracts.md §3) at the tool boundary — per the Phase-2
plan's decision #4, by calling `PaymentRepo` directly, in-process.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.api.errors import ResourceNotFoundError
from app.data.repositories import PaymentRepo
from app.domain.types import SessionUser, Tier
from app.tools.registry import tool

_PAYMENT_ID_PATTERN = r"^txn_[a-z0-9_]+$"
# docs/12 §3.3: txn_id values are short, prefix+timestamp-derived strings
# (e.g. "txn_0724_1414a") — 64 chars is generous headroom, not a real
# observed length, added purely so this pattern-constrained field can't
# be handed an unbounded string (defensive; the pattern itself has no
# ReDoS risk, a simple non-backtracking character class).
_PAYMENT_ID_MAX_LENGTH = 64


class GetPaymentStatusIn(BaseModel):
    """docs/10 §3.1's exact input schema: one required, pattern-
    constrained field."""

    model_config = ConfigDict(extra="forbid")

    payment_id: str = Field(pattern=_PAYMENT_ID_PATTERN, max_length=_PAYMENT_ID_MAX_LENGTH)


class LimitContextOut(BaseModel):
    """docs/10 §3.1's `limit_context` sub-object — populated only for a
    limit-related decline. `PaymentRepo.get_payment_status` already does
    that filtering (its own LEFT JOIN plus a decline-code check); this
    model only carries the shape once the repo has decided it applies.
    """

    model_config = ConfigDict(frozen=True)

    limit: int
    used_today: int
    resets_at: str


class GetPaymentStatusOut(BaseModel):
    """docs/10 §3.1's exact output schema."""

    model_config = ConfigDict(frozen=True)

    payment_id: str
    status: Literal["succeeded", "declined", "pending", "refunded"]
    amount: int
    counterparty: str
    decline_code: str | None = None
    limit_context: LimitContextOut | None = None


def _paise_to_rupees(paise: int, *, field: str) -> int:
    """The tool boundary's one paise->rupee conversion (docs/10 §3
    preamble). Added after review (LOW): a bare `paise // 100` silently
    truncates and voices a WRONG amount if a non-whole-rupee value ever
    reaches this layer — exact only by assumption for Phase-2's seed
    data, not by construction. Fails loudly instead, so a future non-
    whole-rupee row surfaces as a clear error rather than a quietly
    wrong number read aloud to a merchant about their own money.
    """
    rupees, remainder = divmod(paise, 100)
    if remainder != 0:
        raise ValueError(
            f"{field}={paise} paise is not a whole number of rupees "
            "(Phase-2 assumes whole-rupee amounts everywhere at this boundary)"
        )
    return rupees


@tool(
    name="get_payment_status",
    tier=Tier.READ,
    latency_class="read-single",
    repo_type=PaymentRepo,
    description="One payment's status, including decline detail.",
)
async def get_payment_status(
    principal: SessionUser, args: GetPaymentStatusIn, repo: PaymentRepo
) -> GetPaymentStatusOut:
    """`principal.user_id` scopes the repo query, never `args` (invariant
    3, docs/10 §1). `PaymentRepo.get_payment_status` requires
    `merchant_id` precisely so a wrong-owner `payment_id` returns `None`
    here — indistinguishable from an unknown one (docs/13 §1.1: "id
    owned by another user, deliberately indistinguishable" — the fix
    that repo method's own docstring cites from PR #7's security
    review) — mapped to `ResourceNotFoundError` either way, never
    another merchant's payment data.

    `amount`/`limit`/`used_today` are each the tool boundary's one
    paise->rupee conversion (docs/10 §3 preamble) — integer division,
    exact for Phase-2's whole-rupee seed data.
    """
    status = await repo.get_payment_status(args.payment_id, merchant_id=principal.user_id)
    if status is None:
        raise ResourceNotFoundError("Unknown payment", details={"payment_id": args.payment_id})

    limit_context = None
    if status.limit_context is not None:
        limit_context = LimitContextOut(
            limit=_paise_to_rupees(status.limit_context.limit_paise, field="limit_paise"),
            used_today=_paise_to_rupees(
                status.limit_context.used_today_paise, field="used_today_paise"
            ),
            resets_at=status.limit_context.resets_at.isoformat(),
        )

    return GetPaymentStatusOut(
        payment_id=status.payment_id,
        status=status.status,  # type: ignore[arg-type]  # DB CHECK-constrained; Literal at the boundary
        amount=_paise_to_rupees(status.amount_paise, field="amount_paise"),
        counterparty=status.counterparty,
        decline_code=status.decline_code,
        limit_context=limit_context,
    )


__all__ = ["GetPaymentStatusIn", "GetPaymentStatusOut", "LimitContextOut", "get_payment_status"]
