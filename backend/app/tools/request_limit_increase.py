"""`request_limit_increase` tool (docs/10-tool-calling.md §3, §3.1) —
the canonical mutation, CONFIRM_REQUIRED tier. This module builds only
the execution step itself (docs/10 §4's "Executing" -> "Executed"
transition): the confirm-gate state machine and idempotency mechanics in
front of it belong to `ToolExecutor` (Phase-2 plan Batch 4, task 4.4 —
out of scope here). Gating hangs off `Tier.CONFIRM_REQUIRED` at
registration (docs/10 §8: "a new mutating tool cannot opt out of the
gate by forgetting it, because gating hangs off the tier, not the
handler"), not off anything in this file.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.data.repositories import LimitRepo
from app.domain.types import SessionUser, Tier
from app.tools.registry import tool

# docs/12 §3.2: merchant_limits.limit_type CHECK is IN ('daily_txn',
# 'per_txn'); docs/10 §3's request_limit_increase input has no
# limit_type field — the tool always targets the daily transaction
# limit, the one the canonical incident is about.
_DAILY_LIMIT_TYPE = "daily_txn"

_RUPEES_TO_PAISE = 100
_REQUEST_ID_PATTERN = r"^LMT-\d{4}-\d{4}-\d{4}$"
# Defensive sanity ceiling (added after review, security MEDIUM), not a
# business-tier rule — Merchant Pro's real eligibility ceiling (docs/10
# §3.1: "up to ₹50,000") is `LimitRepo`'s/business-rules' job to enforce,
# not this schema's. Submitted requests auto-approve via a scheduled job
# 4 hours later with no further human review (`LimitRepo`'s own comment),
# so an unbounded `requested_limit` had no upper bound anywhere in the
# whole pipeline — an LLM (or a confused user) could request an
# arbitrarily large daily limit and have it silently auto-approved. ₹1
# crore is generous enough to never bind on any legitimate request while
# still rejecting an obviously-wrong amount before it reaches the DB.
_MAX_LIMIT_RUPEES = 10_000_000


class RequestLimitIncreaseIn(BaseModel):
    """docs/10 §3.1's exact input schema. `current_limit` is required
    even though the server already knows it (that section's own
    explanation): the model must state the limit it *believes* it is
    raising, and `LimitRepo.submit_increase_request` rejects a mismatch
    with `StaleLimitViewError` — a cheap compare-and-swap on intent.
    """

    model_config = ConfigDict(extra="forbid")

    current_limit: int = Field(ge=1, le=_MAX_LIMIT_RUPEES)
    requested_limit: int = Field(ge=1, le=_MAX_LIMIT_RUPEES)


class RequestLimitIncreaseOut(BaseModel):
    """docs/10 §3.1's exact output schema. `request_id`'s pattern
    constraint is doubly true: it's both the wire contract *and* a live
    self-check that `LimitRepo._generate_request_id`'s format promise
    (`LMT-YYYY-MMDD-HHMM`) actually holds -- a drift there would fail
    output validation immediately instead of silently voicing a
    malformed reference number."""

    model_config = ConfigDict(frozen=True)

    request_id: str = Field(pattern=_REQUEST_ID_PATTERN)
    status: Literal["submitted"]
    eta_hours: int


@tool(
    name="request_limit_increase",
    tier=Tier.CONFIRM_REQUIRED,
    latency_class="write",
    repo_type=LimitRepo,
    description="Raise the daily bank transaction limit.",
)
async def request_limit_increase(
    principal: SessionUser, args: RequestLimitIncreaseIn, repo: LimitRepo
) -> RequestLimitIncreaseOut:
    """Rupees->paise, exactly once (docs/10 §3 preamble), then a direct
    pass-through to `LimitRepo.submit_increase_request`.
    `ResourceNotFoundError` / `StaleLimitViewError` /
    `LimitRequestAlreadyPendingError` / `ValidationSchemaError` all
    propagate un-mapped — they are already the wire-shared `AppError`
    vocabulary (docs/13 §1: "one shared vocabulary, not a mapping
    table"), so `ToolExecutor` (a later batch) catches `AppError` once,
    generically, at the pipeline's execute stage and renders it via
    `app/tools/errors.py:business_error()` rather than this handler
    re-wrapping the same information under a second name. This handler
    makes no business-logic decision of its own beyond the conversion
    below — `LimitRepo` owns every check (stale view, already-pending,
    non-increasing amount).
    """
    result = await repo.submit_increase_request(
        principal.user_id,
        _DAILY_LIMIT_TYPE,
        args.current_limit * _RUPEES_TO_PAISE,
        args.requested_limit * _RUPEES_TO_PAISE,
    )
    return RequestLimitIncreaseOut(
        request_id=result.request_id, status="submitted", eta_hours=result.eta_hours
    )


__all__ = ["RequestLimitIncreaseIn", "RequestLimitIncreaseOut", "request_limit_increase"]
