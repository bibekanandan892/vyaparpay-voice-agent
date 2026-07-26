"""Async Alembic migration environment (docs/12 §10).

Reads `DATABASE_URL` from `os.environ` directly rather than
`app.config.Settings` — the migration environment must not import the
app package (avoids pulling in the full Settings validation, OTel
setup, etc. just to run a schema migration; a bare asyncpg-driver URL
is all this needs). `DATABASE_URL` must use the `+asyncpg` dialect,
e.g. `postgresql+asyncpg://user:pass@host:5432/dbname`.

Follows Alembic's standard async template (`alembic init -t async`):
`run_migrations_online()` builds an `AsyncEngine` via
`async_engine_from_config` and drives `context.run_migrations()` through
`AsyncConnection.run_sync`, invoked with `asyncio.run`.
"""

from __future__ import annotations

import asyncio
import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import Connection, pool
from sqlalchemy.ext.asyncio import async_engine_from_config

from app.models.orm import Base

# Alembic Config object, providing access to values within alembic.ini.
config = context.config

# Interpret the config file for Python logging (alembic.ini's
# [loggers]/[handlers]/[formatters] sections).
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# `Base.metadata` is what autogenerate would diff against; migration
# 0001 is hand-written instead (docs/12 §10), but target_metadata is
# still wired up for any future `alembic revision --autogenerate` draft.
target_metadata = Base.metadata

# DATABASE_URL from the environment overrides alembic.ini's blank
# sqlalchemy.url — this is the one and only place this env.py reads
# configuration from.
database_url = os.environ.get("DATABASE_URL")
if database_url:
    config.set_main_option("sqlalchemy.url", database_url)


def run_migrations_offline() -> None:
    """Emit SQL to stdout without a live DB connection (`--sql` mode)."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    """Build an AsyncEngine and run migrations against a live connection."""
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
