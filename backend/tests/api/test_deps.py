"""Unit tests for app/api/deps.py — pure in-process, no ASGI stack, no DB,
no Redis.

`get_db`/`require_rate_limit` are exercised against small hand-rolled
doubles (a spy `AsyncSession`, a fake Redis pipeline) rather than a real
Starlette `Request`: both functions only ever touch `request.app.state.*`
and `request.state.*`, so a `types.SimpleNamespace` tree with the same
shape is sufficient and keeps these tests hermetic (no Docker/Postgres/
Redis needed, per this task's brief).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import jwt
import pytest

from app.api.deps import get_db, get_llm, get_principal, require_rate_limit, verify_demo_jwt
from app.api.errors import AuthExpiredTokenError, AuthInvalidTokenError, RateLimitedError
from app.config import Settings
from app.domain.types import SessionUser

# --------------------------------------------------------------------------
# verify_demo_jwt (docs/13 §1.2)
# --------------------------------------------------------------------------


def _settings(**overrides: object) -> Settings:
    defaults: dict[str, object] = {
        "jwt_secret": "test-jwt-secret",
        "database_url": "postgresql+asyncpg://test:test@localhost/test",
        "openrouter_api_key": "test-openrouter-key",
    }
    defaults.update(overrides)
    return Settings(**defaults)  # type: ignore[arg-type]


def _make_token(secret: str, **claims: object) -> str:
    payload: dict[str, object] = {
        "sub": "usr_rajesh01",
        "name": "Rajesh Kumar",
        "iss": "vyaparpay-demo",
        "iat": datetime.now(UTC),
        "exp": datetime.now(UTC) + timedelta(hours=24),
    }
    payload.update(claims)
    return jwt.encode(payload, secret, algorithm="HS256")


def test_verify_demo_jwt_returns_session_user_for_a_valid_token() -> None:
    settings = _settings()
    token = _make_token(settings.jwt_secret.get_secret_value())

    principal = verify_demo_jwt(token, settings)

    assert principal == SessionUser(user_id="usr_rajesh01", name="Rajesh Kumar")


def test_verify_demo_jwt_rejects_a_bad_signature() -> None:
    settings = _settings()
    token = _make_token("a-completely-different-secret")

    with pytest.raises(AuthInvalidTokenError):
        verify_demo_jwt(token, settings)


def test_verify_demo_jwt_rejects_an_expired_token() -> None:
    settings = _settings()
    token = _make_token(
        settings.jwt_secret.get_secret_value(),
        iat=datetime.now(UTC) - timedelta(hours=48),
        exp=datetime.now(UTC) - timedelta(hours=24),
    )

    with pytest.raises(AuthExpiredTokenError):
        verify_demo_jwt(token, settings)


def test_verify_demo_jwt_rejects_a_token_missing_the_sub_claim() -> None:
    settings = _settings()
    # jwt.encode requires *some* payload; build one directly rather than
    # via _make_token (which always sets sub) so `sub` is genuinely absent.
    token = jwt.encode(
        {"name": "Nobody", "exp": datetime.now(UTC) + timedelta(hours=1)},
        settings.jwt_secret.get_secret_value(),
        algorithm="HS256",
    )

    with pytest.raises(AuthInvalidTokenError):
        verify_demo_jwt(token, settings)


def test_verify_demo_jwt_rejects_a_token_with_no_expiry() -> None:
    """`options={"require": ["exp"]}` — a token that never expires is
    rejected outright rather than silently trusted forever."""
    settings = _settings()
    token = jwt.encode(
        {"sub": "usr_rajesh01"}, settings.jwt_secret.get_secret_value(), algorithm="HS256"
    )

    with pytest.raises(AuthInvalidTokenError):
        verify_demo_jwt(token, settings)


# --------------------------------------------------------------------------
# get_db — one AsyncSession per request, commit-on-clean-exit /
# rollback-on-exception (docs/04 §5)
# --------------------------------------------------------------------------


class _SpySession:
    """Stands in for `AsyncSession` in `async with sessionmaker() as
    session:` — just enough surface (`__aenter__`/`__aexit__`/`commit`/
    `rollback`) for get_db's own try/except/else to exercise, with the
    outcome recorded for assertions."""

    def __init__(self) -> None:
        self.committed = False
        self.rolled_back = False

    async def commit(self) -> None:
        self.committed = True

    async def rollback(self) -> None:
        self.rolled_back = True

    async def __aenter__(self) -> _SpySession:
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> bool:
        return False


def _make_request(**app_state: object) -> Any:
    return SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(**app_state)), state=SimpleNamespace()
    )


async def test_get_db_commits_on_clean_exit() -> None:
    spy = _SpySession()
    request = _make_request(sessionmaker=lambda: spy)

    gen = get_db(request)
    session = await gen.__anext__()
    assert session is spy

    with pytest.raises(StopAsyncIteration):
        await gen.__anext__()

    assert spy.committed is True
    assert spy.rolled_back is False


async def test_get_db_rolls_back_and_reraises_on_exception() -> None:
    spy = _SpySession()
    request = _make_request(sessionmaker=lambda: spy)

    gen = get_db(request)
    await gen.__anext__()

    with pytest.raises(ValueError, match="boom"):
        # `get_db`'s return type is annotated `AsyncIterator[AsyncSession]`
        # (the Protocol FastAPI's `Depends` needs); the concrete object it
        # returns is an async generator, which also supports `athrow` —
        # `AsyncIterator` alone doesn't promise that to the type checker.
        await gen.athrow(ValueError("boom"))  # type: ignore[attr-defined]

    assert spy.rolled_back is True
    assert spy.committed is False


# --------------------------------------------------------------------------
# get_llm / get_principal — thin app.state / request.state accessors
# --------------------------------------------------------------------------


def test_get_llm_returns_the_app_state_singleton() -> None:
    sentinel_llm = object()
    request = _make_request(llm=sentinel_llm)

    assert get_llm(request) is sentinel_llm


def test_get_principal_returns_the_request_state_principal() -> None:
    principal = SessionUser(user_id="usr_rajesh01")
    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace()),
        state=SimpleNamespace(principal=principal),
    )

    assert get_principal(request) is principal  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# require_rate_limit — route dependency wiring of enforce_rate (docs/04 §6)
# --------------------------------------------------------------------------


class _FakePipeline:
    """Mimics redis.asyncio's pipeline chaining surface: each verb
    returns `self` (uncalled/unawaited until `.execute()`), matching how
    `enforce_rate` (app/data/redis_client.py) uses it."""

    def __init__(self, zcard_result: int) -> None:
        self._zcard_result = zcard_result

    def zremrangebyscore(self, *args: object, **kwargs: object) -> _FakePipeline:
        return self

    def zadd(self, *args: object, **kwargs: object) -> _FakePipeline:
        return self

    def zcard(self, *args: object, **kwargs: object) -> _FakePipeline:
        return self

    def expire(self, *args: object, **kwargs: object) -> _FakePipeline:
        return self

    async def execute(self) -> tuple[object, object, int, object]:
        return (None, None, self._zcard_result, None)

    async def __aenter__(self) -> _FakePipeline:
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> bool:
        return False


class _FakeRedis:
    def __init__(self, zcard_result: int) -> None:
        self._zcard_result = zcard_result

    def pipeline(self, transaction: bool = True) -> _FakePipeline:
        return _FakePipeline(self._zcard_result)


async def test_require_rate_limit_allows_a_call_under_the_limit() -> None:
    request = _make_request(
        redis=SimpleNamespace(raw=_FakeRedis(zcard_result=3)),
        settings=SimpleNamespace(rate_limit_sessions_per_min=5),
    )
    principal = SessionUser(user_id="usr_rajesh01")

    await require_rate_limit(request, principal)  # must not raise


async def test_require_rate_limit_raises_once_the_window_is_exceeded() -> None:
    """A 6th hit in-window (over `settings.rate_limit_sessions_per_min`)
    must raise `RateLimitedError`, the same 429 vocabulary docs/13 §1.1
    pins."""
    request = _make_request(
        redis=SimpleNamespace(raw=_FakeRedis(zcard_result=6)),
        settings=SimpleNamespace(rate_limit_sessions_per_min=5),
    )
    principal = SessionUser(user_id="usr_rajesh01")

    with pytest.raises(RateLimitedError):
        await require_rate_limit(request, principal)


async def test_require_rate_limit_reads_the_limit_from_settings() -> None:
    """Review finding (LOW): the limit figure was previously a bare
    hardcoded module constant, so changing `RATE_LIMIT_SESSIONS_PER_MIN`
    via env would silently not affect this surface. Pins that the
    *field* is actually consulted at call time, not just a coincidental
    default that happens to match — a limit of 2 with a 3rd concurrent
    hit must raise, where the old hardcoded 5 would not have."""
    request = _make_request(
        redis=SimpleNamespace(raw=_FakeRedis(zcard_result=3)),
        settings=SimpleNamespace(rate_limit_sessions_per_min=2),
    )
    principal = SessionUser(user_id="usr_rajesh01")

    with pytest.raises(RateLimitedError):
        await require_rate_limit(request, principal)
