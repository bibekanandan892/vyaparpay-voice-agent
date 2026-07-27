"""LimitRepo — repository for `merchant_limits`
(docs/04-backend-architecture.md §5), keyed on the composite primary key
`(merchant_id, limit_type)` (docs/12-data-models.md §3.2).

`submit_increase_request` is the one write in this file and the write the
canonical demo pivots on (docs/10-tool-calling.md §4, §6): it must set
`request_id`, `requested_paise`, `request_status`, and `requested_at`
together, never a subset. `ck_merchant_limits_request_pair`
(app/models/orm.py) rejects a partial write at the database level as a
last resort, but the point of this method is to never attempt one in the
first place.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy.exc import IntegrityError

from app.api.errors import (
    AppError,
    LimitRequestAlreadyPendingError,
    ResourceNotFoundError,
    StaleLimitViewError,
    ValidationSchemaError,
)
from app.data.repositories.base import SqlAlchemyRepository
from app.models import MerchantLimit

# docs/12 §3.2: "the seeded 4-business-hour auto-approve job is one
# UPDATE ... WHERE request_status = 'submitted' AND requested_at < now()
# - interval '4 hours'" — the same 4 hours request_limit_increase voices
# as eta_hours (docs/10 §3.1, §6 T7.2).
_LIMIT_INCREASE_SLA_HOURS = 4


@dataclass(frozen=True)
class LimitIncreaseRequest:
    """`request_limit_increase`'s output, minus the tool-handler layer
    (docs/10 §3.1: `{request_id, status, eta_hours}`)."""

    request_id: str
    status: str
    eta_hours: int


def _generate_request_id(now: datetime) -> str:
    """Matches `^LMT-\\d{4}-\\d{4}-\\d{4}$` (docs/10 §3.1) exactly: year,
    then month+day, then hour+minute — the same construction as the
    worked example `LMT-2026-0724-0913` (docs/10 §6 T7.2).

    Collision note: the pattern leaves no room for a disambiguator, so
    two submissions in the same UTC minute (necessarily from different
    merchants — one merchant can only ever have one pending request,
    enforced in `submit_increase_request` below) would collide on this
    id. The column's `UNIQUE` constraint turns that into a loud
    `IntegrityError` at flush rather than a silent overwrite. Acceptable
    for the Phase-2 single-merchant demo; a production evolution would
    need a wider id format.
    """
    return f"LMT-{now.year:04d}-{now.month:02d}{now.day:02d}-{now.hour:02d}{now.minute:02d}"


class LimitRepo(SqlAlchemyRepository[MerchantLimit]):
    model = MerchantLimit

    async def get_by_key(self, merchant_id: str, limit_type: str) -> MerchantLimit | None:
        """Composite-PK lookup, named distinctly from the inherited
        `get(id: str)` floor rather than overriding it (fixed after
        review: an override with a two-argument signature broke the
        `Repository[T]` Protocol `LimitRepo` is declared to satisfy —
        confirmed via mypy `[override]` and a Protocol-conformance check,
        not just by inspection. `Repository[T].get`'s single-`id` shape
        genuinely can't address a composite-PK row, so it stays
        inherited-but-inapplicable here; callers use this method instead.
        `add`/`update` are inherited unchanged since those operate on a
        whole entity instance, not a key."""
        return await self._session.get(MerchantLimit, (merchant_id, limit_type))

    async def submit_increase_request(
        self,
        merchant_id: str,
        limit_type: str,
        current_limit_paise: int,
        requested_limit_paise: int,
    ) -> LimitIncreaseRequest:
        """docs/10 §4's "Proposed" -> "Executing" transition (§6 T7.1):
        called only after the confirm gate has an affirmed proposal in
        hand (`ToolExecutor`'s job, a later batch) — this method does
        the actual `UPDATE` plus request_id minting, inside the caller's
        transaction.

        Raises `ResourceNotFoundError` when the merchant has no row for
        `limit_type`, `StaleLimitViewError` when `current_limit_paise`
        doesn't match the live row (docs/10 §3.1: "the handler rejects a
        mismatch... a cheap compare-and-swap on intent"),
        `LimitRequestAlreadyPendingError` when a request is already in
        flight (docs/10 §5's worked example), and `ValidationSchemaError`
        when `requested_limit_paise` wouldn't actually raise the limit.
        All are the same `AppError` subclasses the REST layer raises
        (docs/13-api-contracts.md §1: "one shared vocabulary, not a
        mapping table"), so the tool-handler layer (a later batch) can
        catch `AppError` uniformly and map it into a `ToolResult` error
        without a second exception hierarchy.

        Locking (fixed after review): the initial read uses
        `with_for_update=True`, not a plain `session.get`. Without it,
        two concurrent submissions for the same `(merchant_id,
        limit_type)` could both read `request_status IS NULL` before
        either commits — the second transaction's write would then
        silently overwrite the first's already-committed request instead
        of raising `LimitRequestAlreadyPendingError`, since nothing in a
        plain `UPDATE` re-checks that column. `with_for_update` makes the
        second transaction's read block behind the first's row lock
        until it commits, so the second call correctly observes the
        now-pending state.
        """
        limit_row = await self._session.get(
            MerchantLimit, (merchant_id, limit_type), with_for_update=True
        )
        if limit_row is None:
            raise ResourceNotFoundError(
                "No limit row for this merchant/limit_type",
                details={"merchant_id": merchant_id, "limit_type": limit_type},
            )
        if limit_row.limit_paise != current_limit_paise:
            raise StaleLimitViewError(
                "The limit has changed since it was last read",
                details={
                    "current_view_paise": current_limit_paise,
                    "actual_limit_paise": limit_row.limit_paise,
                },
            )
        # Only "submitted" blocks a new request — "approved"/"rejected"
        # are terminal (docs/12 §3.2's auto-approve job filters on
        # request_status = 'submitted'), so a resolved request must not
        # permanently block every future one for this merchant/limit_type
        # (fixed after review: the original check treated any non-NULL
        # status, including resolved ones, as still pending).
        if limit_row.request_status == "submitted":
            raise LimitRequestAlreadyPendingError(
                "A limit-increase request is already pending",
                details={
                    "existing_request_id": limit_row.request_id,
                    "status": limit_row.request_status,
                    "requested_at": (
                        limit_row.requested_at.isoformat() if limit_row.requested_at else None
                    ),
                },
            )
        if requested_limit_paise <= limit_row.limit_paise:
            # Checked here, not left to ck_merchant_limits_requested_paise
            # alone (fixed after review): the CHECK is the last resort,
            # not the first response — a violation must surface as a
            # typed, retryable-false validation error (docs/10 §5's
            # "errors are results, not exceptions"), never a raw
            # IntegrityError out of this method.
            raise ValidationSchemaError(
                "requested_limit_paise must exceed the current limit",
                details={
                    "requested_limit_paise": requested_limit_paise,
                    "limit_paise": limit_row.limit_paise,
                },
            )

        now = datetime.now(UTC)
        request_id = _generate_request_id(now)

        # All four fields together, in one assignment sequence before
        # the one flush — ck_merchant_limits_request_pair never sees a
        # partial state (docs/12 §3.2).
        limit_row.request_id = request_id
        limit_row.requested_paise = requested_limit_paise
        limit_row.request_status = "submitted"
        limit_row.requested_at = now
        try:
            await self._session.flush()
        except IntegrityError as exc:
            # The one remaining IntegrityError path: request_id's UNIQUE
            # constraint, on a same-UTC-minute collision between two
            # different merchants (_generate_request_id's own docstring
            # names this as the accepted Phase-2 risk). Still a real
            # caller-visible failure mode this method shouldn't hand back
            # as an unclassified 500 (fixed after review) — surfaced as a
            # typed, retryable error instead so a caller can safely retry
            # a turn later, in whatever minute no longer collides.
            raise AppError(
                "A limit-increase request with this id already exists; retry",
                retryable=True,
            ) from exc

        return LimitIncreaseRequest(
            request_id=request_id, status="submitted", eta_hours=_LIMIT_INCREASE_SLA_HOURS
        )
