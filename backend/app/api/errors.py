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


class ValidationUnsupportedVersionError(AppError):
    """docs/13 §1.1: a `screen_context` or event payload carrying an
    unknown schema version. Distinct from `VALIDATION_SCHEMA` on purpose —
    the body is well-formed, it just speaks a version this server does not
    know, which is a client-upgrade signal rather than a bug in the
    request."""

    status_code = 400
    code = "VALIDATION_UNSUPPORTED_VERSION"


class ResourceNotFoundError(AppError):
    status_code = 404
    code = "RESOURCE_NOT_FOUND"


class SessionNotFoundError(AppError):
    """docs/13 §1.1's `SESSION_NOT_FOUND`, the session-scoped sibling of
    `RESOURCE_NOT_FOUND`. Raised both for an unknown session id AND for a
    session owned by another merchant — "deliberately indistinguishable —
    existence is information" (docs/13 §1.1, §2.2). Callers must therefore
    never put anything ownership-revealing in `details`."""

    status_code = 404
    code = "SESSION_NOT_FOUND"


# docs/13 §2.3: the summary endpoint answers `Retry-After: 2` while the
# post-call pipeline is still running, and the client "polls once or twice".
SESSION_SUMMARY_RETRY_AFTER_S = 2


class SessionSummaryPendingError(AppError):
    """docs/13 §1.1/§2.3: `GET /v1/sessions/{id}/summary` before the
    post-call pipeline has landed. A 404 rather than a 202 because the
    summary resource genuinely does not exist yet; `retry_after` carries
    the documented 2 s so the route can set the header (see
    `app/api/routes/sessions.py` for why the route, not
    `ErrorEnvelopeMiddleware`, sets it today)."""

    status_code = 404
    code = "SESSION_SUMMARY_PENDING"

    def __init__(
        self,
        message: str,
        *,
        details: dict[str, Any] | None = None,
        retry_after: int = SESSION_SUMMARY_RETRY_AFTER_S,
    ) -> None:
        super().__init__(message, details=details, retryable=True)
        self.retry_after = retry_after


class SessionAlreadyEndedError(AppError):
    """docs/13 §1.1: an operation on a session in a terminal state. Note
    that `DELETE /v1/sessions/{id}` deliberately does NOT raise this —
    hang-up is idempotent by contract (docs/13 §2.2); this is for the
    operations that genuinely cannot proceed on a dead session, i.e.
    re-minting a signaling token for it."""

    status_code = 409
    code = "SESSION_ALREADY_ENDED"


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
