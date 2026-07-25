"""AppError hierarchy + the {success,data,error,meta} envelope (docs/13 §1).

`ErrorEnvelopeMiddleware` (app/api/middleware.py, task 3.3) catches
AppError subclasses and calls error_envelope() to build the response body;
route handlers and tool handlers alike raise these directly rather than
returning ad-hoc error dicts. Tool-layer and REST-layer error codes are
deliberately the *same strings* (docs/13 §1: "one shared vocabulary, not a
mapping table") — app/tools/errors.py (task 3.2) reuses these classes.
"""

from __future__ import annotations

from typing import Any


class AppError(Exception):
    """Base for every typed application error. `code` is the stable
    machine string shared between the REST error body and tool-result
    error payloads; `message` is human-readable/snackbar-safe."""

    status_code: int = 500
    code: str = "INTERNAL"

    def __init__(
        self,
        message: str,
        *,
        details: dict[str, Any] | None = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.details = details
        self.retryable = retryable


class ValidationSchemaError(AppError):
    status_code = 400
    code = "VALIDATION_SCHEMA"


class AuthMissingTokenError(AppError):
    status_code = 401
    code = "AUTH_MISSING_TOKEN"


class AuthInvalidTokenError(AppError):
    status_code = 401
    code = "AUTH_INVALID_TOKEN"


class AuthExpiredTokenError(AppError):
    status_code = 401
    code = "AUTH_EXPIRED_TOKEN"


class DailyLimitExceededError(AppError):
    status_code = 402
    code = "DAILY_LIMIT_EXCEEDED"


class InsufficientBalanceError(AppError):
    status_code = 402
    code = "INSUFFICIENT_BALANCE"


class ResourceNotFoundError(AppError):
    status_code = 404
    code = "RESOURCE_NOT_FOUND"


class LimitRequestAlreadyPendingError(AppError):
    status_code = 409
    code = "LIMIT_REQUEST_ALREADY_PENDING"


class StaleLimitViewError(AppError):
    status_code = 409
    code = "STALE_LIMIT_VIEW"


class RateLimitedError(AppError):
    status_code = 429
    code = "RATE_LIMITED"

    def __init__(self, retry_after: int) -> None:
        super().__init__(
            f"Rate limit exceeded, retry after {retry_after}s",
            details={"retry_after": retry_after},
            retryable=True,
        )
        self.retry_after = retry_after


class InternalError(AppError):
    """500 for unexpected/unclassified failures. Deliberately has NO
    free-form `message` parameter, unlike its siblings: the client-facing
    text is always the fixed generic string below. Log the real exception
    server-side (structlog, with the traceback) where it's raised — never
    pass `str(exc)` or a traceback into `details`, which is serialized
    verbatim into the HTTP response body."""

    status_code = 500
    code = "INTERNAL"

    def __init__(self, *, details: dict[str, Any] | None = None) -> None:
        super().__init__("An internal error occurred", details=details, retryable=False)


def error_envelope(err: AppError) -> dict[str, Any]:
    """The `{success:false,...}` body per docs/13 §1. `error.details` is
    optional per the docs/13 schema, so it's omitted entirely when unset
    rather than emitted as an explicit `null`."""
    error: dict[str, Any] = {"code": err.code, "message": err.message}
    if err.details is not None:
        error["details"] = err.details
    return {"success": False, "data": None, "error": error, "meta": None}


def success_envelope(data: Any, *, meta: dict[str, Any] | None = None) -> dict[str, Any]:
    """The `{success:true,...}` body per docs/13 §1."""
    return {"success": True, "data": data, "error": None, "meta": meta}
