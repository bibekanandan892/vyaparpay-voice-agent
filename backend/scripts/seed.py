"""Seeds the canonical VyaparPay demo fixtures (docs/01-product-and-use-
case.md §6-7, docs/12-data-models.md §8) — Rajesh Kumar's merchant row,
his wallet, the `merchant_limits` row that leaves exactly ₹110 of daily
headroom, and the declined ₹245 Amazon Business payment his call opens
with. Every worked example in the doc set (and the live CLI demo,
`scripts/demo_cli.py`, a later batch) depends on these figures being
exact and present before a session starts.

Idempotent by construction: each row is checked via the owning repo's
`get`/`get_by_key` before an insert is attempted, so running this twice
in a row (e.g. a demo rehearsal, or a flaky first run) neither fails nor
duplicates a row. `--reset` deletes the four seeded rows first, in
FK-safe child-to-parent order (transactions -> merchant_limits ->
wallet_accounts -> merchants), inside the same transaction as the
re-insert that follows — so a failed re-insert rolls the delete back too,
never leaving the fixture set half-erased.

Repos, not raw ORM inserts: this script goes through `MerchantRepo`/
`WalletRepo`/`LimitRepo`/`PaymentRepo` so seeding exercises the same
code path `ContextBuilder`/tool handlers use later, rather than a second,
divergent way of writing these tables. None of those repos expose a
`delete`, and only `PaymentRepo` exposes a `create` — and that `create`
has no `created_at` parameter, since every other caller is happy to let
the column's `server_default now()` own it (docs/12 §3.3). That's fine
for those callers, but not for this seed script: the canonical
transcript is pinned to 2026-07-24 14:14:00+05:30 (docs/01 §7 step 1), a
date already in the past relative to "now" on any day this script runs.
So the transaction row is built directly as a `Transaction(...)` and
persisted via the repo's inherited `add()` (from `SqlAlchemyRepository`)
instead of `PaymentRepo.create()` — still the repo's own write path,
just the lower-level one that leaves `created_at` in the caller's hands.
The merchant/wallet/limit rows use the same `get`-then-`add` shape for
the same reason: none of those tables' repos define a bespoke `create`
either.

This script is the one place in Phase 2 that owns a transaction outright
(app/data/repositories/base.py's docstring: repos "never call commit() or
rollback(); that boundary belongs to whatever opened the session") —
there is no request/response cycle here, so this script opens the one
`AsyncSession`, does the work, and commits it itself.
"""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.data import create_engine_and_sessionmaker
from app.data.repositories import LimitRepo, MerchantRepo, PaymentRepo, WalletRepo
from app.models import Merchant, MerchantLimit, Transaction, WalletAccount
from app.models.orm import CallCost, Conversation, ConversationTurn, ToolInvocation

# --------------------------------------------------------------------------
# Canonical fixture facts (docs/01 §6-7, docs/12 §8) — kept as named
# constants, not inlined, so the numbers this script promises to write are
# visible in one place and the doc citation travels with them.
# --------------------------------------------------------------------------

MERCHANT_ID = "usr_rajesh01"
BUSINESS_NAME = "Kumar General Store"
CITY = "Jaipur"
ACCOUNT_TYPE = "Merchant Pro"
PREFERRED_LANGUAGE = "English"
# docs/01 §6 only says "since 2022" — no day-level fact is ever voiced or
# asserted anywhere in the doc set, so Jan 1 is a plain stand-in, not a
# citation.
MERCHANT_SINCE = date(2022, 1, 1)

WALLET_ID = "wal_rajesh01"
WALLET_BALANCE_PAISE = 1_845_000  # ₹18,450 — the figure Asha quotes in turn 3

LIMIT_TYPE = "daily_txn"
DAILY_LIMIT_PAISE = 2_500_000  # ₹25,000 bank daily limit
DAILY_LIMIT_USED_PAISE = 2_489_000  # ₹24,890 already used -> ₹110 headroom
LIMIT_WINDOW_DATE = date(2026, 7, 24)

# Fixed UTC+5:30 offset, matching PaymentRepo's own `_IST` (India has no
# DST, so this never needs a tzdata lookup).
_IST = timezone(timedelta(hours=5, minutes=30))

TXN_ID = "txn_0724_1414a"
TXN_TYPE = "vendor_payment"
TXN_AMOUNT_PAISE = 24_500  # ₹245
TXN_COUNTERPARTY = "Amazon Business"
TXN_STATUS = "declined"
TXN_DECLINE_CODE = "DAILY_LIMIT_EXCEEDED"
TXN_HTTP_STATUS = 402
TXN_CREATED_AT = datetime(2026, 7, 24, 14, 14, 0, tzinfo=_IST)


@dataclass(frozen=True)
class SeedSummary:
    """What actually happened on this run — a demo presenter reads this
    off the terminal before a screen recording, so it must say plainly
    which rows were freshly written vs. already there from a prior run.
    """

    merchant_created: bool
    wallet_created: bool
    limit_created: bool
    transaction_created: bool

    def render(self) -> str:
        rows = [
            ("merchant", MERCHANT_ID, self.merchant_created),
            ("wallet", WALLET_ID, self.wallet_created),
            ("merchant_limits", f"{MERCHANT_ID}/{LIMIT_TYPE}", self.limit_created),
            ("transaction", TXN_ID, self.transaction_created),
        ]
        lines = ["VyaparPay demo fixtures (docs/12 §8):"]
        lines.extend(
            f"  [{_status(created):14s}] {label:16s} {key}" for label, key, created in rows
        )
        return "\n".join(lines)


def _status(created: bool) -> str:
    return "seeded" if created else "already present"


async def reset_seed_rows(session: AsyncSession) -> None:
    """Deletes the four seeded rows (only these, plus this merchant's
    agent-zone rows below — never a table-wide `TRUNCATE`, so this script
    stays safe to run against a database that has grown other data since
    seeding), FK-child before FK-parent.

    Agent-zone cascade (fixed after review — an earlier version of this
    function only cleared the business-zone rows): `conversations.user_id`
    references `merchants.merchant_id` with no `ON DELETE CASCADE`
    (app/models/orm.py), and `conversation_turns`/`tool_invocations`/
    `call_costs` each reference `conversations.session_id` the same way.
    Once any real call happens against `usr_rajesh01` — i.e. exactly the
    "demo rehearsal" scenario `--reset` exists for — a `Conversation` row
    exists, and deleting `merchants` without clearing these first raises
    an `IntegrityError`. None of `ConversationRepo`/`ToolAuditRepo`/
    `CostRepo` expose a `delete` (deliberately — `ToolAuditRepo`'s own
    docstring calls itself "insert-only from the tool path"), so these
    four deletes go through Core directly, scoped via a subquery on this
    merchant's `session_id`s rather than a second, easy-to-typo `merchant_id`
    filter repeated four times.
    """
    session_ids = select(Conversation.session_id).where(Conversation.user_id == MERCHANT_ID)
    await session.execute(delete(CallCost).where(CallCost.session_id.in_(session_ids)))
    await session.execute(delete(ToolInvocation).where(ToolInvocation.session_id.in_(session_ids)))
    await session.execute(
        delete(ConversationTurn).where(ConversationTurn.session_id.in_(session_ids))
    )
    await session.execute(delete(Conversation).where(Conversation.user_id == MERCHANT_ID))

    await session.execute(delete(Transaction).where(Transaction.txn_id == TXN_ID))
    await session.execute(
        delete(MerchantLimit).where(
            MerchantLimit.merchant_id == MERCHANT_ID, MerchantLimit.limit_type == LIMIT_TYPE
        )
    )
    await session.execute(delete(WalletAccount).where(WalletAccount.wallet_id == WALLET_ID))
    await session.execute(delete(Merchant).where(Merchant.merchant_id == MERCHANT_ID))
    await session.flush()


async def _seed_merchant(repo: MerchantRepo) -> bool:
    if await repo.get(MERCHANT_ID) is not None:
        return False
    await repo.add(
        Merchant(
            merchant_id=MERCHANT_ID,
            business_name=BUSINESS_NAME,
            city=CITY,
            account_type=ACCOUNT_TYPE,
            preferred_language=PREFERRED_LANGUAGE,
            merchant_since=MERCHANT_SINCE,
        )
    )
    return True


async def _seed_wallet(repo: WalletRepo) -> bool:
    if await repo.get(WALLET_ID) is not None:
        return False
    await repo.add(
        WalletAccount(
            wallet_id=WALLET_ID,
            merchant_id=MERCHANT_ID,
            balance_paise=WALLET_BALANCE_PAISE,
        )
    )
    return True


async def _seed_limit(repo: LimitRepo) -> bool:
    if await repo.get_by_key(MERCHANT_ID, LIMIT_TYPE) is not None:
        return False
    await repo.add(
        MerchantLimit(
            merchant_id=MERCHANT_ID,
            limit_type=LIMIT_TYPE,
            limit_paise=DAILY_LIMIT_PAISE,
            used_paise=DAILY_LIMIT_USED_PAISE,
            window_date=LIMIT_WINDOW_DATE,
        )
    )
    return True


async def _seed_transaction(repo: PaymentRepo) -> bool:
    if await repo.get(TXN_ID) is not None:
        return False
    # Not repo.create(): that method has no created_at parameter (see
    # module docstring) and this row's created_at is a frozen historical
    # fact, not "now".
    await repo.add(
        Transaction(
            txn_id=TXN_ID,
            merchant_id=MERCHANT_ID,
            wallet_id=WALLET_ID,
            type=TXN_TYPE,
            amount_paise=TXN_AMOUNT_PAISE,
            counterparty=TXN_COUNTERPARTY,
            status=TXN_STATUS,
            decline_code=TXN_DECLINE_CODE,
            http_status=TXN_HTTP_STATUS,
            created_at=TXN_CREATED_AT,
        )
    )
    return True


async def _seed_all(
    *,
    merchant_repo: MerchantRepo,
    wallet_repo: WalletRepo,
    limit_repo: LimitRepo,
    payment_repo: PaymentRepo,
    session: AsyncSession,
) -> SeedSummary:
    """The check-then-insert pass over all four rows, then the one commit
    this standalone script owns (app/data/repositories/base.py: repos
    never commit themselves)."""
    merchant_created = await _seed_merchant(merchant_repo)
    wallet_created = await _seed_wallet(wallet_repo)
    limit_created = await _seed_limit(limit_repo)
    transaction_created = await _seed_transaction(payment_repo)
    await session.commit()
    return SeedSummary(
        merchant_created=merchant_created,
        wallet_created=wallet_created,
        limit_created=limit_created,
        transaction_created=transaction_created,
    )


async def seed(session: AsyncSession, *, reset: bool = False) -> SeedSummary:
    """Entry point for callers that already hold an `AsyncSession`
    (this module's own CLI wiring below, and tests). `reset` and the
    subsequent inserts share this one session/transaction, so a failure
    partway through the insert pass rolls the reset's deletes back too.
    """
    if reset:
        await reset_seed_rows(session)
    return await _seed_all(
        merchant_repo=MerchantRepo(session),
        wallet_repo=WalletRepo(session),
        limit_repo=LimitRepo(session),
        payment_repo=PaymentRepo(session),
        session=session,
    )


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.seed",
        description="Seed the canonical VyaparPay demo fixtures (docs/12 §8).",
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help="Delete the seeded rows first (FK-safe order), then re-insert fresh.",
    )
    return parser.parse_args(argv)


_RESET_ALLOWED_ENVS = frozenset({"dev", "test"})


async def _amain(argv: Sequence[str] | None = None) -> None:
    args = _parse_args(argv)
    settings = get_settings()
    if args.reset and settings.env not in _RESET_ALLOWED_ENVS:
        # Defense-in-depth, not a real security boundary (fixed after
        # review): the deletes below are already tightly scoped to this
        # one merchant's rows, never a wildcard, so a misdirected DATABASE_URL
        # can't touch unrelated data. But there's otherwise zero rail against
        # --reset running against whatever DATABASE_URL happens to be in the
        # environment at invocation time — e.g. a copy-pasted prod connection
        # string in a local .env during a demo rehearsal. This is the one
        # place `settings.env` is actually load-bearing today.
        raise SystemExit(
            f"--reset refused: settings.env={settings.env!r} is not one of "
            f"{sorted(_RESET_ALLOWED_ENVS)} — this only runs against dev/test "
            "databases by design."
        )
    engine, sessionmaker = create_engine_and_sessionmaker(settings)
    try:
        async with sessionmaker() as session:
            summary = await seed(session, reset=args.reset)
        print(summary.render())
    finally:
        await engine.dispose()


def main(argv: Sequence[str] | None = None) -> None:
    asyncio.run(_amain(argv))


if __name__ == "__main__":
    main()
