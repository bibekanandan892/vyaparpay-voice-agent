"""Unit tests for `app.memory.conversation_summary_store` — the durable
half of memory layer 3 (docs/09-memory-architecture.md §8's
`conversation_summaries` row).

Doubling strategy mirrors `tests/agent/test_cost_tracker.py`, which owns
the same session-factory/repo split: a `_FakeSessionFactory` stands in for
`async_sessionmaker` (calling it yields an async context manager over one
fake session) and a recording repo double captures what the store asked
the repo to do. No Docker, no Postgres — the SQL those repo calls compile
to is covered in `tests/data/repositories/test_repositories.py`, and that
Postgres actually enforces the schema is `tests/models/test_memory_orm.py`,
which is Docker-gated.

What is NOT proved here: that a summary row and its `memory_chunks` vector
land in one transaction (docs/04 §5's transaction-boundary note). The
embed does not exist yet — `ConversationSummaryStore`'s judgment call #3
says so plainly, and there is no test here pretending otherwise.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, cast

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from structlog.testing import capture_logs

from app.config import Settings
from app.data.repositories.conversation_repo import ConversationRepo
from app.domain.types import EMBEDDING_DIM, ConversationSummary, Resolution
from app.memory.conversation_summary_store import ConversationSummaryStore
from app.models.orm import ConversationSummary as ConversationSummaryRow
from app.models.orm import MemoryChunk as MemoryChunkRow
from tests.fakes import FakeEmbeddings

CANONICAL = ConversationSummary(
    session_id="a1f3c9",
    user_id="usr_rajesh01",
    summary=(
        "Rajesh's ₹245 vendor payment to Amazon Business was declined at 2:14 PM — "
        "daily limit exceeded (₹24,890 of ₹25,000 used)."
    ),
    resolution=Resolution.PENDING,
    intents=("payment_failure", "limit_increase"),
    tools_used=("get_wallet_balance", "request_limit_increase"),
    turn_count=15,
    duration_s=283,
    cost_usd=Decimal("0.2977"),
)


class _RecordingRepo:
    """Records `ConversationRepo`'s summary methods without touching SQL."""

    def __init__(self) -> None:
        self.upserts: list[dict[str, Any]] = []
        self.get_calls: list[str] = []
        self.list_calls: list[tuple[str, int]] = []
        self.row: ConversationSummaryRow | None = None
        self.rows: list[ConversationSummaryRow] = []

    async def upsert_summary(self, session_id: str, **kwargs: Any) -> None:
        self.upserts.append({"session_id": session_id, **kwargs})

    async def get_summary(self, session_id: str) -> ConversationSummaryRow | None:
        self.get_calls.append(session_id)
        return self.row

    async def list_summaries_for_user(
        self, user_id: str, *, limit: int
    ) -> list[ConversationSummaryRow]:
        self.list_calls.append((user_id, limit))
        return self.rows


class _FakeNestedTransaction:
    """Stands in for the SAVEPOINT `AsyncSession.begin_nested()` opens —
    real enough to prove the isolation property under test: whatever was
    `.add()`-ed to the session DURING this block is discarded if the
    block exits with an exception, and left alone otherwise. Never
    swallows the exception (`__aexit__` returns `False`, matching the
    real `begin_nested()` context manager) — the caller's own try/except
    is what's under test, not this double's."""

    def __init__(self, session: _FakeSession) -> None:
        self._session = session
        self._added_mark = 0

    async def __aenter__(self) -> _FakeNestedTransaction:
        self._added_mark = len(self._session.added)
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        if exc_type is not None:
            self._session.nested_rollbacks += 1
            del self._session.added[self._added_mark :]
        return False


class _FakeSession:
    def __init__(self, *, flush_error: BaseException | None = None) -> None:
        self.commits = 0
        self.added: list[Any] = []
        self.flush_count = 0
        self.nested_rollbacks = 0
        self._flush_error = flush_error

    async def commit(self) -> None:
        self.commits += 1

    def add(self, entity: Any) -> None:
        self.added.append(entity)

    async def flush(self) -> None:
        self.flush_count += 1
        if self._flush_error is not None:
            raise self._flush_error

    def begin_nested(self) -> _FakeNestedTransaction:
        return _FakeNestedTransaction(self)


class _FakeSessionFactory:
    def __init__(self, *, flush_error: BaseException | None = None) -> None:
        self.session = _FakeSession(flush_error=flush_error)
        self.opened = 0

    def __call__(self) -> _FakeSessionFactory:
        self.opened += 1
        return self

    async def __aenter__(self) -> _FakeSession:
        return self.session

    async def __aexit__(self, *exc_info: object) -> bool:
        return False


def _row(**overrides: Any) -> ConversationSummaryRow:
    values: dict[str, Any] = {
        "session_id": "a1f3c9",
        "user_id": "usr_rajesh01",
        "summary": CANONICAL.summary,
        "resolution": "pending",
        "intents": ["payment_failure", "limit_increase"],
        "tools_used": ["get_wallet_balance", "request_limit_increase"],
        "turn_count": 15,
        "duration_s": 283,
        "cost_usd": Decimal("0.2977"),
        "created_at": datetime(2026, 7, 24, 14, 34, tzinfo=UTC),
    }
    values.update(overrides)
    return ConversationSummaryRow(**values)


@pytest.fixture
def repo() -> _RecordingRepo:
    return _RecordingRepo()


@pytest.fixture
def factory() -> _FakeSessionFactory:
    return _FakeSessionFactory()


@pytest.fixture
def store(repo: _RecordingRepo, factory: _FakeSessionFactory) -> ConversationSummaryStore:
    return ConversationSummaryStore(
        cast("async_sessionmaker[AsyncSession]", factory),
        repo_factory=cast(
            "Any", lambda _session: cast("ConversationRepo", repo)
        ),
    )


# --------------------------------------------------------------------------
# save
# --------------------------------------------------------------------------


async def test_save_writes_every_column_docs09_pins(
    store: ConversationSummaryStore, repo: _RecordingRepo
) -> None:
    await store.save(CANONICAL)

    assert len(repo.upserts) == 1
    written = repo.upserts[0]
    assert written["session_id"] == "a1f3c9"
    assert written["user_id"] == "usr_rajesh01"
    assert written["summary"] == CANONICAL.summary
    assert written["resolution"] == "pending"
    assert written["turn_count"] == 15
    assert written["duration_s"] == 283


async def test_save_preserves_the_summary_text_exactly(
    store: ConversationSummaryStore, repo: _RecordingRepo
) -> None:
    """docs/09 §4.2's verbatim contract does not stop at the Redis field:
    the durable row is what the next call's semantic memory retrieves, so
    an amount mangled on the way to Postgres is an amount the agent
    misremembers weeks later."""
    await store.save(CANONICAL)

    stored = repo.upserts[0]["summary"]
    for anchor in ("₹245", "₹24,890", "₹25,000", "Amazon Business", "2:14 PM"):
        assert anchor in stored


async def test_save_binds_array_columns_as_lists(
    store: ConversationSummaryStore, repo: _RecordingRepo
) -> None:
    """The domain type is frozen and uses tuples; `ARRAY(Text)` binds a
    list. A tuple reaching the driver is the kind of thing that works on
    one dialect and not the next."""
    await store.save(CANONICAL)

    assert repo.upserts[0]["intents"] == ["payment_failure", "limit_increase"]
    assert repo.upserts[0]["tools_used"] == [
        "get_wallet_balance",
        "request_limit_increase",
    ]
    assert isinstance(repo.upserts[0]["intents"], list)


async def test_save_quantizes_cost_to_the_display_scale(
    store: ConversationSummaryStore, repo: _RecordingRepo
) -> None:
    """`conversation_summaries.cost_usd` is NUMERIC(8,4) — the rounded
    display copy against `call_costs`'s NUMERIC(10,6) ledger (docs/12
    §4.5). Quantizing here means the persisted value is the one this code
    computed, not whatever the driver would have rounded to."""
    await store.save(CANONICAL.model_copy(update={"cost_usd": Decimal("0.29775")}))

    assert repo.upserts[0]["cost_usd"] == Decimal("0.2978")
    assert repo.upserts[0]["cost_usd"].as_tuple().exponent == -4


async def test_save_commits_the_session_it_opened(
    store: ConversationSummaryStore, factory: _FakeSessionFactory
) -> None:
    """Repos never commit (`base.py`); whatever opened the session does.
    `save` opens it, so `save` commits it — same rule
    `CostTracker.finalize()` follows."""
    await store.save(CANONICAL)

    assert factory.opened == 1
    assert factory.session.commits == 1


async def test_save_can_be_repeated_for_the_documented_pipeline_retry(
    store: ConversationSummaryStore, repo: _RecordingRepo, factory: _FakeSessionFactory
) -> None:
    """docs/09 §8: "A crash mid-pipeline is retried whole from the
    still-live Redis hash." A save that raised on the second attempt would
    make that documented retry impossible.

    This asserts the store's side (it re-issues the write and re-commits).
    That the write itself is an upsert rather than a duplicate-key insert
    is asserted at the SQL level in
    tests/data/repositories/test_repositories.py.
    """
    await store.save(CANONICAL)
    await store.save(CANONICAL)

    assert len(repo.upserts) == 2
    assert factory.session.commits == 2


# --------------------------------------------------------------------------
# get / list
# --------------------------------------------------------------------------


async def test_get_returns_none_before_the_pipeline_has_landed_the_row(
    store: ConversationSummaryStore, repo: _RecordingRepo
) -> None:
    repo.row = None

    assert await store.get("a1f3c9") is None
    assert repo.get_calls == ["a1f3c9"]


async def test_get_maps_the_row_back_to_the_frozen_domain_value(
    store: ConversationSummaryStore, repo: _RecordingRepo
) -> None:
    repo.row = _row()

    result = await store.get("a1f3c9")

    assert result is not None
    assert result.session_id == "a1f3c9"
    assert result.resolution is Resolution.PENDING
    assert result.intents == ("payment_failure", "limit_increase")
    assert result.tools_used == ("get_wallet_balance", "request_limit_increase")
    assert result.created_at == datetime(2026, 7, 24, 14, 34, tzinfo=UTC)
    assert result.model_config["frozen"] is True


async def test_get_rejects_a_resolution_outside_the_documented_set(
    store: ConversationSummaryStore, repo: _RecordingRepo
) -> None:
    """The DB CHECK constrains the column, but a row read back is still
    just a string until Pydantic says otherwise — and this store is
    reachable against a database whose CHECK a future migration could
    widen."""
    repo.row = _row(resolution="half_resolved")

    with pytest.raises(ValueError, match="resolution"):
        await store.get("a1f3c9")


async def test_list_for_user_scopes_the_query_by_user_id(
    store: ConversationSummaryStore, repo: _RecordingRepo
) -> None:
    """docs/09 §6.2: cross-merchant reachability is a security invariant.
    The scoping is the repo's WHERE clause, so what this asserts is that
    the store passes the caller's `user_id` through unmodified and adds no
    widening of its own — the clause itself is asserted in
    tests/data/repositories/test_repositories.py."""
    repo.rows = [_row()]

    result = await store.list_for_user("usr_rajesh01", limit=3)

    assert repo.list_calls == [("usr_rajesh01", 3)]
    assert len(result) == 1
    assert result[0].user_id == "usr_rajesh01"


async def test_list_for_user_is_bounded_by_default(
    store: ConversationSummaryStore, repo: _RecordingRepo
) -> None:
    """An uncapped read of a merchant's whole call history has no caller
    and would be a slot-budget hazard if one appeared."""
    await store.list_for_user("usr_rajesh01")

    assert repo.list_calls[0][1] == 10


async def test_list_for_user_returns_an_immutable_tuple(
    store: ConversationSummaryStore, repo: _RecordingRepo
) -> None:
    repo.rows = [_row(), _row(session_id="b2e4d1")]

    result = await store.list_for_user("usr_rajesh01")

    assert isinstance(result, tuple)
    assert [s.session_id for s in result] == ["a1f3c9", "b2e4d1"]


# --------------------------------------------------------------------------
# embed-and-insert (post-call-summary-profile-wiring task, judgment call #3)
# --------------------------------------------------------------------------


class _RaisingEmbeddings:
    """`EmbeddingProvider`-shaped double whose `embed()` always raises —
    for proving the isolation contract without a real HTTP failure."""

    async def embed(self, texts: list[str]) -> list[tuple[float, ...]]:
        raise RuntimeError("embeddings provider unreachable")


def _fixture_settings() -> Settings:
    # Pydantic coerces plain str to SecretStr at construction, but mypy
    # types the *parameter* as SecretStr — the same mismatch
    # tests/conftest.py's settings_factory and scripts/test_seed_kb.py's
    # own _fixture_settings() already carry an ignore for.
    return Settings(
        jwt_secret="test-jwt-secret",
        database_url="postgresql+asyncpg://test:test@localhost/test",
        openrouter_api_key="test-openrouter-key",
    )  # type: ignore[arg-type]


def test_constructor_requires_embeddings_and_settings_together() -> None:
    """Judgment call #3: a half-configured pair is a wiring bug, caught
    at construction rather than at the first `save()`."""
    with pytest.raises(ValueError, match="together"):
        ConversationSummaryStore(
            cast("async_sessionmaker[AsyncSession]", lambda: None),
            embeddings=cast("Any", FakeEmbeddings()),
        )
    with pytest.raises(ValueError, match="together"):
        ConversationSummaryStore(
            cast("async_sessionmaker[AsyncSession]", lambda: None),
            settings=_fixture_settings(),
        )


async def test_save_embeds_and_inserts_a_memory_chunk_when_a_provider_is_configured(
    repo: _RecordingRepo, factory: _FakeSessionFactory
) -> None:
    embeddings = FakeEmbeddings()
    embeddings.script(tuple([0.5] * EMBEDDING_DIM))
    store = ConversationSummaryStore(
        cast("async_sessionmaker[AsyncSession]", factory),
        repo_factory=cast("Any", lambda _session: cast("ConversationRepo", repo)),
        embeddings=cast("Any", embeddings),
        settings=_fixture_settings(),
    )

    await store.save(CANONICAL)

    assert embeddings.calls == [[CANONICAL.summary]]
    (chunk,) = factory.session.added
    assert isinstance(chunk, MemoryChunkRow)
    assert chunk.kind == "call_summary"
    assert chunk.source_id == CANONICAL.session_id
    assert chunk.content == CANONICAL.summary
    assert chunk.user_id == CANONICAL.user_id
    assert chunk.embedding == [0.5] * EMBEDDING_DIM
    assert factory.session.commits == 1  # one commit covers both writes
    assert factory.session.nested_rollbacks == 0


async def test_save_skips_the_embed_step_when_no_provider_is_configured(
    store: ConversationSummaryStore, factory: _FakeSessionFactory
) -> None:
    """The default (no `embeddings`/`settings`) — every other test in
    this file already exercises this path implicitly; asserted directly
    here as the documented baseline judgment call #3 describes."""
    await store.save(CANONICAL)

    assert factory.session.added == []
    assert factory.session.commits == 1


async def test_save_commits_the_summary_row_even_when_the_embed_call_itself_raises(
    repo: _RecordingRepo, factory: _FakeSessionFactory
) -> None:
    """THE test: an embeddings provider that raises before ever touching
    the database must not prevent the summary row from committing. This
    is docs/09 §8's "an embedding outage queues the memory_chunks insert
    for retry without blocking the summary row" — the blocking half,
    proven end to end through `save()`."""
    store = ConversationSummaryStore(
        cast("async_sessionmaker[AsyncSession]", factory),
        repo_factory=cast("Any", lambda _session: cast("ConversationRepo", repo)),
        embeddings=cast("Any", _RaisingEmbeddings()),
        settings=_fixture_settings(),
    )

    with capture_logs() as logs:
        await store.save(CANONICAL)  # must not raise

    assert len(repo.upserts) == 1  # the summary row's own write still happened
    assert repo.upserts[0]["session_id"] == CANONICAL.session_id
    assert factory.session.added == []  # nothing landed in memory_chunks
    assert factory.session.commits == 1  # the summary write's commit still ran
    assert any(e["event"] == "conversation_summary_store.embed_failed_skipped" for e in logs)


async def test_save_commits_the_summary_row_even_when_the_chunk_insert_fails_at_flush(
    repo: _RecordingRepo,
) -> None:
    """The stronger proof a bare try/except cannot give: the embeddings
    call SUCCEEDS and the chunk genuinely reaches `session.add()`, but
    the DB-level write (`flush()`, standing in for a real constraint
    violation) fails — a SAVEPOINT is what stops that from poisoning the
    surrounding transaction and taking the already-written summary row
    down with it. A bare try/except around the insert would look
    identical to the previous test on this path only by accident."""
    factory = _FakeSessionFactory(flush_error=RuntimeError("constraint violation"))
    embeddings = FakeEmbeddings()
    embeddings.script(tuple([0.25] * EMBEDDING_DIM))
    store = ConversationSummaryStore(
        cast("async_sessionmaker[AsyncSession]", factory),
        repo_factory=cast("Any", lambda _session: cast("ConversationRepo", repo)),
        embeddings=cast("Any", embeddings),
        settings=_fixture_settings(),
    )

    await store.save(CANONICAL)  # must not raise

    assert len(repo.upserts) == 1
    # The chunk was add()-ed, then the SAVEPOINT rolled it back on the
    # flush failure — nothing survives in the session's added list.
    assert factory.session.added == []
    assert factory.session.nested_rollbacks == 1
    assert factory.session.commits == 1
