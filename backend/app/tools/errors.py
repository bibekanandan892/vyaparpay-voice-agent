"""Tool-result error-shape builders — the three wire shapes docs/10-
tool-calling.md §5 specifies verbatim (validation / business / timeout),
each rendered as the *inner* `error` dict that goes into
`app.domain.types.ToolResult.error` (`ToolResult` itself is a frozen
pydantic model constructed by the caller, never raised — these functions
build the piece of it that `to_llm_message()` re-serializes as
`{"ok": false, "error": {...}}`).

None of these are called by the three Phase-2 tool handlers themselves:
per docs/10 §2's pipeline, schema validation happens *before* a handler
ever runs (so a handler never sees a raw Pydantic `ValidationError`),
and `AppError` subclasses a handler's repo call raises are left to
propagate un-caught (see `app/tools/request_limit_increase.py`'s
docstring) rather than being mapped here inline. These builders are the
shared mapping `ToolExecutor` (Phase-2 plan Batch 4, task 4.4 — out of
scope in this task) will call once, generically, at the point it catches
whatever a handler raised — not duplicated per tool.

`app/api/errors.py`'s module docstring is explicit that tool-layer and
REST-layer error codes are "one shared vocabulary, not a mapping table";
`business_error()` below is that reuse in code — it never invents a
second code string for something `AppError.code` already names.
"""

from __future__ import annotations

from typing import Any

from pydantic import ValidationError

from app.api.errors import AppError

_SCHEMA_VALIDATION_CODE = "SCHEMA_VALIDATION_FAILED"
_TIMEOUT_CODE = "TOOL_TIMEOUT"
_INTERNAL_CODE = "INTERNAL"
_INTERNAL_MESSAGE = "An internal error occurred"


def validation_error(exc: ValidationError, *, hint: str | None = None) -> dict[str, Any]:
    """docs/10 §5's "validation" shape (worked example: §6 T5.2).
    `hint` is the one field in that example written *for the model, not
    a human* ("amounts are integer rupees, e.g. 50000") — it has no
    general per-field source, so a caller that has one passes it and it
    is applied to every field in `exc` (Pydantic's own `loc`/`msg` are
    already field-scoped; a per-field hint map would be more machinery
    than any Phase-2 caller needs).
    """
    fields: list[dict[str, Any]] = []
    for error in exc.errors():
        field: dict[str, Any] = {
            "loc": ".".join(str(part) for part in error["loc"]),
            "msg": error["msg"],
        }
        if hint is not None:
            field["hint"] = hint
        fields.append(field)
    return {
        "type": "validation",
        "code": _SCHEMA_VALIDATION_CODE,
        "fields": fields,
        "retryable": True,
    }


def business_error(err: AppError) -> dict[str, Any]:
    """docs/10 §5's "business" shape (worked example:
    `LIMIT_REQUEST_ALREADY_PENDING`, §5's own JSON). `detail` (not
    `details`) is the wire key docs/10 §5 uses; the value is
    `AppError.details` verbatim, defaulting to `{}` rather than `None`
    so a business error always has *something* voiceable in `detail`
    (docs/10 §5: "business errors carry enough `detail` to *be
    voiced*")."""
    return {
        "type": "business",
        "code": err.code,
        "detail": err.details or {},
        "retryable": err.retryable,
    }


def timeout_error(elapsed_ms: int, *, retryable: bool = True) -> dict[str, Any]:
    """docs/10 §5's "timeout" shape — the 2 s execution-ceiling result
    (docs/10 §2's Execute-stage budget: `asyncio.wait_for(handler,
    timeout=2.0)`)."""
    return {
        "type": "timeout",
        "code": _TIMEOUT_CODE,
        "elapsed_ms": elapsed_ms,
        "retryable": retryable,
    }


def internal_error() -> dict[str, Any]:
    """The fourth wire shape, added after review (security MEDIUM): none
    of the three docs/10 §5 shapes above are meant to receive an
    unexpected, non-`AppError` exception — that path was previously
    entirely unhandled (whatever `ToolExecutor`, task 4.4, eventually
    catches with a bare `except Exception` would have no builder to call).
    Deliberately has NO parameter for the real exception, mirroring
    `app/api/errors.py`'s `InternalError`'s own "no free-form message"
    design: the fixed generic string below is the only thing that can
    ever reach `detail`/`fields`, so a future caller physically cannot do
    `internal_error(str(exc))` and violate `ToolResult.error`'s frozen
    "pre-vetted, user/LLM-safe content only, never raw exception text"
    docstring (`app/domain/types.py`) by construction. The real exception
    still gets logged — server-side, by whoever catches it — this
    function only ever builds the client/LLM-facing shape.
    """
    return {
        "type": "business",
        "code": _INTERNAL_CODE,
        "detail": {"message": _INTERNAL_MESSAGE},
        "retryable": False,
    }


__all__ = ["business_error", "internal_error", "timeout_error", "validation_error"]
