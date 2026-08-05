"""`SemanticRepo.search()` against a real pgvector server — the two things
no mock can prove: that the scoping predicate excludes another merchant's
call summary when Postgres executes it, and that the HNSW post-filter
hazard is actually mitigated rather than merely written about.

Lives in `tests/models/` for the established reason — the standard gate
runs `pytest tests --ignore=tests/models --ignore=tests/e2e`, because
everything here needs a Postgres container. CI runs it on every PR.

**How this file behaves without Docker**, following
`test_memory_orm.py` exactly (read its module docstring for the full
reasoning — a prior audit in this repo found a test that `importorskip`-ed
itself into a permanent silent green skip):

- **Missing dependency** (`testcontainers`, `docker`, `pgvector`) — the
  top-level imports raise at collection. Loud, and it fails the run.
- **Dependency present, daemon absent** — `_docker_daemon_or_skip()`
  catches only `DockerException` from an explicit `ping()` and skips with
  the reason attached, so `-rs` names it and the summary counts it.
- **Daemon present, anything else wrong** — propagates as a failure. The
  `except` is narrow on purpose.

The fixtures below are deliberately module-local duplicates of
`test_memory_orm.py`'s rather than being hoisted into a
`tests/models/conftest.py`: both existing modules in this directory own
their own container, and hoisting would silently change their lifecycles
in a batch that has no business touching them.

**These tests were not executed while being written** — there is no Docker
on the development machine, which is the whole reason for the gating above.
They are written from pgvector's documented behaviour (0.8.0+ iterative
index scans; `hnsw.ef_search` default 40) and CI is their first real run.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import AsyncGenerator, Generator
from pathlib import Path

import docker
import pytest
import pytest_asyncio
from docker.errors import DockerException
from sqlalchemy import insert, select, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from testcontainers.postgres import PostgresContainer

from app.config import Settings
from app.data.repositories.semantic_repo import SemanticRepo
from app.domain.types import EMBEDDING_DIM, MemoryKind, SessionUser
from app.models.orm import MemoryChunk, memory_scope

# backend/tests/models/test_semantic_repo_pg.py -> backend/
BACKEND_ROOT = Path(__file__).resolve().parents[2]

MINE = SessionUser(user_id="usr_mine")
THEIRS = "usr_theirs"

# Enough foreign rows to bury the principal's own row past any plausible
# non-iterative candidate list: comfortably more than pgvector's default
# `hnsw.ef_search` of 40 and more than this repo's configured 100.
FOREIGN_ROWS = 250

MY_SUMMARY = "MINE: my vendor payment was declined at 2:14 PM."
KB_CONTENT = "KB: daily limits reset at midnight IST."


def _vector(second: float) -> list[float]:
    """A 1536-dim vector that differs from the query vector only in its
    second component, so cosine distance is a simple monotone function of
    `second`: larger `second` means farther away. Nothing is a zero
    vector — cosine distance against one is undefined.
    """
    return [1.0, second] + [0.0] * (EMBEDDING_DIM - 2)


QUERY_VECTOR = tuple(_vector(0.0))
# Both matching rows sit FARTHER from the query than every foreign row, so
# a candidate list that is not extended past its first pass contains only
# foreign rows and the scoped query yields nothing.
KB_VECTOR = _vector(0.40)
MY_VECTOR = _vector(0.50)


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
            "Docker daemon unreachable — the Phase-5 semantic-retrieval tests need a "
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


def _settings(**overrides: object) -> Settings:
    defaults: dict[str, object] = {
        "jwt_secret": "test-jwt-secret",
        "database_url": "postgresql+asyncpg://test:test@localhost/test",
        "openrouter_api_key": "test-openrouter-key",
    }
    defaults.update(overrides)
    return Settings(**defaults)  # type: ignore[arg-type]


async def _seed_corpus(session: AsyncSession) -> None:
    """One KB chunk, one call summary owned by `usr_mine`, and
    `FOREIGN_ROWS` call summaries owned by `usr_theirs` — every foreign row
    strictly nearer the query vector than either matching row."""
    await session.execute(
        insert(MemoryChunk),
        [
            {
                "kind": MemoryKind.KB_ARTICLE.value,
                "user_id": None,
                "source_id": "kb_daily_limits",
                "content": KB_CONTENT,
                "embedding": KB_VECTOR,
            },
            {
                "kind": MemoryKind.CALL_SUMMARY.value,
                "user_id": MINE.user_id,
                "source_id": "sess_mine",
                "content": MY_SUMMARY,
                "embedding": MY_VECTOR,
            },
            *[
                {
                    "kind": MemoryKind.CALL_SUMMARY.value,
                    "user_id": THEIRS,
                    "source_id": f"sess_theirs_{i}",
                    "content": f"THEIRS: another shop's payment history {i}.",
                    "embedding": _vector(0.001 * (i + 1)),
                }
                for i in range(FOREIGN_ROWS)
            ],
        ],
    )
    await session.flush()


async def _force_index_scan(session: AsyncSession) -> None:
    """At ~250 rows the planner picks a sequential scan, which applies the
    filter directly and has no post-filter hazard at all — docs/09 §6.1's
    "at ~200 rows the HNSW index is decoration".

    Turning off sequential scans is what makes the index path — the one
    that exists in production once the corpus grows — reachable in a test
    that finishes in seconds. Without this, every assertion below would
    pass no matter what `SemanticRepo` does with `hnsw.*`, which is the
    definition of a vacuous test.
    """
    await session.execute(text("SET LOCAL enable_seqscan = off"))


# --------------------------------------------------------------------------
# The security invariant, executed
# --------------------------------------------------------------------------


async def test_search_never_returns_another_merchants_call_summary(
    session: AsyncSession,
) -> None:
    """**The headline test.** docs/09 §6.2: "a `call_summary` from another
    merchant must be unreachable by construction — in a fintech context
    this filter is a security invariant".

    250 foreign call summaries sit nearer the query vector than anything
    the principal may see, so a retriever with a broken or missing scope
    would return three of them and this test would fail on the first
    assertion. Asserted on `user_id` as well as content, which is exactly
    what `RetrievedMemory` carries that field for.
    """
    await _seed_corpus(session)
    await _force_index_scan(session)

    results = await SemanticRepo(session, _settings()).search(QUERY_VECTOR, MINE, k=3)

    assert all(
        result.kind is MemoryKind.KB_ARTICLE or result.user_id == MINE.user_id
        for result in results
    ), f"cross-tenant leak: {[(r.kind, r.user_id) for r in results]}"
    assert not any(result.user_id == THEIRS for result in results)
    assert not any("THEIRS" in result.content for result in results)


async def test_search_returns_the_shared_kb_and_the_principals_own_summary(
    session: AsyncSession,
) -> None:
    """The other half of the predicate. A scope that returned nothing at
    all would pass the leak test above trivially — this is what stops
    "return zero rows" from being a way to be secure."""
    await _seed_corpus(session)
    await _force_index_scan(session)

    results = await SemanticRepo(session, _settings()).search(QUERY_VECTOR, MINE, k=3)
    contents = {result.content for result in results}

    assert KB_CONTENT in contents
    assert MY_SUMMARY in contents


async def test_search_scopes_to_the_principal_that_was_passed(
    session: AsyncSession,
) -> None:
    """Symmetry check: the same corpus, the other merchant. `usr_theirs`
    must see their own summaries and never `usr_mine`'s — a predicate
    hard-wired to one merchant would pass the tests above and fail here.
    """
    await _seed_corpus(session)
    await _force_index_scan(session)

    results = await SemanticRepo(session, _settings()).search(
        QUERY_VECTOR, SessionUser(user_id=THEIRS), k=3
    )

    assert results, "the foreign merchant's own rows are the nearest in the corpus"
    assert all(result.user_id != MINE.user_id for result in results)
    assert MY_SUMMARY not in {result.content for result in results}


# --------------------------------------------------------------------------
# The HNSW post-filter hazard, demonstrated and then mitigated
# --------------------------------------------------------------------------


async def test_a_plain_hnsw_scan_loses_the_principals_own_rows(
    session: AsyncSession,
) -> None:
    """**The hazard, reproduced.** This is the failure `SemanticRepo`
    exists to avoid, executed rather than described: with iterative scans
    off and pgvector's default `ef_search` of 40, the index hands up the
    ~40 globally nearest rows — all of them foreign — and the scoping
    predicate then deletes every one. The principal's own summary and the
    shared KB article are both in the table, both perfectly valid results,
    and the query returns neither.

    No error is raised at any layer. Downstream it reads as "no relevant
    memory", which is why this is worth a test rather than a comment.

    Written as raw SQL rather than through `SemanticRepo` because the repo
    deliberately makes this state unreachable. If a future pgvector
    changes its defaults such that this no longer reproduces, THIS test
    fails while the guarantee tests stay green — which is the right way
    round: it would mean the hazard is gone, not that the fix broke.
    """
    await _seed_corpus(session)
    await _force_index_scan(session)
    await session.execute(text("SET LOCAL hnsw.iterative_scan = off"))
    await session.execute(text("SET LOCAL hnsw.ef_search = 40"))

    distance = MemoryChunk.embedding.cosine_distance(list(QUERY_VECTOR)).label("distance")
    rows = (
        await session.execute(
            select(MemoryChunk.content)
            .where(memory_scope(MINE))
            .order_by(distance)
            .limit(3)
        )
    ).scalars()
    contents = set(rows)

    assert MY_SUMMARY not in contents, (
        "expected the HNSW post-filter to hide the principal's own row at "
        "ef_search=40 with iterative scans off — if this now passes, pgvector's "
        "behaviour changed and SemanticRepo's mitigation should be re-justified"
    )


async def test_iterative_scan_recovers_the_principals_own_rows(
    session: AsyncSession,
) -> None:
    """**The mitigation, on the identical corpus and the identical query.**
    Same 250 foreign rows, same vector, same `k` — the only difference is
    that `SemanticRepo.search()` sets `hnsw.iterative_scan` before issuing
    the query, so the scan keeps fetching candidates until rows have
    actually passed the filter.

    Paired with the test above on purpose: alone, either one could be
    explained by the planner having quietly ignored the index.
    """
    await _seed_corpus(session)
    await _force_index_scan(session)

    results = await SemanticRepo(session, _settings()).search(QUERY_VECTOR, MINE, k=3)
    contents = {result.content for result in results}

    assert MY_SUMMARY in contents
    assert KB_CONTENT in contents


async def test_search_returns_only_rows_the_principal_may_see(
    session: AsyncSession,
) -> None:
    """**The scope holds against a real server**, on the hard corpus: 250
    foreign summaries all strictly nearer the query vector than either
    row the principal is allowed to see. Exactly two rows in this corpus
    are visible to `usr_mine`, so a correct implementation returns two for
    `k=3`, and every returned row is either the shared KB or owned by the
    principal.

    **This test was renamed, and the rename is the point.** It was called
    `test_search_transports_no_foreign_row_into_the_process` and asserted
    only `len(results) == 2`. A security review observed that an
    implementation issuing an unscoped `LIMIT 300` and filtering in Python
    returns 2 and passes — while transporting 250 merchants' summaries
    into process memory, which is precisely what the old name forbade. A
    test named after a property its assertions cannot observe is worse
    than a narrow test, because the name is what a reader trusts.

    So this now asserts what it *can* see from here — membership, which
    the old version did not check either, despite its docstring promising
    "never with a foreign row among them". The no-over-fetch property is
    genuinely proven, one layer down, by
    `test_search_does_not_over_fetch_beyond_k` in
    `tests/data/repositories/test_semantic_repo.py`, which asserts the
    compiled statement's `LIMIT` is `k` — the number of rows the server is
    told to send is the number of rows that can cross the wire.
    """
    await _seed_corpus(session)
    await _force_index_scan(session)

    results = await SemanticRepo(session, _settings()).search(QUERY_VECTOR, MINE, k=3)

    assert len(results) == 2
    assert {result.content for result in results} == {MY_SUMMARY, KB_CONTENT}
    for result in results:
        assert result.user_id in (None, MINE.user_id), (
            f"row owned by {result.user_id!r} reached a search by {MINE.user_id!r}"
        )
        assert result.user_id != THEIRS


# --------------------------------------------------------------------------
# Scoring and ordering, against real cosine distances
# --------------------------------------------------------------------------


async def test_search_returns_cosine_similarity_not_distance(
    session: AsyncSession,
) -> None:
    """`RetrievedMemory.similarity` is cosine similarity (higher is
    better); pgvector's `<=>` returns cosine distance (lower is better).
    The KB vector's true similarity to the query is 1/sqrt(1 + 0.40**2).

    If the conversion were dropped, this would read ~0.14 instead of
    ~0.93 — below docs/09 §6.2's 0.70 floor, so every genuinely good
    result would be silently discarded by `SemanticMemory`.
    """
    await _seed_corpus(session)
    await _force_index_scan(session)

    results = await SemanticRepo(session, _settings()).search(QUERY_VECTOR, MINE, k=3)
    kb = next(result for result in results if result.content == KB_CONTENT)

    assert kb.similarity == pytest.approx(1 / (1 + 0.40**2) ** 0.5, abs=1e-4)
    assert kb.similarity > 0.70


async def test_search_orders_nearest_first(session: AsyncSession) -> None:
    """The KB chunk is nearer than the principal's own summary by
    construction, so top-k truncation and the floor both act on a list
    that is genuinely sorted."""
    await _seed_corpus(session)
    await _force_index_scan(session)

    results = await SemanticRepo(session, _settings()).search(QUERY_VECTOR, MINE, k=3)

    assert [result.content for result in results] == [KB_CONTENT, MY_SUMMARY]
    assert results[0].similarity > results[1].similarity


async def test_search_respects_k_against_a_real_index(session: AsyncSession) -> None:
    await _seed_corpus(session)
    await _force_index_scan(session)

    results = await SemanticRepo(session, _settings()).search(QUERY_VECTOR, MINE, k=1)

    assert len(results) == 1
    assert results[0].content == KB_CONTENT
