"""Unit tests for `scripts/seed_kb.py` (docs/09-memory-architecture.md
§6.1, docs/17-roadmap.md §2.5).

No Docker Desktop is available in this environment, and no live OpenAI key
is configured either — the needs-human ledger's H1/H2 items — so, matching
`tests/scripts/test_seed.py`'s own established approach, these tests never
touch a real Postgres or make a real embeddings HTTP call. Two kinds of
double are used:

- `_FakeKbSession`, a small in-memory stand-in for `AsyncSession` covering
  exactly the operations `seed_kb.py` calls (`get` by primary key, `add`,
  a Core `delete()` statement, `flush`, `commit`) — enough to drive
  `seed_kb()` across two runs and prove idempotency and `--reset` end to
  end, which a fully mocked, stateless `AsyncMock` cannot do on its own
  (there would be nothing for a second call's `session.get` to find).
- `tests.fakes.FakeEmbeddings`, the existing scriptable `EmbeddingProvider`
  double every other Phase-5 memory test already uses.

The retrieval-ranking test near the bottom drives real `SemanticRepo.
search()` (mocked `AsyncSession`, exactly `tests/data/repositories/
test_semantic_repo.py`'s own pattern) against a deterministic, dependency-
free bag-of-words embedding function — real cosine geometry computed in
this test file, not a hand-picked mock distance — since neither
`test_semantic_repo.py` nor `tests/memory/test_semantic_memory.py` had a
reusable "realistic fake embedding" helper to import (checked before
writing this one).
"""

from __future__ import annotations

import hashlib
import math
from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.data.repositories.semantic_repo import SemanticRepo
from app.domain.types import EMBEDDING_DIM, MemoryKind, SessionUser
from app.memory.semantic import SemanticMemory
from app.models.orm import KbArticle as KbArticleRow
from app.models.orm import MemoryChunk as MemoryChunkRow
from scripts import seed_kb as seed_kb_module
from scripts.kb_chunker import chunk_article
from scripts.kb_content import ALL_ARTICLES, ALLOWED_CATEGORIES
from scripts.kb_content import devices as devices_module
from scripts.kb_content import kyc as kyc_module
from scripts.kb_content import limits as limits_module
from scripts.kb_content.types import ArticleSpec
from tests.fakes import FakeEmbeddings

PRINCIPAL = SessionUser(user_id="usr_rajesh01")


# --------------------------------------------------------------------------
# _FakeKbSession — in-memory AsyncSession stand-in
# --------------------------------------------------------------------------


class _FakeKbSession:
    """Covers exactly the `AsyncSession` surface `scripts/seed_kb.py`'s
    functions call. `execute()` only ever receives one of the two Core
    `delete()` statements this module issues (never a `select`), so it is
    resolved by compiling the statement to SQL text and checking which
    table it targets — the same compile-then-inspect approach
    `tests/scripts/test_seed.py` already uses for `scripts/seed.py`'s own
    reset deletes.
    """

    def __init__(self) -> None:
        self.kb_articles: dict[str, KbArticleRow] = {}
        self.memory_chunks: list[MemoryChunkRow] = []
        self.flush_count = 0
        self.commit_count = 0

    async def get(self, model: type, pk: Any) -> Any:
        if model is KbArticleRow:
            return self.kb_articles.get(pk)
        raise AssertionError(f"_FakeKbSession.get() called with unexpected model {model!r}")

    def add(self, entity: Any) -> None:
        if isinstance(entity, KbArticleRow):
            self.kb_articles[entity.slug] = entity
        elif isinstance(entity, MemoryChunkRow):
            self.memory_chunks.append(entity)
        else:
            raise AssertionError(f"_FakeKbSession.add() called with unexpected entity {entity!r}")

    async def flush(self) -> None:
        self.flush_count += 1

    async def commit(self) -> None:
        self.commit_count += 1

    async def execute(self, statement: Any) -> None:
        compiled = str(statement.compile(dialect=postgresql.dialect()))
        if "memory_chunks" in compiled:
            self.memory_chunks = []
        elif "kb_articles" in compiled:
            self.kb_articles = {}
        else:
            raise AssertionError(
                f"_FakeKbSession.execute() saw an unrecognized statement: {compiled}"
            )


def _fixture_settings() -> Settings:
    return Settings(
        jwt_secret="test-jwt-secret",
        database_url="postgresql+asyncpg://test:test@localhost/test",
        openrouter_api_key="test-openrouter-key",
        openai_api_key="test-openai-key",
        openai_base_url="https://openai.test/v1",
    )  # type: ignore[call-arg]


def _spec(
    slug: str = "kb_test_article",
    body_md: str | None = None,
    category: str = "limits",
) -> ArticleSpec:
    return ArticleSpec(
        slug=slug,
        title="Test article",
        category=category,
        body_md=body_md or "## A heading\n\nA short body under any token budget.",
    )


# --------------------------------------------------------------------------
# Idempotency: re-running seed_kb() does not duplicate rows
# --------------------------------------------------------------------------


async def test_first_run_inserts_a_new_article() -> None:
    session = _FakeKbSession()
    summary = await seed_kb_module.seed_kb(
        session, _fixture_settings(), FakeEmbeddings(), articles=(_spec(),)
    )

    assert summary.articles_inserted == 1
    assert summary.articles_updated == 0
    assert summary.articles_unchanged == 0
    assert list(session.kb_articles) == ["kb_test_article"]


async def test_rerunning_with_unchanged_content_does_not_duplicate_the_article() -> None:
    session = _FakeKbSession()
    spec = _spec()

    await seed_kb_module.seed_kb(session, _fixture_settings(), FakeEmbeddings(), articles=(spec,))
    summary2 = await seed_kb_module.seed_kb(
        session, _fixture_settings(), FakeEmbeddings(), articles=(spec,)
    )

    assert summary2.articles_inserted == 0
    assert summary2.articles_updated == 0
    assert summary2.articles_unchanged == 1
    # Still exactly one row for this slug, never two.
    assert list(session.kb_articles) == ["kb_test_article"]


async def test_rerunning_does_not_accumulate_duplicate_chunks() -> None:
    """The derived table is rebuilt (delete + re-add) every run by design
    (see seed_kb.py's module docstring), so the chunk count after a
    second run must equal the first run's, never grow."""
    session = _FakeKbSession()
    spec = _spec()

    await seed_kb_module.seed_kb(session, _fixture_settings(), FakeEmbeddings(), articles=(spec,))
    count_after_first = len(session.memory_chunks)

    await seed_kb_module.seed_kb(session, _fixture_settings(), FakeEmbeddings(), articles=(spec,))
    count_after_second = len(session.memory_chunks)

    assert count_after_first > 0
    assert count_after_first == count_after_second


async def test_upsert_updates_and_bumps_version_when_body_md_changes() -> None:
    session = _FakeKbSession()
    v1 = _spec(body_md="## H\n\nOriginal body text.")
    v2 = _spec(body_md="## H\n\nCompletely different body text now.")

    await seed_kb_module.seed_kb(session, _fixture_settings(), FakeEmbeddings(), articles=(v1,))
    assert session.kb_articles["kb_test_article"].version == 1

    summary = await seed_kb_module.seed_kb(
        session, _fixture_settings(), FakeEmbeddings(), articles=(v2,)
    )

    assert summary.articles_updated == 1
    assert summary.articles_unchanged == 0
    row = session.kb_articles["kb_test_article"]
    assert row.body_md == v2.body_md
    assert row.version == 2


async def test_upsert_does_not_bump_version_when_nothing_changed() -> None:
    session = _FakeKbSession()
    spec = _spec()

    await seed_kb_module.seed_kb(session, _fixture_settings(), FakeEmbeddings(), articles=(spec,))
    await seed_kb_module.seed_kb(session, _fixture_settings(), FakeEmbeddings(), articles=(spec,))

    assert session.kb_articles["kb_test_article"].version == 1


# --------------------------------------------------------------------------
# --reset: full wipe, including a slug no longer in the content list
# --------------------------------------------------------------------------


async def test_reset_wipes_kb_articles_and_kb_article_chunks_before_reseeding() -> None:
    session = _FakeKbSession()
    spec = _spec()
    await seed_kb_module.seed_kb(session, _fixture_settings(), FakeEmbeddings(), articles=(spec,))

    # Simulate an orphaned row — an article removed from ALL_ARTICLES since
    # the last seed run. A normal (non-reset) run must never touch it; a
    # --reset run must wipe it, since it fully owns both tables.
    session.kb_articles["kb_orphan"] = KbArticleRow(
        slug="kb_orphan", title="Orphan", body_md="## H\n\nX.", category="limits", version=1
    )

    await seed_kb_module.seed_kb(
        session, _fixture_settings(), FakeEmbeddings(), articles=(spec,), reset=True
    )

    assert set(session.kb_articles) == {"kb_test_article"}


async def test_non_reset_run_leaves_an_orphaned_article_row_untouched() -> None:
    """The mirror of the test above: without --reset, an article slug not
    present in `articles` is never deleted, only ones present are upserted."""
    session = _FakeKbSession()
    session.kb_articles["kb_orphan"] = KbArticleRow(
        slug="kb_orphan", title="Orphan", body_md="## H\n\nX.", category="limits", version=1
    )

    await seed_kb_module.seed_kb(
        session, _fixture_settings(), FakeEmbeddings(), articles=(_spec(),)
    )

    assert "kb_orphan" in session.kb_articles
    assert "kb_test_article" in session.kb_articles


# --------------------------------------------------------------------------
# The embed call is batched once, not once per chunk
# --------------------------------------------------------------------------


async def test_embed_is_called_once_for_the_whole_batch() -> None:
    embeddings = FakeEmbeddings()
    articles = (
        _spec(slug="kb_a", body_md="## One\n\nBody one.\n\n## Two\n\nBody two."),
        _spec(slug="kb_b", body_md="## Three\n\nBody three."),
    )
    session = _FakeKbSession()

    await seed_kb_module.seed_kb(session, _fixture_settings(), embeddings, articles=articles)

    assert len(embeddings.calls) == 1
    expected_chunk_count = sum(len(chunk_article(a.slug, a.body_md)) for a in articles)
    assert len(embeddings.calls[0]) == expected_chunk_count
    assert expected_chunk_count > 1  # otherwise "batched, not per-chunk" is untested


async def test_embed_is_called_once_even_when_the_batch_is_the_full_real_content() -> None:
    """The realistic-scale version: ~40 real authored articles chunk to
    ~170 pieces (see scripts/kb_content's own docstrings for the ~180
    ballpark from docs/09 §6.1), all pushed through embed() in one call."""
    embeddings = FakeEmbeddings()
    session = _FakeKbSession()

    await seed_kb_module.seed_kb(
        session, _fixture_settings(), embeddings, articles=ALL_ARTICLES
    )

    assert len(embeddings.calls) == 1
    assert len(embeddings.calls[0]) == len(session.memory_chunks)
    # Sanity range around docs/09 §6.1's own "~40 articles -> ~180 chunks"
    # estimate — not pinned to an exact count, which would make this test
    # brittle to a future one-line content edit.
    assert 140 <= len(session.memory_chunks) <= 210


# --------------------------------------------------------------------------
# Every produced row satisfies the ORM's CHECK constraints
# --------------------------------------------------------------------------


async def test_every_produced_row_satisfies_the_orm_check_constraints() -> None:
    session = _FakeKbSession()

    await seed_kb_module.seed_kb(
        session, _fixture_settings(), FakeEmbeddings(), articles=ALL_ARTICLES
    )

    assert session.kb_articles  # sanity: the run actually wrote something
    for row in session.kb_articles.values():
        # ck_kb_articles_category
        assert row.category in ALLOWED_CATEGORIES
        assert row.slug.strip() and row.title.strip() and row.body_md.strip()

    assert session.memory_chunks
    for chunk in session.memory_chunks:
        # ck_memory_chunks_kind
        assert chunk.kind == MemoryKind.KB_ARTICLE.value
        # ck_memory_chunks_user_scope + ck_memory_chunks_user_id_not_blank:
        # NULL for kb_article, never a blank string either.
        assert chunk.user_id is None
        assert chunk.content.strip()
        assert len(chunk.embedding) == EMBEDDING_DIM


async def test_category_validation_rejects_an_unknown_category() -> None:
    session = _FakeKbSession()
    bad = _spec(category="not_a_real_category")

    with pytest.raises(ValueError, match="not_a_real_category"):
        await seed_kb_module.seed_kb(
            session, _fixture_settings(), FakeEmbeddings(), articles=(bad,)
        )


# --------------------------------------------------------------------------
# SQL-level check: the delete inside _rebuild_chunks targets memory_chunks
# scoped to kind='kb_article', never an unscoped table wipe
# --------------------------------------------------------------------------


async def test_rebuild_chunks_delete_is_scoped_to_kb_article_kind() -> None:
    session = AsyncMock(spec=AsyncSession)
    settings = _fixture_settings()

    await seed_kb_module._rebuild_chunks(session, settings, FakeEmbeddings(), (_spec(),))

    delete_call = session.execute.await_args_list[0]
    compiled = delete_call.args[0].compile(dialect=postgresql.dialect())
    sql = str(compiled)
    assert "DELETE FROM memory_chunks" in sql
    assert "kind" in sql
    assert "kb_article" in compiled.params.values()


# --------------------------------------------------------------------------
# Retrieval proof: a query resembling the canonical incident retrieves a
# `limits`-category chunk, ranked above unrelated categories — via
# SemanticRepo.search() against fixture-inserted chunks and a
# fake-but-real deterministic embedding function.
# --------------------------------------------------------------------------


def _stable_hash_index(word: str, dim: int) -> int:
    """A stable (non-PYTHONHASHSEED-salted) hash -> index mapping.
    Python's builtin `hash()` is randomized per process for strings,
    which would make this test's ranking outcome flaky across runs even
    though it is deterministic *within* one run — sha256 keeps it
    reproducible everywhere, every time."""
    digest = hashlib.sha256(word.encode("utf-8")).digest()
    return int.from_bytes(digest[:4], "big") % dim


def _bow_embedding(text: str) -> tuple[float, ...]:
    """A deterministic, dependency-free "embedding": a feature-hashed bag
    of words, L2-normalized to unit length. This is real vector geometry
    — cosine similarity between two of these vectors genuinely reflects
    literal word overlap — not a hand-picked mock distance. It is *not* a
    claim that this behaves like a real semantic embedding model (it has
    no notion of synonymy or meaning beyond shared vocabulary), which is
    exactly why the test below asserts on ranking, not on clearing the
    real 0.70 similarity floor — see that test's own comment.
    """
    import re

    counts = [0.0] * EMBEDDING_DIM
    for word in re.findall(r"[a-z0-9]+", text.lower()):
        counts[_stable_hash_index(word, EMBEDDING_DIM)] += 1.0
    norm = math.sqrt(sum(v * v for v in counts))
    if norm == 0.0:
        return tuple(counts)
    return tuple(v / norm for v in counts)


def _cosine_similarity(a: tuple[float, ...], b: tuple[float, ...]) -> float:
    return sum(x * y for x, y in zip(a, b, strict=True))  # both already unit-normalized


class _RowStub:
    """Matches the six columns `SemanticRepo.search()` reads off each
    result row (id, kind, source_id, content, user_id, distance) — the
    same shape `tests/data/repositories/test_semantic_repo.py`'s own
    `_row` helper uses, reimplemented here to keep this test file
    self-contained rather than importing from a sibling test module."""

    def __init__(
        self,
        chunk_id: int,
        kind: str,
        source_id: str,
        content: str,
        user_id: str | None,
        distance: float,
    ) -> None:
        self.id = chunk_id
        self.kind = kind
        self.source_id = source_id
        self.content = content
        self.user_id = user_id
        self.distance = distance


def _make_search_session(rows: list[_RowStub]) -> AsyncMock:
    session = AsyncMock(spec=AsyncSession)
    # SemanticRepo.search() issues two SET LOCAL statements, then the
    # SELECT — see tests/data/repositories/test_semantic_repo.py.
    session.execute.side_effect = [None, None, rows]
    return session


async def test_search_ranks_a_limits_chunk_top_for_a_daily_limit_query(settings: Settings) -> None:
    """The task's own required proof: a query resembling the canonical
    incident (docs/01 §7-8: Rajesh, ₹245, DAILY_LIMIT_EXCEEDED) retrieves
    a chunk from the `limits` category content authored in this task,
    ranked above chunks from unrelated categories (kyc, devices).

    Uses SemanticRepo.search() against fixture-inserted chunks built from
    the real chunker over the real authored content, with a deterministic
    bag-of-words embedding (`_bow_embedding`) providing genuine cosine
    geometry rather than a hand-picked mock score.

    `floor=0.0` is passed to `SemanticMemory.retrieve()` deliberately: a
    feature-hashed bag-of-words vector is real geometry but not a real
    semantic embedding, so its absolute cosine similarity has no reason
    to clear the production 0.70 floor calibrated for `text-embedding-3-
    small`. What this test proves is the *ranking* — the limits chunk
    scores higher than the unrelated ones — which is exactly the claim
    "this query would retrieve a limits chunk" makes, without overclaiming
    that a toy hash embedding reproduces a real model's absolute scores.
    """
    limits_article = next(
        a for a in limits_module.ARTICLES if a.slug == "kb_limits_daily_limit_exceeded"
    )
    kyc_article = next(a for a in kyc_module.ARTICLES if a.slug == "kb_kyc_overview")
    devices_article = next(a for a in devices_module.ARTICLES if a.slug == "kb_devices_tracking")

    limits_chunk = chunk_article(limits_article.slug, limits_article.body_md)[0]
    kyc_chunk = chunk_article(kyc_article.slug, kyc_article.body_md)[0]
    devices_chunk = chunk_article(devices_article.slug, devices_article.body_md)[0]

    query = "why did my payment fail with a daily limit error"
    query_vec = _bow_embedding(query)

    candidates = [
        (1, limits_chunk.source_id, limits_chunk.text),
        (2, kyc_chunk.source_id, kyc_chunk.text),
        (3, devices_chunk.source_id, devices_chunk.text),
    ]
    rows = [
        _RowStub(
            chunk_id=chunk_id,
            kind="kb_article",
            source_id=source_id,
            content=content,
            user_id=None,
            distance=1.0 - _cosine_similarity(query_vec, _bow_embedding(content)),
        )
        for chunk_id, source_id, content in candidates
    ]
    # A real `ORDER BY distance` would hand these back nearest-first —
    # the mock session stands in for the database, so it must too.
    rows.sort(key=lambda r: r.distance)

    embeddings = FakeEmbeddings()
    embeddings.script(query_vec)
    session = _make_search_session(rows)
    memory = SemanticMemory(embeddings, SemanticRepo(session, settings))

    results = await memory.retrieve(query, PRINCIPAL, floor=0.0)

    assert results
    assert results[0].source_id == "kb_limits_daily_limit_exceeded"
    assert results[0].kind is MemoryKind.KB_ARTICLE
    # Ranked strictly above both unrelated-category candidates, not merely
    # present somewhere in the result set.
    other_slugs = {r.source_id for r in results[1:]}
    assert "kb_limits_daily_limit_exceeded" not in other_slugs


# --------------------------------------------------------------------------
# CLI wiring parity with scripts/seed.py's own guards
# --------------------------------------------------------------------------


def test_parse_args_defaults_reset_to_false() -> None:
    args = seed_kb_module._parse_args([])
    assert args.reset is False


def test_parse_args_reset_flag() -> None:
    args = seed_kb_module._parse_args(["--reset"])
    assert args.reset is True


class _FakeSettings:
    def __init__(self, env: str = "dev") -> None:
        self.env = env


@pytest.mark.parametrize("env", ["prod", "production", "staging"])
async def test_amain_refuses_reset_outside_dev_test(
    monkeypatch: pytest.MonkeyPatch, env: str
) -> None:
    monkeypatch.setattr(seed_kb_module, "get_settings", lambda: _FakeSettings(env=env))
    engine_and_sessionmaker_called = AsyncMock()
    monkeypatch.setattr(
        seed_kb_module,
        "create_engine_and_sessionmaker",
        lambda settings: engine_and_sessionmaker_called(),
    )

    with pytest.raises(SystemExit, match=env):
        await seed_kb_module._amain(["--reset"])

    engine_and_sessionmaker_called.assert_not_called()
