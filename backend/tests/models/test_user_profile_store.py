"""`UserProfileRepo` + `UserProfileMemory` against a live Postgres — the
parts of docs/09-memory-architecture.md §5 that only a real server can
prove.

The unit tests in tests/memory/test_user_profile.py and
tests/data/repositories/test_repositories.py cover the merge policy and
the compiled statement. What they cannot cover, and this file does:

- that `INSERT ... ON CONFLICT (user_id) DO UPDATE` is actually accepted
  by Postgres and updates rather than raising, which is docs/09 §8's
  idempotency at the SQL layer;
- that `updated_at` advances on the conflict path — a compiled-string
  assertion proves the set-clause was *written*, not that it *fires*;
- that `model_dump(mode="json")` output survives a JSONB round trip and
  validates back into `OpenIssue`, datetimes included;
- that the closed schema laundering works against a row Postgres really
  stored, including the disallowed keys tests/models/test_memory_orm.py
  proves the column accepts.

Lives in `tests/models/` for the established reason — the standard gate
runs `pytest tests --ignore=tests/models --ignore=tests/e2e`, because
everything here needs a Postgres container. Fixture pattern is
`test_memory_orm.py`'s, including its three-outcome discipline, restated
because it is the point:

- **Missing dependency** (`testcontainers`, `docker`) — the top-level
  imports below raise at collection. Loud, and it fails the run. Nothing
  here is behind `importorskip`.
- **Dependency present, daemon absent** — `_docker_daemon_or_skip()`
  catches only `DockerException` from an explicit `ping()` and skips with
  the reason attached, so `-rs` names it and the summary counts it.
- **Daemon present, anything else wrong** — propagates as a failure. The
  `except` is narrow on purpose: catching broad `Exception` here is how a
  real regression gets relabelled as "no Docker" and disappears.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import AsyncGenerator, Generator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import docker
import pytest
import pytest_asyncio
from docker.errors import DockerException
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from testcontainers.postgres import PostgresContainer

from app.data.repositories.user_profile_repo import UserProfileRepo
from app.memory.user_profile import IssueOpen, UserProfileMemory
from app.models.orm import UserProfile as UserProfileRow

# backend/tests/models/test_user_profile_store.py -> backend/
BACKEND_ROOT = Path(__file__).resolve().parents[2]

CALL = "a1f3c9"
LATER_CALL = "b2e4d8"


def _docker_daemon_or_skip() -> None:
    """Skip only for an unreachable daemon, and say so in the reason."""
    try:
        docker.from_env().ping()
    except DockerException as exc:
        pytest.skip(
            "Docker daemon unreachable — the user-profile store tests need a "
            f"live pgvector/pgvector:pg16 container ({type(exc).__name__}: {exc})",
            allow_module_level=True,
        )


@pytest.fixture(scope="session")
def postgres_container() -> Generator[PostgresContainer, None, None]:
    _docker_daemon_or_skip()
    with PostgresContainer("pgvector/pgvector:pg16", driver="asyncpg") as container:
        yield container


@pytest.fixture(scope="session")
def database_url(postgres_container: PostgresContainer) -> str:
    return postgres_container.get_connection_url()


@pytest.fixture(scope="session", autouse=True)
def _migrated(database_url: str) -> None:
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=str(BACKEND_ROOT),
        env={**os.environ, "DATABASE_URL": database_url},
        check=True,
    )


@pytest_asyncio.fixture
async def engine(database_url: str) -> AsyncGenerator[AsyncEngine, None]:
    """Function-scoped — see tests/models/test_orm.py::engine for why a
    session-scoped async engine breaks under per-test event loops."""
    eng = create_async_engine(database_url, future=True)
    yield eng
    await eng.dispose()


@pytest_asyncio.fixture
async def session(engine: AsyncEngine) -> AsyncGenerator[AsyncSession, None]:
    async with engine.connect() as conn:
        outer_txn = await conn.begin()
        maker = async_sessionmaker(bind=conn, expire_on_commit=False)
        async with maker() as sess:
            yield sess
        await outer_txn.rollback()


# --------------------------------------------------------------------------
# UserProfileRepo.upsert against real Postgres
# --------------------------------------------------------------------------


async def test_upsert_inserts_a_row_that_did_not_exist(session: AsyncSession) -> None:
    repo = UserProfileRepo(session)

    await repo.upsert(
        "usr_insert",
        facts={"city": "Jaipur"},
        preferences={"language": "English"},
        open_issues=[],
        updated_at=datetime(2026, 7, 24, 14, 29, tzinfo=UTC),
        updated_by_call=CALL,
    )

    row = await session.get(UserProfileRow, "usr_insert")
    assert row is not None
    assert row.facts == {"city": "Jaipur"}
    assert row.updated_by_call == CALL


async def test_upsert_twice_updates_instead_of_raising(session: AsyncSession) -> None:
    """docs/09 §8's idempotency at the SQL layer: a retried pipeline must
    correct the row, not hit a primary-key `IntegrityError` that masks
    whatever caused the retry."""
    repo = UserProfileRepo(session)
    first = datetime(2026, 7, 24, 14, 29, tzinfo=UTC)

    await repo.upsert(
        "usr_conflict",
        facts={"city": "Jaipur"},
        preferences={},
        open_issues=[],
        updated_at=first,
        updated_by_call=CALL,
    )
    await repo.upsert(
        "usr_conflict",
        facts={"city": "Udaipur"},
        preferences={"language": "Hindi"},
        open_issues=[],
        updated_at=first + timedelta(minutes=5),
        updated_by_call=LATER_CALL,
    )

    row = await session.get(UserProfileRow, "usr_conflict")
    assert row is not None
    assert row.facts == {"city": "Udaipur"}
    assert row.preferences == {"language": "Hindi"}
    assert row.updated_by_call == LATER_CALL


async def test_upsert_advances_updated_at_on_the_conflict_path(session: AsyncSession) -> None:
    """The column has `server_default now()` and no `onupdate`, so this
    only holds because the DO UPDATE set-clause carries `updated_at`
    explicitly. A compiled-string assertion proves the clause was written;
    this proves it fires."""
    repo = UserProfileRepo(session)
    first = datetime(2026, 7, 24, 14, 29, tzinfo=UTC)
    second = first + timedelta(hours=3)

    await repo.upsert(
        "usr_touch",
        facts={},
        preferences={},
        open_issues=[],
        updated_at=first,
        updated_by_call=CALL,
    )
    await repo.upsert(
        "usr_touch",
        facts={},
        preferences={},
        open_issues=[],
        updated_at=second,
        updated_by_call=LATER_CALL,
    )

    row = await session.get(UserProfileRow, "usr_touch")
    assert row is not None
    assert row.updated_at == second


# --------------------------------------------------------------------------
# UserProfileMemory end to end
# --------------------------------------------------------------------------


async def test_load_returns_an_empty_profile_for_a_merchant_with_no_row(
    session: AsyncSession,
) -> None:
    memory = UserProfileMemory(UserProfileRepo(session))

    profile = await memory.load("usr_never_called")

    assert profile.user_id == "usr_never_called"
    assert profile.facts.business_name is None
    assert profile.open_issues == ()
    assert profile.updated_by_call is None


async def test_merge_then_load_round_trips_through_jsonb(session: AsyncSession) -> None:
    """The one thing the in-memory double cannot prove: that
    `model_dump(mode="json")` output survives a real JSONB column and
    validates back into `OpenIssue` — datetimes included, since they cross
    the boundary as ISO strings."""
    memory = UserProfileMemory(UserProfileRepo(session))

    written = await memory.merge_post_call(
        "usr_roundtrip",
        session_id=CALL,
        extraction={
            "facts": {"business_name": "Kumar General Store", "city": "Jaipur"},
            "preferences": {"language": "English"},
        },
        opened_issues=[
            IssueOpen(
                id="iss_071",
                summary="Daily limit increase requested: 25,000 -> 50,000",
                status="pending",
            )
        ],
    )
    reloaded = await memory.load("usr_roundtrip")

    assert reloaded.facts == written.facts
    assert reloaded.preferences == written.preferences
    assert reloaded.open_issues == written.open_issues
    assert reloaded.open_issues[0].opened_at == written.open_issues[0].opened_at
    assert reloaded.updated_by_call == CALL


async def test_a_second_merge_folds_onto_the_stored_row(session: AsyncSession) -> None:
    memory = UserProfileMemory(UserProfileRepo(session))

    await memory.merge_post_call(
        "usr_fold", session_id=CALL, extraction={"facts": {"city": "Jaipur"}}
    )
    profile = await memory.merge_post_call(
        "usr_fold",
        session_id=LATER_CALL,
        extraction={"facts": {"account_type": "Merchant Pro"}},
    )

    assert profile.facts.city == "Jaipur"  # not erased by a call that stayed silent
    assert profile.facts.account_type == "Merchant Pro"
    assert profile.updated_by_call == LATER_CALL


async def test_load_launders_a_row_postgres_really_stored(session: AsyncSession) -> None:
    """tests/models/test_memory_orm.py proves the column accepts
    `{"mood": ...}`. This proves the read path drops it — the closed
    schema is a property of this code, never of the table."""
    session.add(
        UserProfileRow(
            user_id="usr_junk",
            facts={"business_name": "Kumar General Store", "mood": "frustrated"},
            preferences={},
            open_issues=[{"id": "iss_broken"}],
            updated_by_call=CALL,
        )
    )
    await session.flush()
    memory = UserProfileMemory(UserProfileRepo(session))

    profile = await memory.load("usr_junk")

    assert profile.facts.business_name == "Kumar General Store"
    assert "mood" not in profile.facts.model_dump()
    assert profile.open_issues == ()  # the malformed entry is dropped, not raised on


async def test_merge_does_not_write_back_disallowed_keys(session: AsyncSession) -> None:
    session.add(
        UserProfileRow(
            user_id="usr_launder",
            facts={"city": "Jaipur", "mood": "frustrated"},
            preferences={},
            open_issues=[],
            updated_by_call=CALL,
        )
    )
    await session.flush()
    memory = UserProfileMemory(UserProfileRepo(session))

    await memory.merge_post_call(
        "usr_launder",
        session_id=LATER_CALL,
        extraction={"facts": {"account_type": "Merchant Pro"}},
    )

    row = await session.get(UserProfileRow, "usr_launder")
    assert row is not None
    await session.refresh(row)
    assert row.facts == {"city": "Jaipur", "account_type": "Merchant Pro"}


async def test_a_profile_can_exist_without_a_merchant_row(session: AsyncSession) -> None:
    """`user_profiles` has no FK to `merchants` — docs/09 §5.1 owns this
    table and declares a bare `TEXT PRIMARY KEY`, while docs/12 §1's ER
    diagram draws a relationship neither doc's DDL enforces. Batch 1
    pinned the discrepancy at the model level; this pins the *consequence*
    for the write path, so that adding the FK later is a deliberate
    migration that fails this test loudly rather than a silent
    reconciliation."""
    memory = UserProfileMemory(UserProfileRepo(session))

    profile = await memory.merge_post_call(
        "usr_no_merchant_row_exists",
        session_id=CALL,
        extraction={"facts": {"city": "Jaipur"}},
    )

    assert profile.facts.city == "Jaipur"
    assert await session.get(UserProfileRow, "usr_no_merchant_row_exists") is not None
