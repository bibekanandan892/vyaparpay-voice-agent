"""`POST /v1/limits/increase-requests` (docs/13-api-contracts.md §3.4).

Thin wrapper over `LimitRepo.submit_increase_request`, which already
raises the correctly-typed `AppError` subclasses for every failure mode
(`ResourceNotFoundError`, `StaleLimitViewError`,
`LimitRequestAlreadyPendingError`, `ValidationSchemaError`, and a
retryable `AppError` on an `request_id` collision) — this route lets all
of them propagate to `ErrorEnvelopeMiddleware` unchanged rather than
catching and re-wrapping, per the plan's task brief ("let them propagate
to the error middleware").

Rate-limited (`require_rate_limit`) since it mutates state, per the
plan's decision #7 — same as `POST /v1/payments`.

Scope note (flagged in review, not fixed here): docs/13 §3 describes
`Idempotency-Key` header handling as a general convention for mutating
`POST`s across the business API. This route doesn't read or act on
that header — a client retry (e.g. after a network timeout) mints a
fresh request through `LimitRepo.submit_increase_request` rather than
being deduped against the original. `submit_increase_request`'s own
`LimitRequestAlreadyPendingError` happens to catch the most likely
real-world consequence (a retry while the first request is still
`submitted`), but that's an incidental side effect of a different
check, not actual idempotency-key support. Deferred as a real, larger
feature (parse the header, look up/replay a prior terminal result) —
out of this task's scope, not silently missed.
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db, get_principal, require_rate_limit
from app.api.errors import success_envelope
from app.data.repositories import LimitRepo
from app.domain.types import SessionUser
from app.tools.request_limit_increase import _MAX_LIMIT_RUPEES, _RUPEES_TO_PAISE

router = APIRouter(prefix="/v1/limits", tags=["limits"])

# MerchantLimit.limit_type's CHECK constraint (app/models/orm.py) —
# validated here so an unknown limit_type is a clean `400
# VALIDATION_SCHEMA` rather than reaching LimitRepo and surfacing as
# `ResourceNotFoundError` for a reason the client can't distinguish from
# "this merchant genuinely has no such row".
_LimitType = Literal["daily_txn", "per_txn"]

# Security review (HIGH): this REST route fronts the exact same
# `LimitRepo.submit_increase_request` mutation as the `request_limit_increase`
# tool (app/tools/request_limit_increase.py), which caps its own
# `requested_limit` at `_MAX_LIMIT_RUPEES` (₹1 crore) after an earlier
# review found an unbounded value had no ceiling anywhere in the pipeline.
# This route — the more exposed, directly network-callable path — never got
# the same fix. Reuse the tool's ceiling AND its rupees->paise conversion
# factor as the single source of truth (rather than duplicate magic numbers
# that could drift), since money is integer paise on REST (docs/13 §1) but
# the tool's constant is denominated in rupees.
_MAX_REQUESTED_LIMIT_PAISE = _MAX_LIMIT_RUPEES * _RUPEES_TO_PAISE


class IncreaseRequestBody(BaseModel):
    """docs/13 §3.4's request body, verbatim field set — paise, not
    rupees (docs/13 §1: "money is integer paise on REST")."""

    limit_type: _LimitType
    current_limit_paise: int = Field(gt=0)
    requested_limit_paise: int = Field(gt=0, le=_MAX_REQUESTED_LIMIT_PAISE)


@router.post("/increase-requests", status_code=201, dependencies=[Depends(require_rate_limit)])
async def create_increase_request(
    body: IncreaseRequestBody,
    principal: SessionUser = Depends(get_principal),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> dict[str, Any]:
    result = await LimitRepo(db).submit_increase_request(
        principal.user_id,
        body.limit_type,
        body.current_limit_paise,
        body.requested_limit_paise,
    )
    return success_envelope(
        {
            "request_id": result.request_id,
            "status": result.status,
            "eta_hours": result.eta_hours,
        }
    )


__all__ = ["router"]
