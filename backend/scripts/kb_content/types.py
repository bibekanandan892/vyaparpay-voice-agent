"""Shared shape for the seeded support-KB content (docs/09-memory-
architecture.md §6.1, docs/17-roadmap.md §2.5) — the plain data every
`scripts/kb_content/<category>.py` module exports a tuple of, and that
`scripts/seed_kb.py` upserts into `kb_articles`.

Kept separate from `scripts/kb_content/__init__.py` (rather than defining
`ArticleSpec` there) so a category module can `from scripts.kb_content.types
import ArticleSpec` without importing the aggregator — the aggregator
imports every category module, so the reverse direction would be a cycle.

This is deliberately a plain frozen dataclass, not
`app.domain.types.KbArticle`: the domain type carries fields (`version`,
`updated_at`) this content has no opinion on — those are the seed script's
job to assign — and `category` there is an unchecked `str` (its own
docstring says so). `ALLOWED_CATEGORIES` below is this package's own
allowlist, checked by `tests/scripts/test_kb_content.py` against exactly
`kb_articles`' CHECK constraint (`ck_kb_articles_category`,
app/models/orm.py) so a typo'd category fails a fast content test instead
of a Postgres CHECK violation nobody runs in this environment (no Docker,
per the H2 needs-human ledger item).
"""

from __future__ import annotations

from dataclasses import dataclass

# Mirrors `ck_kb_articles_category` in app/models/orm.py exactly. Duplicated
# rather than imported from the ORM module on purpose: this package
# (scripts/kb_content/) is pure content with no dependency on `app.models`,
# and the two are cheap to keep in sync — a mismatch fails
# `test_kb_content.py`'s `test_every_article_category_is_in_the_kb_articles_check_constraint`
# loudly rather than drifting silently.
ALLOWED_CATEGORIES: frozenset[str] = frozenset(
    {"limits", "settlements", "refunds", "devices", "kyc"}
)


@dataclass(frozen=True)
class ArticleSpec:
    """One authored KB article, before it becomes a `KbArticle` row.

    `slug` is the `kb_articles` primary key and also `memory_chunks.source_id`
    for every chunk derived from this article (docs/09 §6.1) — it must be
    stable across re-seeds, since it is what makes the upsert-by-slug
    idempotency design possible (see `scripts/seed_kb.py`).

    `body_md` is heading-first by convention: every article in this package
    starts directly with a `##` heading (no top-level `#` — the article
    `title` already carries that role, stored separately in `kb_articles.
    title`) so `scripts/kb_chunker.chunk_article`'s heading-aware split has
    no untitled preamble to special-case in the common case. The chunker
    still handles a headingless preamble correctly (tested), for content
    that doesn't follow this convention.
    """

    slug: str
    title: str
    category: str
    body_md: str


__all__ = ["ALLOWED_CATEGORIES", "ArticleSpec"]
