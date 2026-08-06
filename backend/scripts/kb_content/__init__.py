"""The seeded support-KB content (docs/09-memory-architecture.md §6.1,
docs/17-roadmap.md §2.5) — 40 articles across the 5 `kb_articles.category`
values, authored as Python data rather than a directory of `.md` files the
seed script would have to read and parse at runtime.

**Why Python constants, not a data file.** `scripts/seed.py` (the sibling
seed script for the demo actor fixtures) already establishes this
codebase's convention for seed-time content: named constants, not a
side-channel file, so the values a script promises to write are visible in
the same diff as the script that writes them and importable directly by
its tests (`tests/scripts/test_seed.py` asserts against `seed_module.
MERCHANT_ID` the same way `tests/scripts/test_kb_content.py` asserts
against `ALL_ARTICLES` here). A `.md`-per-article directory would need its
own frontmatter parser (category, slug) invented for this one script, for
content that never needs runtime editing without a code change anyway —
KB content is versioned in git and re-chunked, not hot-edited by a
non-engineer, so the usual argument for external content files (non-engineer
editability) doesn't apply here.

**Why 5 category modules, not one file.** 40 articles' markdown bodies is
several hundred lines; one `kb_content.py` would blow past this codebase's
own 800-line file ceiling (~/.claude/rules/ecc/common/coding-style.md) and
mix five unrelated topics in one diff. Splitting by category (the same
axis `kb_articles.category`'s CHECK constraint already carves the table on)
keeps each file focused, and each category module's own docstring records
which facts in it are grounded in canon vs. invented for internal
consistency.
"""

from __future__ import annotations

from scripts.kb_content import devices, kyc, limits, refunds, settlements
from scripts.kb_content.types import ALLOWED_CATEGORIES, ArticleSpec

# Category order matches docs/09 §6.1's listing (limits, settlements,
# refunds, devices, kyc); within a category, articles stay in the order
# their own module defines them. Order has no functional consequence for
# the seed script (upsert is keyed by slug, not position) — it only makes
# `ALL_ARTICLES[i]` a stable, predictable read for anyone skimming it.
ALL_ARTICLES: tuple[ArticleSpec, ...] = (
    limits.ARTICLES + settlements.ARTICLES + refunds.ARTICLES + devices.ARTICLES + kyc.ARTICLES
)

__all__ = ["ALLOWED_CATEGORIES", "ALL_ARTICLES", "ArticleSpec"]
