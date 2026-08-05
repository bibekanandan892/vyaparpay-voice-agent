"""ORM round-trip and CHECK-constraint tests for the Phase-5 memory tables
(`conversation_summaries`, `user_profiles`, `kb_articles`,
`memory_chunks`), plus the one thing only a live server can prove: that
migration 0002 applies on top of 0001 and Postgres actually *enforces*
the constraints the models declare.

Lives in `tests/models/` for the established reason — the standard gate
runs `pytest tests --ignore=tests/models --ignore=tests/e2e`, because
everything here needs a Postgres container. Fixture pattern mirrors
`test_orm.py`: one session-scoped `pgvector/pgvector:pg16` container,
migrated once via `alembic upgrade head`, per-test engine (function-scoped
on purpose — see that file's `engine` docstring for the event-loop
reason), per-test rolled-back transaction.

**How this file behaves without Docker, deliberately.** A prior audit in
this repo found a test that `importorskip`-ed itself into a permanent
silent green skip, hiding an untested integration path for a whole phase.
The three outcomes here are kept distinguishable:

- **Missing dependency** (`testcontainers`, `docker`, `pgvector`) — the
  top-level imports below raise at collection. Loud, and it fails the run.
  Nothing here is behind `importorskip`.
- **Dependency present, daemon absent** — `_docker_daemon_or_skip()`
  catches only `DockerException` from an explicit `ping()` and skips with
  the reason attached, so `-rs` names it and the summary counts it. That
  is the honest state on a machine with no Docker.
- **Daemon present, anything else wrong** (image pull fails, migration
  errors, a CHECK is missing) — propagates as a failure. The `except` is
  narrow on purpose: catching broad `Exception` here is exactly how a real
  regression would get relabelled as "no Docker" and disappear.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import AsyncGenerator, Generator
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import docker
import pytest
import pytest_asyncio
from docker.errors import DockerException
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from testcontainers.postgres import PostgresContainer

from app.domain.types import EMBEDDING_DIM
from app.models.orm import (
    Conversation,
    ConversationSummary,
    KbArticle,
    MemoryChunk,
    Merchant,
    UserProfile,
)

# backend/tests/models/test_memory_orm.py -> backend/
BACKEND_ROOT = Path(__file__).resolve().parents[2]


def _embedding(fill: float = 0.01) -> list[float]:
    return [fill] * EMBEDDING_DIM


def _docker_daemon_or_skip() -> None:
    """Skip only for an unreachable daemon, and say so in the reason.

    `DockerException` is what `docker.from_env()` raises when it cannot
    reach a daemon; nothing else is caught, so a broken image or a failed
    migration still fails the run rather than masquerading as "no Docker".
    """
    try:
        docker.from_env().ping()
    except DockerException as exc:
        pytest.skip(
            "Docker daemon unreachable — the Phase-5 memory ORM tests need a "
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
    """`alembic upgrade head` once per session — which now means 0001 then
    0002. This is the only place the two revisions are proven to compose
    (0002's `vector(1536)` column depends on the extension 0001 created)."""
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


async def _seed_conversation(session: AsyncSession, suffix: str) -> tuple[str, str]:
    """A merchant + its conversation anchor row, since
    `conversation_summaries` FKs `conversations` (docs/12 §4.3)."""
    user_id, session_id = f"usr_{suffix}", f"sess_{suffix}"
    session.add(
        Merchant(
            merchant_id=user_id,
            business_name="Kumar General Store",
            city="Jaipur",
            account_type="Merchant Pro",
            merchant_since=datetime(2022, 1, 1, tzinfo=UTC).date(),
        )
    )
    await session.flush()
    session.add(
        Conversation(session_id=session_id, user_id=user_id, signaling_token_hash="deadbeef")
    )
    await session.flush()
    return user_id, session_id


# --------------------------------------------------------------------------
# Migration 0002 applied cleanly
# --------------------------------------------------------------------------


async def test_pgvector_extension_is_present_and_still_installed(session: AsyncSession) -> None:
    """0001 created it; 0002 must not have recreated or dropped it. If
    0002 had run `CREATE EXTENSION` without `IF NOT EXISTS`, the migration
    would have failed outright and `_migrated` would have raised."""
    result = await session.execute(text("SELECT 1 FROM pg_extension WHERE extname = 'vector'"))

    assert result.scalar_one() == 1


async def test_hnsw_cosine_index_exists_on_memory_chunks(session: AsyncSession) -> None:
    result = await session.execute(
        text("SELECT indexdef FROM pg_indexes WHERE indexname = 'idx_chunks_embedding'")
    )
    indexdef = result.scalar_one()

    assert "USING hnsw" in indexdef
    assert "vector_cosine_ops" in indexdef


# --------------------------------------------------------------------------
# Round trips
# --------------------------------------------------------------------------


async def test_conversation_summary_round_trip(session: AsyncSession) -> None:
    user_id, session_id = await _seed_conversation(session, "summary")

    session.add(
        ConversationSummary(
            session_id=session_id,
            user_id=user_id,
            summary="Vendor payment declined - daily limit exceeded.",
            resolution="pending",
            intents=["payment_failure", "limit_increase"],
            tools_used=["get_wallet_balance", "request_limit_increase"],
            turn_count=15,
            duration_s=214,
            cost_usd=Decimal("0.2841"),
        )
    )
    await session.flush()

    fetched = await session.get(ConversationSummary, session_id)

    assert fetched is not None
    assert fetched.intents == ["payment_failure", "limit_increase"]
    assert fetched.cost_usd == Decimal("0.2841")
    assert fetched.created_at is not None  # server_default now()


async def test_conversation_summary_rejects_unknown_resolution(session: AsyncSession) -> None:
    """`ck_conversation_summaries_resolution` (docs/12 §4.3)."""
    user_id, session_id = await _seed_conversation(session, "badres")

    session.add(
        ConversationSummary(
            session_id=session_id,
            user_id=user_id,
            summary="...",
            resolution="half_resolved",
            intents=[],
            tools_used=[],
            turn_count=1,
            duration_s=1,
            cost_usd=Decimal("0"),
        )
    )

    with pytest.raises(IntegrityError):
        await session.flush()


async def test_user_profile_round_trip_with_jsonb(session: AsyncSession) -> None:
    session.add(
        UserProfile(
            user_id="usr_profile",
            facts={"business_name": "Kumar General Store", "city": "Jaipur"},
            preferences={"language": "English"},
            open_issues=[{"id": "iss_071", "status": "pending"}],
            updated_by_call="a1f3c9",
        )
    )
    await session.flush()

    fetched = await session.get(UserProfile, "usr_profile")

    assert fetched is not None
    assert fetched.facts["city"] == "Jaipur"
    assert fetched.open_issues[0]["id"] == "iss_071"
    assert fetched.updated_by_call == "a1f3c9"


async def test_user_profile_jsonb_defaults_to_empty_containers(session: AsyncSession) -> None:
    session.add(UserProfile(user_id="usr_empty_profile"))
    await session.flush()

    fetched = await session.get(UserProfile, "usr_empty_profile")

    assert fetched is not None
    assert fetched.facts == {}
    assert fetched.preferences == {}
    assert fetched.open_issues == []
    assert fetched.updated_at is not None


async def test_user_profile_row_does_not_validate_its_jsonb_shape(session: AsyncSession) -> None:
    """Pinning a *gap*, not a feature. docs/09 §5.2's closed extraction
    schema is a Pydantic-boundary control on the post-call merge path;
    the column itself is plain JSONB and will store an inferred key like
    `mood` without complaint. Nothing at this layer stops it, and the
    model docstring says so — this test exists so that stays true and
    documented rather than being quietly assumed to be enforced here.
    """
    session.add(UserProfile(user_id="usr_unvalidated", facts={"mood": "frustrated"}))
    await session.flush()

    fetched = await session.get(UserProfile, "usr_unvalidated")

    assert fetched is not None
    assert fetched.facts == {"mood": "frustrated"}


async def test_kb_article_round_trip(session: AsyncSession) -> None:
    session.add(
        KbArticle(
            slug="kb_daily_limits",
            title="Daily transaction limits",
            body_md="# Daily limits\nLimits reset at midnight IST.",
            category="limits",
        )
    )
    await session.flush()

    fetched = await session.get(KbArticle, "kb_daily_limits")

    assert fetched is not None
    assert fetched.version == 1  # server_default round-trips
    assert fetched.updated_at is not None


async def test_kb_article_rejects_unknown_category(session: AsyncSession) -> None:
    """`ck_kb_articles_category` — the 5 seeded categories (docs/12 §5)."""
    session.add(
        KbArticle(
            slug="kb_bogus",
            title="...",
            body_md="...",
            category="cryptocurrency",
        )
    )

    with pytest.raises(IntegrityError):
        await session.flush()


async def test_memory_chunk_round_trip_preserves_the_vector(session: AsyncSession) -> None:
    session.add(
        MemoryChunk(
            kind="kb_article",
            source_id="kb_daily_limits",
            content="Daily limits reset at midnight IST.",
            embedding=_embedding(),
        )
    )
    await session.flush()

    fetched = (
        await session.execute(select(MemoryChunk).where(MemoryChunk.source_id == "kb_daily_limits"))
    ).scalar_one()

    assert fetched.id is not None  # BIGSERIAL assigned
    assert len(fetched.embedding) == EMBEDDING_DIM
    assert fetched.user_id is None
    assert fetched.created_at is not None


async def test_memory_chunk_rejects_a_wrong_width_vector(session: AsyncSession) -> None:
    """The `vector(1536)` column width is enforced by Postgres, not just by
    `app.domain.types.MemoryChunk`'s validator — this is the backstop for
    any write path that bypasses the domain type."""
    session.add(
        MemoryChunk(
            kind="kb_article",
            source_id="kb_wrong_width",
            content="...",
            embedding=[0.01] * 3072,  # text-embedding-3-large's width
        )
    )

    with pytest.raises(Exception, match="(?i)expected 1536 dimensions|vector"):
        await session.flush()


# --------------------------------------------------------------------------
# ck_memory_chunks_user_scope — the constraint neither doc's DDL declared
# --------------------------------------------------------------------------


async def test_call_summary_chunk_without_user_id_is_rejected(session: AsyncSession) -> None:
    """`ck_memory_chunks_user_scope`. Both docs annotate the column
    "REQUIRED for call_summary (scoping)" and neither declares a
    constraint; this asserts Postgres now enforces it.

    Why it matters: docs/09 §6.2's retrieval predicate is
    `kind = 'kb_article' OR user_id = :session_user`. A NULL `user_id` on
    a call summary makes `user_id = :session_user` evaluate to NULL for
    every merchant, so the row is unreachable by its own owner — memory
    loss with no error at retrieval time.
    """
    session.add(
        MemoryChunk(
            kind="call_summary",
            source_id="a1f3c9",
            content="Rajesh's vendor payment was declined...",
            embedding=_embedding(),
            user_id=None,
        )
    )

    with pytest.raises(IntegrityError):
        await session.flush()


async def test_kb_article_chunk_with_user_id_is_rejected(session: AsyncSession) -> None:
    """The mirror-image half of the same biconditional: a KB chunk is
    globally readable through the `kind` half of the predicate, so
    labelling one merchant-private is incoherent."""
    session.add(
        MemoryChunk(
            kind="kb_article",
            source_id="kb_daily_limits",
            content="...",
            embedding=_embedding(),
            user_id="usr_rajesh01",
        )
    )

    with pytest.raises(IntegrityError):
        await session.flush()


async def test_memory_chunk_rejects_unknown_kind(session: AsyncSession) -> None:
    """`ck_memory_chunks_kind` — only the two corpus kinds docs/09 §6.1
    defines share this table and index."""
    session.add(
        MemoryChunk(
            kind="scraped_webpage",
            source_id="x",
            content="...",
            embedding=_embedding(),
            user_id="usr_rajesh01",
        )
    )

    with pytest.raises(IntegrityError):
        await session.flush()


# --------------------------------------------------------------------------
# The scoping predicate itself, executed against real rows
# --------------------------------------------------------------------------


async def test_scoping_predicate_hides_another_merchants_call_summary(
    session: AsyncSession,
) -> None:
    """docs/09 §6.2's WHERE clause, run against two merchants' rows.

    This is the closest a schema-level test can get to the security
    invariant: it proves the predicate *shape* excludes a foreign
    `call_summary` while still admitting the shared KB. It does NOT prove
    Batch 2's retriever uses this predicate — that test belongs with the
    implementation, and `SemanticMemoryProto`'s docstring says as much.
    """
    session.add_all(
        [
            MemoryChunk(
                kind="kb_article",
                source_id="kb_daily_limits",
                content="KB: limits reset at midnight.",
                embedding=_embedding(),
            ),
            MemoryChunk(
                kind="call_summary",
                source_id="sess_mine",
                content="MINE: my declined payment.",
                embedding=_embedding(),
                user_id="usr_mine",
            ),
            MemoryChunk(
                kind="call_summary",
                source_id="sess_theirs",
                content="THEIRS: another shop's payment history.",
                embedding=_embedding(),
                user_id="usr_theirs",
            ),
        ]
    )
    await session.flush()

    rows = (
        await session.execute(
            select(MemoryChunk.content).where(
                (MemoryChunk.kind == "kb_article") | (MemoryChunk.user_id == "usr_mine")
            )
        )
    ).scalars()
    contents = set(rows)

    assert "KB: limits reset at midnight." in contents
    assert "MINE: my declined payment." in contents
    assert "THEIRS: another shop's payment history." not in contents
