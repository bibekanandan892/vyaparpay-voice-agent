"""WalletRepo — repository for `wallet_accounts`
(docs/04-backend-architecture.md §5, docs/12-data-models.md §3.1).

docs/04 §5 maps this repo to `wallet_accounts, cards` — Phase 2's 8-table
scope (docs/17-roadmap.md §2.2) has no `cards` table, so no card methods
live here; `block_card`/`reset_pin` (sensitive-tier tools, docs/10-tool-
calling.md §3) stay out of scope until the table does.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select

from app.data.repositories.base import SqlAlchemyRepository
from app.models import WalletAccount


@dataclass(frozen=True)
class WalletDebitResult:
    """Result of `debit_if_sufficient` — added after review (CRITICAL:
    `POST /v1/payments`'s success path never actually moved money; a
    payment could be marked `succeeded` regardless of wallet balance,
    and `INSUFFICIENT_BALANCE` — a real, documented decline code,
    docs/13-api-contracts.md §1.1 — was never raised anywhere).
    `balance_paise` is the balance *before* this call, present on both
    outcomes so a decline path can report it without a second query."""

    sufficient: bool
    balance_paise: int


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

    async def debit_if_sufficient(
        self, merchant_id: str, amount_paise: int, *, with_for_update: bool = False
    ) -> WalletDebitResult | None:
        """Locks the wallet row (when `with_for_update=True`) and, only
        if `balance_paise >= amount_paise`, decrements it right away —
        staged for the caller's flush/commit, this repo never commits
        itself (`SqlAlchemyRepository`'s own rule). Returns `None` if the
        merchant has no wallet row (defensive — Phase-2 seed data always
        has exactly one, docs/12 §3.1's `UNIQUE` on `merchant_id`).

        Check-and-decrement in one call, not two, so there is no window
        between "checked sufficient" and "decremented" for a second
        concurrent debit to slip through — the exact TOCTOU class
        `PaymentRepo.check_daily_limit`'s `with_for_update` was fixed for
        (PR #7's review), applied here for the identical reason.
        `with_for_update` stays an explicit opt-in (default `False`)
        rather than always-on for the same reason it does on that sibling
        method: a plain balance read (e.g. a future informational check)
        has no reason to hold a row lock.
        """
        stmt = select(WalletAccount).where(WalletAccount.merchant_id == merchant_id)
        if with_for_update:
            stmt = stmt.with_for_update()
        result = await self._session.execute(stmt)
        wallet = result.scalar_one_or_none()
        if wallet is None:
            return None
        balance_before = wallet.balance_paise
        if balance_before < amount_paise:
            return WalletDebitResult(sufficient=False, balance_paise=balance_before)
        wallet.balance_paise = balance_before - amount_paise
        return WalletDebitResult(sufficient=True, balance_paise=balance_before)
