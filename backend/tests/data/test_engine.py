"""Unit tests for app.data.engine — the async engine/sessionmaker
factory. No real Postgres involved: `create_async_engine` never opens a
socket at construction time (it only parses the DSN and builds a pool
descriptor), so a syntactically valid but unreachable DSN is enough to
exercise the factory's return shape without Docker/testcontainers (this
task's environment has neither).

Self-contained — a local `_settings(**overrides)` helper, not the shared
`tests/conftest.py` fixture from a sibling in-flight batch (task 2.3),
so this file collects and runs standalone regardless of that PR's merge
order (fixed after review: an earlier version of this file depended on
`tests.conftest`, which doesn't exist on `main` yet and broke pytest
collection for the whole suite). Matches the same local-helper pattern
already used in `tests/obs/test_tracing.py` / `test_logging.py` /
`tests/models/test_orm.py`.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.config import Settings
from app.data.engine import create_engine_and_sessionmaker

_FAKE_DSN = "postgresql+asyncpg://fakeuser:fakepass@localhost/fakedb"


def _settings(**overrides: object) -> Settings:
    """Minimal valid Settings for a test — only the three fields with no
    default (docs/04's fail-fast-on-missing-secrets rule) need supplying;
    kwargs win over any real .env, so this is hermetic."""
    defaults: dict[str, object] = {
        "jwt_secret": "test-secret",
        "database_url": _FAKE_DSN,
        "openrouter_api_key": "test-key",
    }
    defaults.update(overrides)
    return Settings(**defaults)  # type: ignore[arg-type]


def test_create_engine_and_sessionmaker_returns_expected_types() -> None:
    engine, sessionmaker = create_engine_and_sessionmaker(_settings())

    assert isinstance(engine, AsyncEngine)
    assert isinstance(sessionmaker, async_sessionmaker)


def test_create_engine_and_sessionmaker_does_not_connect() -> None:
    """Constructing the engine against an unreachable host must not raise
    or block — `create_async_engine` is lazy, the first socket opens on
    first query, which this test never issues."""
    engine, _ = create_engine_and_sessionmaker(_settings())

    assert engine.url.drivername == "postgresql+asyncpg"
    assert engine.url.host == "localhost"


def test_create_engine_uses_database_url_secret_value() -> None:
    """`database_url` is a SecretStr (app/config.py) specifically so it
    can't leak via an accidental repr/str; the factory must still pull
    the *real* DSN via get_secret_value() when building the engine, or it
    would try to connect to the literal string "**********"."""
    engine, _ = create_engine_and_sessionmaker(_settings())

    assert engine.url.render_as_string(hide_password=False) == _FAKE_DSN


def test_sessionmaker_is_bound_to_the_returned_engine() -> None:
    _, sessionmaker = create_engine_and_sessionmaker(_settings())

    kw = sessionmaker.kw
    assert kw["bind"].url.render_as_string(hide_password=False) == _FAKE_DSN
    assert kw["expire_on_commit"] is False


async def test_sessionmaker_produces_asyncsession_instances_without_connecting() -> None:
    engine, sessionmaker = create_engine_and_sessionmaker(_settings())

    async with sessionmaker() as session:
        assert isinstance(session, AsyncSession)

    await engine.dispose()


def test_different_settings_produce_independent_engines() -> None:
    """The factory is a plain function, not a cached singleton — calling
    it twice must not accidentally share pooled state between engines."""
    engine_a, _ = create_engine_and_sessionmaker(_settings())
    engine_b, _ = create_engine_and_sessionmaker(
        _settings(database_url="postgresql+asyncpg://other:other@localhost/other")
    )

    assert engine_a is not engine_b
    assert engine_a.url.database != engine_b.url.database
