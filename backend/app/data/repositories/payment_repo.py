"""PaymentRepo — repository for `transactions` + `merchant_limits`
(docs/04-backend-architecture.md §5), the aggregate `get_payment_status`
and the seeded `POST /v1/payments` handler read and write.

Two responsibilities live in one file rather than split across two repos
because both need the same join. `get_payment_status` reads
`transactions` and, for a limit-related decline, `merchant_limits`
alongside it (docs/10-tool-calling.md §3.1 — "a deliberate join... so the
LLM gets the explanation in one call instead of needing a get_limits
tool"). The daily-limit check that decides whether a payment gets
*created* as `declined` in the first place reads that same
`merchant_limits` row before the write (docs/12-data-models.md §3.2's own
phrasing of the decline logic: "`used_paise + amount_paise > limit_paise`
-> `402 DAILY_LIMIT_EXCEEDED`").

Money stays in integer paise throughout this file — the tool-handler
layer (a later batch) does the rupee conversion at the tool boundary
(docs/10 §3 preamble: "conversion to the database's paise happens inside
each handler"), never here.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone

from sqlalchemy import select

from app.data.repositories.base import SqlAlchemyRepository
from app.models import MerchantLimit, Transaction

# Fixed UTC+5:30 offset — India has no DST, so this never needs a tzdata
# lookup. `resets_at` is rendered in this offset directly, matching
# docs/13-api-contracts.md §3.2's worked example verbatim:
# window_date=2026-07-24 -> "resets_at": "2026-07-25T00:00:00+05:30".
_IST = timezone(timedelta(hours=5, minutes=30))

# The only decline code in Phase-2 scope whose explanation needs a
# merchant_limits join (docs/10 §3.1). INSUFFICIENT_BALANCE is also a
# real decline_code (docs/13 §1.1) but its explanation lives entirely in
# wallet_accounts.balance_paise — no merchant_limits join applies to it.
_LIMIT_RELATED_DECLINE_CODES: frozenset[str] = frozenset({"DAILY_LIMIT_EXCEEDED"})

_DAILY_LIMIT_TYPE = "daily_txn"


def _midnight_ist_after(window_date: date) -> datetime:
    """The daily limit's `used_paise` counter resets at the next
    midnight IST after `window_date` (docs/11-prompt-engineering.md:
    "resets at midnight IST"; docs/13 §3.2's example confirms
    window_date=2026-07-24 -> resets_at=2026-07-25T00:00:00+05:30)."""
    return datetime.combine(window_date + timedelta(days=1), time.min, tzinfo=_IST)


@dataclass(frozen=True)
class LimitContext:
    """`limit_context` from the `get_payment_status` output schema
    (docs/10 §3.1), paise-native — the tool-handler layer renames/
    converts these into the wire shape's rupee-integer `limit`/
    `used_today`/`resets_at` fields."""

    limit_paise: int
    used_today_paise: int
    resets_at: datetime


@dataclass(frozen=True)
class PaymentStatus:
    """Everything `get_payment_status` (docs/10 §3.1) needs to answer,
    before the tool-handler layer's rupee conversion and JSON-schema
    field renames."""

    payment_id: str
    status: str
    amount_paise: int
    counterparty: str
    decline_code: str | None
    limit_context: LimitContext | None


@dataclass(frozen=True)
class DailyLimitCheck:
    """Result of `check_daily_limit` — what the seeded `POST /v1/payments`
    handler (a later batch) needs to decide `succeeded` vs. `402
    DAILY_LIMIT_EXCEEDED` and, on the decline path, to populate the error
    body's `details` (docs/13 §3.2)."""

    exceeded: bool
    limit_paise: int
    used_paise: int
    resets_at: datetime


class PaymentRepo(SqlAlchemyRepository[Transaction]):
    model = Transaction

    async def create(
        self,
        *,
        txn_id: str,
        merchant_id: str,
        wallet_id: str,
        txn_type: str,
        amount_paise: int,
        counterparty: str,
        status: str,
        decline_code: str | None = None,
        http_status: int | None = None,
    ) -> Transaction:
        """Insert one `transactions` row (docs/12 §3.3). Used by the
        seed script (the canonical declined ₹245 txn) and, later, the
        `POST /v1/payments` handler for both the succeeded and declined
        paths. `status`/`decline_code`/`http_status` are the caller's
        decision — already made via `check_daily_limit` on the limit
        path — this method only persists it, matching
        `ck_transactions_decline_code_pair`'s pairing rule by
        construction whenever the caller respects it.
        """
        txn = Transaction(
            txn_id=txn_id,
            merchant_id=merchant_id,
            wallet_id=wallet_id,
            type=txn_type,
            amount_paise=amount_paise,
            counterparty=counterparty,
            status=status,
            decline_code=decline_code,
            http_status=http_status,
        )
        return await self.add(txn)

    async def check_daily_limit(
        self,
        merchant_id: str,
        amount_paise: int,
        *,
        limit_type: str = _DAILY_LIMIT_TYPE,
        with_for_update: bool = False,
    ) -> DailyLimitCheck | None:
        """`used_paise + amount_paise > limit_paise` — docs/12 §3.2's own
        phrasing of the decline logic, verbatim. Returns `None` if the
        merchant has no row for `limit_type`; callers decide what that
        means (Phase-2 seed data always has one `daily_txn` row per
        merchant, so this is a defensive path, not an expected one).

        `with_for_update` (added after review, defaults `False` — no
        caller in this diff writes `used_paise`, so nothing here needs
        locking yet): a future `POST /v1/payments` handler that pairs
        this check with an `used_paise` increment MUST pass `True` and do
        both inside one transaction, or two concurrent payments can both
        pass this check before either commits and jointly exceed the
        daily limit — the same read-then-write race
        `LimitRepo.submit_increase_request` was fixed for for this exact
        reason. Kept as an explicit opt-in here rather than always-on
        since this method also serves plain informational reads (e.g. a
        future `get_wallet_balance`-adjacent tool call) that have no
        reason to hold a row lock.
        """
        limit_row = await self._session.get(
            MerchantLimit, (merchant_id, limit_type), with_for_update=with_for_update
        )
        if limit_row is None:
            return None
        return DailyLimitCheck(
            exceeded=limit_row.used_paise + amount_paise > limit_row.limit_paise,
            limit_paise=limit_row.limit_paise,
            used_paise=limit_row.used_paise,
            resets_at=_midnight_ist_after(limit_row.window_date),
        )

    async def record_daily_usage(
        self, merchant_id: str, amount_paise: int, *, limit_type: str = _DAILY_LIMIT_TYPE
    ) -> None:
        """Increments `used_paise` for a payment whose caller has ALREADY
        confirmed, in this same transaction, that it (a) passes
        `check_daily_limit(..., with_for_update=True)` and (b) passes a
        wallet-balance check — added after review (CRITICAL: nothing
        anywhere incremented `used_paise`, so the daily-limit control was
        inert after the seeded state; docs/12 §3.2's own invariant is
        `SUM(successful payouts today) == used_paise`, which is exactly
        why this must not be called until a payment is confirmed to
        actually succeed, not merely attempted).

        Deliberately not folded into `check_daily_limit` itself: that
        method is a pure check other Phase-2 callers rely on staying
        read-only (`check_daily_limit`'s own tests, PR #7), and a payment
        can still fail the *wallet* balance check after passing the daily-
        limit one — incrementing here, only once both checks are known to
        have passed, is what keeps a declined-for-insufficient-balance
        payment from being charged against the daily limit it never used.

        No new lock is taken: `session.get()` on the same primary key
        within the same session/transaction returns the already-managed
        instance the caller's earlier `with_for_update=True` fetch
        loaded (SQLAlchemy's identity map), not a fresh unlocked read —
        so this issues no additional `SELECT` and can only ever mutate a
        row `check_daily_limit` already locked.
        """
        limit_row = await self._session.get(MerchantLimit, (merchant_id, limit_type))
        if limit_row is None:
            raise RuntimeError(
                f"record_daily_usage({merchant_id!r}, {limit_type!r}): no merchant_limits "
                "row found — the caller's own check_daily_limit call should already have "
                "found (or None-returned on) this row before reaching here"
            )
        limit_row.used_paise += amount_paise

    async def get_payment_status(self, txn_id: str, merchant_id: str) -> PaymentStatus | None:
        """docs/10 §3.1's `get_payment_status` tool, minus the rupee
        conversion the tool-handler layer applies. One query — a LEFT
        JOIN on `merchant_limits` scoped to the same merchant's
        `daily_txn` row — cheap even when the decline wasn't
        limit-related, and avoids a second round trip for the common
        case where it was.

        `merchant_id` is required, not optional (fixed after review):
        the `WHERE` clause is scoped to it, not just `txn_id` alone. Per
        docs/10 §1 invariant 3 and docs/13 §1.1's "`RESOURCE_NOT_FOUND` —
        unknown id, or id owned by another user (deliberately
        indistinguishable)", a wrong-owner `txn_id` must return `None`
        (-> `RESOURCE_NOT_FOUND` one layer up) exactly like an unknown
        one — not another merchant's payment amount, counterparty, and
        decline reason. Filtering here, where the FK actually lives,
        means the not-yet-built `ToolExecutor`/tool-handler layer can't
        forget to enforce this even if it tried to.
        """
        stmt = (
            select(Transaction, MerchantLimit)
            .outerjoin(
                MerchantLimit,
                (MerchantLimit.merchant_id == Transaction.merchant_id)
                & (MerchantLimit.limit_type == _DAILY_LIMIT_TYPE),
            )
            .where(Transaction.txn_id == txn_id, Transaction.merchant_id == merchant_id)
        )
        result = await self._session.execute(stmt)
        row = result.one_or_none()
        if row is None:
            return None
        txn, limit_row = row

        limit_context = None
        if txn.decline_code in _LIMIT_RELATED_DECLINE_CODES and limit_row is not None:
            limit_context = LimitContext(
                limit_paise=limit_row.limit_paise,
                used_today_paise=limit_row.used_paise,
                resets_at=_midnight_ist_after(limit_row.window_date),
            )

        return PaymentStatus(
            payment_id=txn.txn_id,
            status=txn.status,
            amount_paise=txn.amount_paise,
            counterparty=txn.counterparty,
            decline_code=txn.decline_code,
            limit_context=limit_context,
        )
