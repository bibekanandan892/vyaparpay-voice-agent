"""`GET /v1/wallet` (docs/13-api-contracts.md §3.1) — wraps
`WalletRepo.get_by_merchant`, the same repository method the (not yet
built) `get_wallet_balance` tool handler will call.

Deliberate omission: docs/13 §3.1's worked example includes a `"card":
{"last4": "4417", "status": "active"}` field, but Phase 2's 8-table scope
has no `cards` table (see `WalletRepo`'s own module docstring —
"Phase 2's 8-table scope... has no cards table, so no card methods live
here"). Rather than fabricate a card block from data that doesn't exist,
this response omits the field entirely; a client following docs/13 §9's
mandatory "ignore unknown/missing response fields" rule tolerates this
without a version bump.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db, get_principal
from app.api.errors import ResourceNotFoundError, success_envelope
from app.data.repositories import WalletRepo
from app.domain.types import SessionUser

router = APIRouter(prefix="/v1/wallet", tags=["wallet"])


@router.get("")
async def get_wallet(
    principal: SessionUser = Depends(get_principal),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> dict[str, Any]:
    wallet = await WalletRepo(db).get_by_merchant(principal.user_id)
    if wallet is None:
        raise ResourceNotFoundError(
            "No wallet found for this merchant",
            details={"merchant_id": principal.user_id},
        )
    return success_envelope(
        {
            "wallet_id": wallet.wallet_id,
            "balance_paise": wallet.balance_paise,
            "currency": wallet.currency,
            "updated_at": wallet.updated_at.isoformat(),
        }
    )


__all__ = ["router"]
