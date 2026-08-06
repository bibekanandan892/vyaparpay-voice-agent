"""Seeds the support knowledge base (docs/09-memory-architecture.md §6.1,
docs/17-roadmap.md §2.5) — the RAG retrieval layer built in Phase-5 T2a
(`SemanticRepo.search()`, `SemanticMemory.retrieve()`) has had nothing to
retrieve until this script runs: `kb_articles` and the `kind='kb_article'`
slice of `memory_chunks` are empty in every environment until it does.

**Why a separate script from `scripts/seed.py`.** That script owns the
canonical demo *actor* fixtures — Rajesh, his wallet, the one declined
transaction — pinned exact facts every worked example in the doc set cites
verbatim. This script owns ~40 pieces of support-KB *content*: closer to
static site copy than to pinned demo facts, versioned and re-chunked
independently, with its own re-run semantics (see `seed_kb`'s docstring).
Merging the two would tie two unrelated concerns to one `--reset` flag and
one idempotency story; splitting them costs one more `python -m
scripts.seed_kb` invocation in the deploy runbook and buys a script whose
diff is legible on its own.

**Idempotency design.** `kb_articles` is upserted by `slug`: a new slug
inserts, an existing slug whose `title`/`body_md`/`category` differ updates
in place and bumps `version`, and an unchanged slug is a no-op — matching
`app.models.orm.KbArticle`'s own docstring: "`kb_articles` itself never
changes" except through exactly this kind of content update. `memory_chunks`
(`kind='kb_article'`) is **always** deleted and rebuilt from scratch on
every run, reset or not — per that same docstring's stated rebuild pattern
("`DELETE WHERE kind = 'kb_article'` plus a re-run of the seed embedder")
and per this task's own guidance: diffing individual chunks against a
changed article is exactly the complexity that rebuild pattern exists to
avoid. At the corpus's demo scale (~40 articles, ~170-200 chunks) a full
re-embed on every run costs one batched HTTP call, not a meaningful delay
or expense — see `EmbeddingProvider.embed`'s own docstring on being
"batched by construction" for exactly this workload.

`--reset` additionally wipes `kb_articles` itself before reseeding (see
`reset_kb_rows`), which the normal path does not: a normal run only
touches slugs present in `scripts.kb_content.ALL_ARTICLES`, so an article
removed from that list would otherwise linger forever. `--reset` is the
"start from an empty table" escape hatch, mirroring `scripts/seed.py`'s own
`--reset` semantics and its dev/test-only guard.

Run as `python -m scripts.seed_kb [--reset]` from `backend/`, matching
`scripts/seed.py`'s own invocation convention (`scripts/__init__.py`).

**Scope note (task honesty contract, not a code decision):** this script
is written to run for real the moment a human points it at a live
Postgres with pgvector and a real `OPENAI_API_KEY` — but this development
environment has neither (no Docker daemon, no live OpenAI key; the
needs-human ledger's H1/H2 items), so it has only ever been exercised here
against the mocks/fakes in `tests/scripts/test_seed_kb.py`.
"""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Sequence
from dataclasses import dataclass

import httpx
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.data import create_engine_and_sessionmaker
from app.data.repositories import SemanticRepo
from app.domain.interfaces import EmbeddingProvider
from app.domain.types import KbArticle as KbArticleDomain
from app.domain.types import MemoryChunk as MemoryChunkDomain
from app.domain.types import MemoryKind

# Aliased per app/models/orm.py's own module docstring: these ORM classes
# share a name with the domain twins imported above, and a plain double
# import would silently shadow the first.
from app.models.orm import KbArticle as KbArticleRow
from app.models.orm import MemoryChunk as MemoryChunkRow
from app.providers import OpenAIEmbeddings
from scripts.kb_chunker import chunk_article
from scripts.kb_content import ALL_ARTICLES, ALLOWED_CATEGORIES, ArticleSpec

_RESET_ALLOWED_ENVS = frozenset({"dev", "test"})


@dataclass(frozen=True)
class KbSeedSummary:
    """What actually happened on this run, in the same spirit as
    `scripts/seed.py`'s `SeedSummary` — printed to the terminal so a run
    plainly states how many articles were freshly written vs. already
    present, and how many chunks the rebuild produced.
    """

    articles_inserted: int
    articles_updated: int
    articles_unchanged: int
    chunks_written: int

    def render(self) -> str:
        lines = [
            "VyaparPay support KB (docs/09 §6.1):",
            f"  kb_articles:   {self.articles_inserted} inserted, "
            f"{self.articles_updated} updated, {self.articles_unchanged} unchanged",
            f"  memory_chunks: {self.chunks_written} chunks written (kind=kb_article)",
        ]
        return "\n".join(lines)


def _validate_category(category: str) -> None:
    """`app.domain.types.KbArticle.category` is an unchecked `str` (its
    own docstring says so — the allowlist is DB-CHECK-only). Checked here
    instead, against this package's own mirror of that CHECK
    (`scripts.kb_content.ALLOWED_CATEGORIES`), so a typo'd category in
    authored content fails this script with a clear message instead of
    reaching a Postgres CHECK violation this environment cannot even
    trigger (no Docker)."""
    if category not in ALLOWED_CATEGORIES:
        raise ValueError(
            f"KB article category {category!r} is not one of {sorted(ALLOWED_CATEGORIES)} — "
            "mirrors ck_kb_articles_category (app/models/orm.py)"
        )


async def _upsert_article(session: AsyncSession, spec: ArticleSpec) -> str:
    """Insert-or-update one `kb_articles` row by `slug`. Returns
    `"inserted"`, `"updated"`, or `"unchanged"`.

    Validated through `app.domain.types.KbArticle` before touching the
    ORM at all — catches a malformed `ArticleSpec` at the content
    boundary rather than at a DB constraint this environment cannot run.
    """
    _validate_category(spec.category)
    KbArticleDomain(slug=spec.slug, title=spec.title, body_md=spec.body_md, category=spec.category)

    existing = await session.get(KbArticleRow, spec.slug)
    if existing is None:
        session.add(
            KbArticleRow(
                slug=spec.slug,
                title=spec.title,
                body_md=spec.body_md,
                category=spec.category,
                # Explicit, not left to the column's `server_default="1"`:
                # a server default only takes effect once a real Postgres
                # actually processes the INSERT, so the in-memory object
                # this function just built would otherwise carry
                # `version=None` until a real flush against a real
                # database — never true in this environment (no Docker),
                # and a needless DB round-trip dependency for a value
                # this script already knows.
                version=1,
            )
        )
        return "inserted"

    changed = (
        existing.title != spec.title
        or existing.body_md != spec.body_md
        or existing.category != spec.category
    )
    if not changed:
        return "unchanged"

    # Direct attribute mutation on the session-tracked entity, matching
    # this codebase's established update idiom for a fetched ORM row
    # (PaymentRepo.record_daily_usage, LimitRepo.submit_increase_request)
    # rather than build-a-new-instance-and-session.merge(): `existing` is
    # already attached to this session's identity map, and SQLAlchemy's
    # unit-of-work tracks the mutation for the next flush.
    existing.title = spec.title
    existing.body_md = spec.body_md
    existing.category = spec.category
    existing.version += 1
    return "updated"


async def _rebuild_chunks(
    session: AsyncSession,
    settings: Settings,
    embeddings: EmbeddingProvider,
    articles: Sequence[ArticleSpec],
) -> int:
    """The derived-table rebuild `app.models.orm.KbArticle`'s own
    docstring names: delete every existing `kind='kb_article'` chunk, then
    re-chunk and re-embed every article in `articles` from scratch.
    Returns the number of chunks written.

    Always runs — every seed invocation, not only `--reset` — per this
    module's docstring: `kb_articles` rows persist across re-chunks by
    design, and diffing individual chunks against a changed article is the
    complexity the documented rebuild pattern exists to avoid.
    """
    await session.execute(
        delete(MemoryChunkRow).where(MemoryChunkRow.kind == MemoryKind.KB_ARTICLE.value)
    )
    await session.flush()

    chunks = [
        chunk for article in articles for chunk in chunk_article(article.slug, article.body_md)
    ]
    if not chunks:
        return 0

    # One batched call for every chunk across every article — never one
    # call per chunk. See EmbeddingProvider.embed's own docstring: "the
    # seed embedder pushes ~180 KB chunks through here in one pass."
    vectors = await embeddings.embed([chunk.text for chunk in chunks])
    if len(vectors) != len(chunks):
        raise ValueError(
            f"embeddings.embed returned {len(vectors)} vectors for {len(chunks)} chunks — "
            "EmbeddingProvider's contract requires positional alignment "
            "(app/domain/interfaces.py)"
        )

    repo = SemanticRepo(session, settings)
    for chunk, vector in zip(chunks, vectors, strict=True):
        # Validated through the domain type first (embedding width, the
        # kb_article/user_id=NULL biconditional) for the same reason
        # _upsert_article validates through KbArticleDomain above — a
        # safety net this environment cannot get from a live DB CHECK.
        MemoryChunkDomain(
            kind=MemoryKind.KB_ARTICLE,
            source_id=chunk.source_id,
            content=chunk.text,
            embedding=vector,
            user_id=None,
        )
        # SemanticRepo(session).add(...) rather than a raw session.add():
        # SemanticRepo is the one class app/ treats as the sole reader of
        # memory_chunks (tests/data/repositories/test_semantic_repo.py),
        # and it already inherits a usable add() from
        # SqlAlchemyRepository[MemoryChunk] — reusing it keeps one writer
        # entrypoint for this table even though this script sits outside
        # the app/ tree the reader lint scans (backend/scripts/, not
        # backend/app/) and so isn't itself covered by that guard.
        await repo.add(
            MemoryChunkRow(
                kind=MemoryKind.KB_ARTICLE.value,
                source_id=chunk.source_id,
                content=chunk.text,
                embedding=list(vector),
                user_id=None,
            )
        )
    return len(chunks)


async def reset_kb_rows(session: AsyncSession) -> None:
    """`--reset`: wipe both the derived chunks and the source-of-record
    articles. Chunks first, articles second — `memory_chunks` carries no
    FK to `kb_articles` (`source_id` is polymorphic and deliberately
    unconstrained, `app/models/orm.py`), so the order has no referential-
    integrity consequence, but child-before-parent is kept anyway to
    mirror the convention `scripts/seed.py`'s own `reset_seed_rows`
    established.

    Unlike that sibling script's reset, this is a full-table wipe of both
    tables, not scoped to a specific set of slugs: `kb_articles` and its
    `kind='kb_article'` chunks are wholly owned by this script — nothing
    else in the app ever writes them (docs/09 §6.1) — so there is no
    other data in either table a scoped delete would need to protect.
    """
    await session.execute(
        delete(MemoryChunkRow).where(MemoryChunkRow.kind == MemoryKind.KB_ARTICLE.value)
    )
    await session.execute(delete(KbArticleRow))
    await session.flush()


async def seed_kb(
    session: AsyncSession,
    settings: Settings,
    embeddings: EmbeddingProvider,
    *,
    articles: Sequence[ArticleSpec] = ALL_ARTICLES,
    reset: bool = False,
) -> KbSeedSummary:
    """Entry point for callers already holding an `AsyncSession` and an
    `EmbeddingProvider` (this module's own CLI wiring below, and tests).
    `reset` and the subsequent upsert-and-rebuild share this one
    session/transaction, so a failure partway through never leaves the KB
    half-erased — matching `scripts/seed.py`'s `seed()` docstring's same
    guarantee for the actor fixtures.

    `articles` defaults to the full authored set
    (`scripts.kb_content.ALL_ARTICLES`) but is overridable — the seam
    `tests/scripts/test_seed_kb.py` uses to drive this against a handful
    of fixture articles instead of all ~40.
    """
    if reset:
        await reset_kb_rows(session)

    inserted = updated = unchanged = 0
    for spec in articles:
        outcome = await _upsert_article(session, spec)
        if outcome == "inserted":
            inserted += 1
        elif outcome == "updated":
            updated += 1
        else:
            unchanged += 1

    chunks_written = await _rebuild_chunks(session, settings, embeddings, articles)
    await session.commit()
    return KbSeedSummary(
        articles_inserted=inserted,
        articles_updated=updated,
        articles_unchanged=unchanged,
        chunks_written=chunks_written,
    )


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.seed_kb",
        description="Seed the support knowledge base (docs/09 §6.1, docs/17 §2.5).",
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help="Wipe kb_articles and kind='kb_article' memory_chunks first, then reseed fresh.",
    )
    return parser.parse_args(argv)


async def _amain(argv: Sequence[str] | None = None) -> None:
    args = _parse_args(argv)
    settings = get_settings()
    if args.reset and settings.env not in _RESET_ALLOWED_ENVS:
        # Same defense-in-depth rationale as scripts/seed.py's own guard:
        # the deletes here are already scoped to this script's own two
        # tables, never a wildcard across the schema, but there is
        # otherwise no rail against --reset running with whatever
        # DATABASE_URL happens to be in the environment.
        raise SystemExit(
            f"--reset refused: settings.env={settings.env!r} is not one of "
            f"{sorted(_RESET_ALLOWED_ENVS)} — this only runs against dev/test "
            "databases by design."
        )
    engine, sessionmaker = create_engine_and_sessionmaker(settings)
    http = httpx.AsyncClient()
    try:
        embeddings = OpenAIEmbeddings(http, settings)
        async with sessionmaker() as session:
            summary = await seed_kb(session, settings, embeddings, reset=args.reset)
        print(summary.render())
    finally:
        await http.aclose()
        await engine.dispose()


def main(argv: Sequence[str] | None = None) -> None:
    asyncio.run(_amain(argv))


if __name__ == "__main__":
    main()
