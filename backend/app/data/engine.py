"""Async SQLAlchemy engine + sessionmaker factory (docs/04-backend-
architecture.md §4-5).

`app/main.py`'s lifespan (Batch 3.3) is the only production caller: it
calls `create_engine_and_sessionmaker(settings)` once at startup and
stores both halves on `app.state` (the engine so it can be `.dispose()`d
on shutdown, the sessionmaker so `app/api/deps.py`'s `get_db` can open
one `AsyncSession` per request, docs/04 §4/§5). Returning the engine
alongside the sessionmaker — rather than only the sessionmaker, as docs/04
§4's inline sketch shows — is this factory's one deliberate addition: the
sketch never disposes the engine it builds, and a later batch needs a
handle to do that on the `yield` line of the lifespan context manager.

Never eagerly connects: `create_async_engine` only builds a connection
pool descriptor and parses the DSN; the first real socket is opened by
the first query issued through the pool. That laziness is what makes this
factory testable without Docker/Postgres — see `tests/data/test_engine.py`.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import Settings


def create_engine_and_sessionmaker(
    settings: Settings,
) -> tuple[AsyncEngine, async_sessionmaker[AsyncSession]]:
    """Build the process-singleton async engine + its sessionmaker.

    `settings.database_url` is a `SecretStr` (app/config.py's fail-fast/
    no-accidental-repr-leak rule) — `.get_secret_value()` is called here,
    at the one point the real DSN is actually needed, never earlier.
    `expire_on_commit=False` on the sessionmaker matches docs/04 §5's
    one-`AsyncSession`-per-request transaction-boundary model: a caller
    that reads an ORM attribute off a committed object later in the same
    request (e.g. after `session.commit()` but before the response is
    built) shouldn't trigger an implicit re-fetch.
    """
    engine = create_async_engine(settings.database_url.get_secret_value(), future=True)
    sessionmaker = async_sessionmaker(bind=engine, expire_on_commit=False)
    return engine, sessionmaker


__all__ = ["create_engine_and_sessionmaker"]
