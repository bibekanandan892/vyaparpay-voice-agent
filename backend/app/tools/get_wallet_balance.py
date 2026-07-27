"""`get_wallet_balance` tool (docs/10-tool-calling.md §3): the merchant's
current wallet balance, read tier, no input. Fronts `GET /v1/wallet`
(docs/13-api-contracts.md §3.1) at the tool boundary — per the Phase-2
plan's decision #4, by calling `WalletRepo` directly, in-process, rather
than over HTTP.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from app.api.errors import ResourceNotFoundError
from app.data.repositories import WalletRepo
from app.domain.types import SessionUser, Tier
from app.tools.registry import tool


class GetWalletBalanceIn(BaseModel):
    """No input fields — docs/10 §3's own note: "`get_wallet_balance`
    takes literally nothing, the session already knows whose wallet."
    `extra="forbid"` still matters on an empty model: it is what makes
    `model_json_schema()` emit `"additionalProperties": false`, and it
    is the schema-level backstop for invariant 3 (docs/10 §1) — an
    LLM-supplied `user_id`/`merchant_id` argument is a validation
    rejection, not a silently-ignored extra key.
    """

    model_config = ConfigDict(extra="forbid")


class GetWalletBalanceOut(BaseModel):
    """docs/10 §3's compact output: `{balance, currency, updated_at}`.
    `balance` is integer rupees (docs/10 §3 preamble: "amounts at the
    tool boundary are integer rupees"); `updated_at` is rendered as an
    ISO-8601 string rather than a `datetime` so it round-trips through
    `model_dump_json()`/the eventual `ToolResult.data` dict unchanged.
    """

    model_config = ConfigDict(frozen=True)

    balance: int
    currency: str
    updated_at: str


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
    name="get_wallet_balance",
    tier=Tier.READ,
    latency_class="read-single",
    repo_type=WalletRepo,
    description="Current wallet balance.",
)
async def get_wallet_balance(
    principal: SessionUser, args: GetWalletBalanceIn, repo: WalletRepo
) -> GetWalletBalanceOut:
    """`principal.user_id` is the only scoping key — never anything from
    `args`, which has no fields to smuggle one through anyway (docs/10
    §1 invariant 3). `repo` is supplied by the `@tool` decorator
    (`app/tools/registry.py`), which wraps this exact 3-argument
    function into the 2-argument shape `app.domain.interfaces.
    ToolHandler` requires — see that module's docstring for why the
    split exists.

    `balance_paise -> rupees` is the tool boundary's one paise->rupee
    conversion (docs/10 §3 preamble) via `_paise_to_rupees` — exact, not
    lossy, for Phase-2's whole-rupee seed data; a fractional-paise
    balance would need `Decimal` instead, which Phase 2 deliberately
    doesn't have to reach for yet.
    """
    wallet = await repo.get_by_merchant(principal.user_id)
    if wallet is None:
        # Defensive, not expected: Phase-2 seed data gives every merchant
        # exactly one wallet_accounts row (docs/12 §3.1's UNIQUE on
        # merchant_id). Mirrors PaymentRepo.get_payment_status's own
        # "missing -> None -> RESOURCE_NOT_FOUND" convention (docs/13
        # §1.1) rather than raising an unclassified 500.
        raise ResourceNotFoundError(
            "No wallet found for this merchant", details={"merchant_id": principal.user_id}
        )
    return GetWalletBalanceOut(
        balance=_paise_to_rupees(wallet.balance_paise, field="balance_paise"),
        currency=wallet.currency,
        updated_at=wallet.updated_at.isoformat(),
    )


__all__ = ["GetWalletBalanceIn", "GetWalletBalanceOut", "get_wallet_balance"]
