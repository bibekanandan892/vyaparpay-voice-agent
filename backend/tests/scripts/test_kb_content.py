"""Content sanity tests for `scripts/kb_content/` — the ~40 authored
support-KB articles (docs/09-memory-architecture.md §6.1, docs/17-roadmap.md
§2.5).

These are not tests of the seed script or the chunker (see
`test_seed_kb.py` / `test_kb_chunker.py`) — they check the *content itself*:
shape (counts, unique slugs, categories), the CHECK-constraint mirror
(`ALLOWED_CATEGORIES`), and the specific consistency requirement the task
called out explicitly — an invented policy number (a refund SLA, a KYC
turnaround) must be the *same* number everywhere it's referenced, never a
different plausible number per article.
"""

from __future__ import annotations

import re

from app.domain.types import KbArticle as KbArticleDomain
from scripts.kb_content import ALL_ARTICLES, ALLOWED_CATEGORIES
from scripts.kb_content import devices as devices_module
from scripts.kb_content import kyc as kyc_module
from scripts.kb_content import limits as limits_module
from scripts.kb_content import refunds as refunds_module
from scripts.kb_content import settlements as settlements_module

_CATEGORY_MODULES = {
    "limits": limits_module,
    "settlements": settlements_module,
    "refunds": refunds_module,
    "devices": devices_module,
    "kyc": kyc_module,
}

_HEADING_RE = re.compile(r"^#{1,6}\s+.+$", re.MULTILINE)

# --------------------------------------------------------------------------
# Shape: ~40 articles, 8 per category, matching docs/17 §2.5's "~8 articles
# per category, ~40 total"
# --------------------------------------------------------------------------


def test_total_article_count_is_40() -> None:
    assert len(ALL_ARTICLES) == 40


def test_exactly_8_articles_per_category() -> None:
    for category, module in _CATEGORY_MODULES.items():
        assert len(module.ARTICLES) == 8, f"{category} has {len(module.ARTICLES)} articles"


def test_all_articles_aggregates_every_category_module() -> None:
    aggregated = sum((module.ARTICLES for module in _CATEGORY_MODULES.values()), start=())
    assert set(a.slug for a in ALL_ARTICLES) == set(a.slug for a in aggregated)
    assert len(ALL_ARTICLES) == len(aggregated)


# --------------------------------------------------------------------------
# Category allowlist: mirrors ck_kb_articles_category (app/models/orm.py)
# --------------------------------------------------------------------------


def test_allowed_categories_matches_the_kb_articles_check_constraint() -> None:
    assert ALLOWED_CATEGORIES == {"limits", "settlements", "refunds", "devices", "kyc"}


def test_every_article_category_is_in_the_allowlist() -> None:
    for article in ALL_ARTICLES:
        assert article.category in ALLOWED_CATEGORIES, article.slug


def test_every_article_lives_in_the_category_module_matching_its_own_category() -> None:
    for category, module in _CATEGORY_MODULES.items():
        for article in module.ARTICLES:
            assert article.category == category, (
                f"{article.slug} declares category={article.category!r} but lives in "
                f"scripts/kb_content/{category}.py"
            )


# --------------------------------------------------------------------------
# Slugs and titles
# --------------------------------------------------------------------------


def test_every_slug_is_globally_unique() -> None:
    slugs = [a.slug for a in ALL_ARTICLES]
    assert len(slugs) == len(set(slugs)), "duplicate slug would collide on the kb_articles PK"


def test_every_title_is_unique() -> None:
    titles = [a.title for a in ALL_ARTICLES]
    assert len(titles) == len(set(titles))


def test_every_slug_is_prefixed_with_its_own_category() -> None:
    for article in ALL_ARTICLES:
        assert article.slug.startswith(f"kb_{article.category}_"), article.slug


def test_no_blank_title_or_body() -> None:
    for article in ALL_ARTICLES:
        assert article.title.strip(), article.slug
        assert article.body_md.strip(), article.slug


# --------------------------------------------------------------------------
# Heading-aware-chunker-friendly structure
# --------------------------------------------------------------------------


def test_every_article_opens_directly_with_a_markdown_heading() -> None:
    """ArticleSpec's own docstring convention: no top-level `#`/preamble —
    the article title is stored separately in kb_articles.title, so
    body_md should start with a `##`-or-deeper heading."""
    for article in ALL_ARTICLES:
        assert article.body_md.lstrip().startswith("#"), article.slug


def test_every_article_has_at_least_two_headings() -> None:
    """A single-heading article gives the heading-aware chunker nothing to
    exploit — docs/17 §2.5 asks for "markdown headings that make good
    chunk boundaries", plural."""
    for article in ALL_ARTICLES:
        headings = _HEADING_RE.findall(article.body_md)
        assert len(headings) >= 2, f"{article.slug} has only {len(headings)} heading(s)"


def test_no_article_uses_a_top_level_h1_heading() -> None:
    """`#` (h1) is reserved for the title, stored separately — every
    section heading in body_md should be `##` or deeper."""
    for article in ALL_ARTICLES:
        for line in article.body_md.splitlines():
            if line.startswith("#"):
                assert not re.match(r"^#\s", line), f"{article.slug}: {line!r}"


# --------------------------------------------------------------------------
# Validates through the domain type (app.domain.types.KbArticle) — the same
# validation scripts/seed_kb.py runs before ever touching the ORM
# --------------------------------------------------------------------------


def test_every_article_validates_through_the_kb_article_domain_type() -> None:
    for article in ALL_ARTICLES:
        KbArticleDomain(
            slug=article.slug,
            title=article.title,
            body_md=article.body_md,
            category=article.category,
        )


# --------------------------------------------------------------------------
# Canonical-incident grounding: the `limits` category must be consistent
# with what app/api/errors.py, app/data/repositories/payment_repo.py, and
# docs/01/docs/10 actually say about DAILY_LIMIT_EXCEEDED
# --------------------------------------------------------------------------


def _combined_text(module: object) -> str:
    return "\n".join(a.body_md for a in module.ARTICLES)  # type: ignore[attr-defined]


def test_limits_category_names_the_canonical_error_code() -> None:
    assert "DAILY_LIMIT_EXCEEDED" in _combined_text(limits_module)


def test_limits_category_is_consistent_with_the_canonical_merchant_pro_tiers() -> None:
    """docs/01 §6 (Rajesh's ₹25,000 limit) and docs/10 §6 turn 5 ("raise
    your daily limit from ₹25,000 to ₹50,000... within 4 business hours")
    — every limits article that cites the Merchant Pro tier must use these
    exact figures, never a different plausible pair."""
    text = _combined_text(limits_module)
    assert "₹25,000" in text
    assert "₹50,000" in text
    assert "4 business hours" in text
    assert "midnight IST" in text


def test_limits_category_uses_one_consistent_merchant_basic_tier() -> None:
    """Merchant Basic has no grounding doc — invented once
    (kb_limits_daily_txn_overview) and reused verbatim everywhere else
    it's referenced, never a second, differently-invented figure."""
    text = _combined_text(limits_module)
    assert "₹10,000" in text


# --------------------------------------------------------------------------
# Invented-number consistency, per category (the task's explicit ask:
# "pick one number and use it consistently across every article that
# references it")
# --------------------------------------------------------------------------


def test_refunds_sla_is_the_same_number_everywhere_it_is_referenced() -> None:
    text = _combined_text(refunds_module)
    assert text.count("7 business days") + text.count("7-business-day") >= 3
    # No competing SLA figure sneaks into this category's own content.
    for forbidden in ("3 business days", "5 business days", "10 business days"):
        assert forbidden not in text


def test_refunds_eligibility_window_is_the_same_number_everywhere() -> None:
    text = _combined_text(refunds_module)
    assert text.count("30 days") >= 2


def test_devices_dispatch_sla_is_the_same_number_everywhere() -> None:
    text = _combined_text(devices_module)
    assert text.count("5 business days") >= 2


def test_devices_return_window_is_the_same_number_everywhere() -> None:
    text = _combined_text(devices_module)
    assert text.count("10 days") + text.count("10-day") >= 2


def test_kyc_turnaround_is_the_same_number_everywhere() -> None:
    text = _combined_text(kyc_module)
    assert text.count("3 business days") >= 2


def test_kyc_reverification_cadence_is_the_same_number_everywhere() -> None:
    text = _combined_text(kyc_module)
    assert "12 months" in text


def test_settlements_cycle_is_consistently_t_plus_1() -> None:
    text = _combined_text(settlements_module)
    assert text.count("T+1") >= 3
    assert "6 PM IST" in text


def test_settlements_platform_fee_is_the_same_number_everywhere() -> None:
    text = _combined_text(settlements_module)
    assert text.count("0.3%") >= 2
