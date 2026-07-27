"""Unit tests for `app/tools/errors.py` (docs/10-tool-calling.md §5's
three wire shapes, plus `internal_error()` — added after review, see its
own docstring).

`validation_error()` is already exercised indirectly inside
`test_get_payment_status.py`/`test_get_wallet_balance.py` against a real
Pydantic `ValidationError`; `business_error()`/`timeout_error()`/
`internal_error()` had no direct coverage at all until this file (a
review LOW finding) — tested here in isolation rather than scattered
into unrelated tool test files, since none of the three docs/10 §5 shapes
is specific to any one tool.
"""

from __future__ import annotations

from app.api.errors import LimitRequestAlreadyPendingError
from app.tools import errors


def test_business_error_uses_the_apperror_code_and_details() -> None:
    """docs/10 §5's worked example: LIMIT_REQUEST_ALREADY_PENDING."""
    err = LimitRequestAlreadyPendingError(
        "A limit-increase request is already pending",
        details={"existing_request_id": "LMT-2026-0724-0913", "status": "submitted"},
    )

    wire = errors.business_error(err)

    assert wire == {
        "type": "business",
        "code": "LIMIT_REQUEST_ALREADY_PENDING",
        "detail": {"existing_request_id": "LMT-2026-0724-0913", "status": "submitted"},
        "retryable": False,
    }


def test_business_error_defaults_detail_to_empty_dict_not_none() -> None:
    """docs/10 §5: "business errors carry enough detail to be voiced" —
    detail is always a dict, never null, even when the AppError carries
    no details of its own."""
    err = LimitRequestAlreadyPendingError("pending")

    wire = errors.business_error(err)

    assert wire["detail"] == {}


def test_timeout_error_shape() -> None:
    wire = errors.timeout_error(2000)

    assert wire == {
        "type": "timeout",
        "code": "TOOL_TIMEOUT",
        "elapsed_ms": 2000,
        "retryable": True,
    }


def test_timeout_error_retryable_can_be_overridden() -> None:
    wire = errors.timeout_error(2000, retryable=False)

    assert wire["retryable"] is False


def test_internal_error_never_carries_the_real_exception_text() -> None:
    """The whole point of internal_error() (review MEDIUM): it takes no
    exception parameter at all, so it's structurally impossible for a
    caller to leak str(exc)/a traceback into the client/LLM-facing
    payload — ToolResult.error's frozen docstring requires "pre-vetted,
    user/LLM-safe content only, never raw exception text"."""
    wire = errors.internal_error()

    assert wire == {
        "type": "business",
        "code": "INTERNAL",
        "detail": {"message": "An internal error occurred"},
        "retryable": False,
    }
    # structural guarantee, not just a value check: the function takes no
    # arguments, so there is no call shape that could pass exception text in.
    import inspect

    assert inspect.signature(errors.internal_error).parameters == {}
