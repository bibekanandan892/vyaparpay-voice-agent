"""Request-scope FastAPI dependencies (docs/04-backend-architecture.md §4).

`get_db`/`get_llm` are the request-scope accessors docs/04 §4's inline
lifespan sketch shows verbatim. `get_principal` deliberately does **not**
match that sketch's `Depends(bearer)`-based verifier — per this batch's
brief, JWT verification happens exactly once, in `AuthMiddleware`
(app/api/middleware.py), before any route dependency runs, so every route
uniformly gets `request.state.principal` set (or a 401 short-circuit
response) ahead of its own body (docs/04 §4.1's 4-stage middleware order).
`get_principal` here is the thin accessor a route handler's signature
actually calls; `verify_demo_jwt` is the decode/verify logic
`AuthMiddleware` calls, kept in this module (not inlined into
middleware.py) so it is independently unit-testable
(tests/api/test_deps.py) without spinning up the ASGI stack.

`require_rate_limit` is the route dependency form of docs/04 §6's
`enforce_rate` — applied only to the two mutating endpoints (`POST
/v1/payments`, `POST /v1/limits/increase-requests`, per the Phase-2 plan's
decision #7: Phase 2 has no `POST /v1/sessions` to attach
`RATE_LIMIT_SESSIONS_PER_MIN` to instead). It depends on `get_principal`
so it always runs *after* auth, keying the window on the verified `sub`
claim, never a spoofable body field (docs/04 §4.1). The limit figure
itself is read from `Settings.rate_limit_sessions_per_min` at call time
(fixed after review, LOW: an earlier version hardcoded the same number
as a bare module constant, so changing the env var would silently stop
affecting this surface even though docs/04 §6 frames session creation
and the mutating business endpoints as sharing one budget) — reusing the
field object, not just its current value, keeps the two surfaces
actually coupled to one number.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import jwt
from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.errors import AuthExpiredTokenError, AuthInvalidTokenError
from app.config import Settings
from app.data.redis_client import enforce_rate
from app.domain.interfaces import LLMProvider
from app.domain.types import SessionUser


def verify_demo_jwt(token: str, settings: Settings) -> SessionUser:
    """Decode + verify an HS256 demo JWT (docs/13 §1.2), trusting its
    `sub` claim as the merchant_id principal.

    Phase 2 has no `POST /v1/sessions` endpoint to mint real session
    tokens (that's Phase 3, out of scope here) — this is deliberately a
    thin DEMO verifier: signature + expiry only, no issuer allowlist, no
    revocation, no refresh. `options={"require": ["exp"]}` rejects a
    token with no expiry outright rather than silently treating "no exp
    claim" as "never expires".
    """
    try:
        payload = jwt.decode(
            token,
            settings.jwt_secret.get_secret_value(),
            algorithms=["HS256"],
            options={"require": ["exp"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise AuthExpiredTokenError("JWT has expired") from exc
    except jwt.InvalidTokenError as exc:
        raise AuthInvalidTokenError("JWT signature or claims are invalid") from exc

    sub = payload.get("sub")
    if not sub:
        raise AuthInvalidTokenError("JWT is missing the required 'sub' claim")
    return SessionUser(user_id=sub, name=payload.get("name"))


async def get_db(request: Request) -> AsyncIterator[AsyncSession]:
    """One `AsyncSession` per request — commit on clean exit, rollback on
    exception (docs/04 §5: "one AsyncSession = one transaction", opened
    by `get_db` per request and committed/rolled back here).

    A route that needs a business "failure" (e.g. a declined payment) to
    survive past a raised `AppError` — which this generic rollback would
    otherwise undo — commits that write explicitly before raising; see
    `app/api/routes/payments.py`'s `create_payment` for the one Phase-2
    case that applies to.
    """
    sessionmaker = request.app.state.sessionmaker
    async with sessionmaker() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
        else:
            await session.commit()


def get_llm(request: Request) -> LLMProvider:
    """The provider singleton `app/main.py`'s lifespan built once and
    stored on `app.state.llm` (docs/04 §4)."""
    return request.app.state.llm


def get_principal(request: Request) -> SessionUser:
    """Reads the `SessionUser` `AuthMiddleware` already attached to
    `request.state.principal` before routing reached this dependency.
    `AuthMiddleware`, not this function, does the actual JWT
    verification — a route wired to this dependency without
    `AuthMiddleware` ever running is a startup/wiring bug, not a
    client-facing 401, so this deliberately does not re-verify or
    default anything.
    """
    return request.state.principal


async def require_rate_limit(
    request: Request, principal: SessionUser = Depends(get_principal)  # noqa: B008
) -> None:
    """Route dependency (not middleware — docs/04 §4.1: "the rate limiter
    is a route dependency... because it applies to a named subset"),
    wired into the two mutating endpoints' `dependencies=[...]`. Depending
    on `get_principal` (rather than reading `request.state.principal`
    directly) makes the after-auth ordering an explicit part of this
    function's own signature, not just an artifact of routing order.
    """
    await enforce_rate(
        request.app.state.redis.raw,
        principal.user_id,
        limit=request.app.state.settings.rate_limit_sessions_per_min,
    )


__all__ = [
    "get_db",
    "get_llm",
    "get_principal",
    "require_rate_limit",
    "verify_demo_jwt",
]
