"""WalletRepo — repository for `wallet_accounts`
(docs/04-backend-architecture.md §5, docs/12-data-models.md §3.1).

docs/04 §5 maps this repo to `wallet_accounts, cards` — Phase 2's 8-table
scope (docs/17-roadmap.md §2.2) has no `cards` table, so no card methods
live here; `block_card`/`reset_pin` (sensitive-tier tools, docs/10-tool-
calling.md §3) stay out of scope until the table does.
"""

from __future__ import annotations

from sqlalchemy import select

from app.data.repositories.base import SqlAlchemyRepository
from app.models import WalletAccount


class WalletRepo(SqlAlchemyRepository[WalletAccount]):
    """`get(wallet_id)` is inherited (the `Repository[T]` floor), but
    both `get_wallet_balance` (docs/10 §3) and the `user_profile`
    context slot start from a `merchant_id` — nobody upstream carries a
    bare `wallet_id` around, since `WalletAccount.merchant_id` is the FK
    callers actually have (docs/12 §3.1) — hence `get_by_merchant`.
    """

    model = WalletAccount

    async def get_by_merchant(self, merchant_id: str) -> WalletAccount | None:
        stmt = select(WalletAccount).where(WalletAccount.merchant_id == merchant_id)
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()
