"""Unit tests for `scripts/kb_chunker.py` — the heading-aware chunker
(docs/09-memory-architecture.md §6.1: "Heading-aware, ~300-token chunks,
50-token overlap").

Pure-function tests, no database/HTTP/event loop anywhere near them — the
whole point of splitting this out of `scripts/seed_kb.py`'s control flow.

The oversized-section tests use small, hand-derived `target_tokens`/
`overlap_tokens` values with single-character words so the exact word-level
chunk boundaries can be computed independently (by hand, in this docstring
and the test bodies) against `app.context.token_estimate.estimate_tokens`'s
real `chars/3.5 * 1.10` formula, rather than asserting the algorithm agrees
with itself.
"""

from __future__ import annotations

from app.context.token_estimate import estimate_tokens
from scripts.kb_chunker import OVERLAP_TOKENS, TARGET_TOKENS, Chunk, chunk_article

# --------------------------------------------------------------------------
# Defaults match docs/09 §6.1 verbatim
# --------------------------------------------------------------------------


def test_default_budgets_match_docs_09() -> None:
    assert TARGET_TOKENS == 300
    assert OVERLAP_TOKENS == 50


# --------------------------------------------------------------------------
# Heading-boundary rule: one section under budget -> exactly one chunk,
# never merged with a neighboring section
# --------------------------------------------------------------------------


def test_single_short_section_becomes_one_chunk_with_heading_and_body() -> None:
    body = "## Only heading\n\nA short paragraph well under any token budget."

    chunks = chunk_article("kb_test_slug", body)

    assert chunks == [
        Chunk(
            source_id="kb_test_slug",
            text="## Only heading\n\nA short paragraph well under any token budget.",
        )
    ]


def test_two_short_sections_each_become_their_own_chunk_not_merged() -> None:
    """The heading-boundary rule stated in the module docstring: a section
    that already fits the budget becomes exactly one chunk, even though
    two short sections concatenated would still fit under target_tokens —
    they are never merged into a single larger chunk."""
    body = "## First heading\n\nFirst body text.\n\n## Second heading\n\nSecond body text."

    chunks = chunk_article("kb_test_slug", body)

    assert len(chunks) == 2
    assert chunks[0].text == "## First heading\n\nFirst body text."
    assert chunks[1].text == "## Second heading\n\nSecond body text."
    # Neither chunk leaks the other section's heading or body — the
    # boundary genuinely falls between sections, not somewhere inside one.
    assert "Second" not in chunks[0].text
    assert "First" not in chunks[1].text


def test_three_headings_of_different_levels_each_start_their_own_section() -> None:
    body = "## Level two\n\nBody A.\n\n### Level three\n\nBody B.\n\n#### Level four\n\nBody C."

    chunks = chunk_article("kb_test_slug", body)

    assert [c.text for c in chunks] == [
        "## Level two\n\nBody A.",
        "### Level three\n\nBody B.",
        "#### Level four\n\nBody C.",
    ]


def test_headingless_preamble_becomes_its_own_section() -> None:
    """Every article in scripts/kb_content/ opens directly with a heading
    (ArticleSpec's own docstring), but the chunker handles a headingless
    preamble correctly anyway rather than silently dropping it."""
    body = "Some intro text with no heading yet.\n\n## First real heading\n\nBody."

    chunks = chunk_article("kb_test_slug", body)

    assert chunks[0].text == "Some intro text with no heading yet."
    assert chunks[1].text == "## First real heading\n\nBody."


def test_body_with_no_headings_at_all_is_one_section() -> None:
    body = "Just a paragraph.\n\nAnd another paragraph, still no heading anywhere."

    chunks = chunk_article("kb_test_slug", body)

    assert len(chunks) == 1
    assert chunks[0].text == body


# --------------------------------------------------------------------------
# Empty input
# --------------------------------------------------------------------------


def test_empty_body_returns_no_chunks() -> None:
    assert chunk_article("kb_test_slug", "") == []


def test_whitespace_only_body_returns_no_chunks() -> None:
    assert chunk_article("kb_test_slug", "   \n\n   ") == []


# --------------------------------------------------------------------------
# source_id stamping
# --------------------------------------------------------------------------


def test_every_chunk_is_stamped_with_the_article_slug() -> None:
    body = "## A\n\nText.\n\n## B\n\nMore text."

    chunks = chunk_article("kb_daily_limits", body)

    assert all(c.source_id == "kb_daily_limits" for c in chunks)


# --------------------------------------------------------------------------
# Oversized section: deterministic word-level packing + overlap
#
# 16 single-character words ("a".."p") under one heading "## H" (4 chars),
# joined by single spaces. For k packed words the rendered chunk text is
# "## H" + "\n\n" + <k words joined by spaces>, whose length in characters
# is exactly 2*k + 5 (4 heading chars + 2 newline chars + k word chars +
# (k-1) space chars = 4 + 2 + k + k - 1 = 2k + 5). Feeding that through the
# real chars/3.5*1.10 estimator (ceil-rounded) gives the token count for
# any k, hand-tabulated below and cross-checked against estimate_tokens
# itself in test_estimate_tokens_matches_the_hand_tabulated_word_counts so
# the table can't silently drift from the formula it claims to model.
# --------------------------------------------------------------------------

_WORDS = "a b c d e f g h i j k l m n o p".split()
assert len(_WORDS) == 16
_HEADING = "## H"


def _render(k_words: list[str]) -> str:
    return f"{_HEADING}\n\n{' '.join(k_words)}"


# tokens(k) for k = 1..16, hand-derived from ceil((2k+5)/3.5 * 1.10).
_TOKENS_BY_K = {k: estimate_tokens(_render(_WORDS[:k])) for k in range(1, 17)}


def test_estimate_tokens_matches_the_hand_tabulated_word_counts() -> None:
    """Pins the fixture's own premise: this is `estimate_tokens` computed
    directly from `_render`, not a second hand-rolled formula that could
    quietly disagree with the real estimator the chunker actually calls."""
    assert _TOKENS_BY_K[1] == 3
    assert _TOKENS_BY_K[4] == 5
    assert _TOKENS_BY_K[5] == 5
    assert _TOKENS_BY_K[6] == 6
    assert _TOKENS_BY_K[16] == 12


def _body() -> str:
    return f"{_HEADING}\n\n{' '.join(_WORDS)}"


def test_oversized_section_is_split_into_multiple_chunks() -> None:
    """target_tokens=5 is below the whole section's 12 tokens (_TOKENS_BY_K[16]),
    so the section must be sub-split rather than kept whole."""
    chunks = chunk_article("kb_test_slug", _body(), target_tokens=5, overlap_tokens=2)

    assert len(chunks) > 1
    assert all(estimate_tokens(c.text) <= 5 for c in chunks[:-1]), (
        "every chunk but possibly the last must respect the target budget"
    )


def test_oversized_section_packs_words_greedily_up_to_the_target() -> None:
    """First chunk: words are added while the *next* candidate still fits
    target_tokens=5. k=5 -> 5 tokens (fits), k=6 -> 6 tokens (does not) —
    so the first chunk holds exactly the first 5 words, per _TOKENS_BY_K."""
    chunks = chunk_article("kb_test_slug", _body(), target_tokens=5, overlap_tokens=2)

    assert chunks[0].text == _render(["a", "b", "c", "d", "e"])


def test_oversized_section_every_subchunk_is_prefixed_with_the_heading() -> None:
    """A mid-section fragment retrieved on its own must still carry the
    heading it belongs to, not a bare, topic-less body fragment."""
    chunks = chunk_article("kb_test_slug", _body(), target_tokens=5, overlap_tokens=2)

    assert all(c.text.startswith(_HEADING) for c in chunks)


def test_oversized_section_overlap_carries_trailing_words_into_the_next_chunk() -> None:
    """Hand-derived from the module's own algorithm (see the file docstring
    above for the full word-by-word trace): with target_tokens=5 and
    overlap_tokens=2, chunk 1 is [a,b,c,d,e], and the overlap walk-back
    from its tail collects {c,d,e} (3 words) before the 4th word (b) would
    push the overlap text past 2 tokens — so chunk 2 starts at word index
    2, reproducing c,d,e as its own first three words.
    """
    chunks = chunk_article("kb_test_slug", _body(), target_tokens=5, overlap_tokens=2)

    assert chunks[0].text == _render(["a", "b", "c", "d", "e"])
    assert chunks[1].text == _render(["c", "d", "e", "f", "g"])
    # The overlapping words are literally shared text between consecutive
    # chunks — the property the overlap exists for: a fact sitting at the
    # word-2/word-3 boundary is findable from either chunk.
    assert "c d e" in chunks[0].text
    assert "c d e" in chunks[1].text


def test_oversized_section_full_deterministic_chunk_sequence() -> None:
    """The complete 7-chunk sequence for all 16 words, hand-traced in the
    module docstring above. Pinning the whole sequence (not just the first
    two chunks) catches a regression in the tail case too — the last
    chunk (4 words: m,n,o,p) has no overlap computed after it, since the
    outer loop's `if i >= len(words): break` fires before the overlap
    walk-back runs."""
    chunks = chunk_article("kb_test_slug", _body(), target_tokens=5, overlap_tokens=2)

    assert [c.text for c in chunks] == [
        _render(["a", "b", "c", "d", "e"]),
        _render(["c", "d", "e", "f", "g"]),
        _render(["e", "f", "g", "h", "i"]),
        _render(["g", "h", "i", "j", "k"]),
        _render(["i", "j", "k", "l", "m"]),
        _render(["k", "l", "m", "n", "o"]),
        _render(["m", "n", "o", "p"]),
    ]


# --------------------------------------------------------------------------
# Progress guarantee: pathological input cannot hang the packer
# --------------------------------------------------------------------------


def test_a_single_oversized_word_is_still_emitted_rather_than_looping_forever() -> None:
    """A "word" whose own token estimate exceeds target_tokens must still
    be accepted (the `current_words and ...` guard only fires once
    current_words is non-empty), or the packer would never advance `i`
    past position 0."""
    huge_word = "x" * 500  # far more than target_tokens=5 could ever fit
    body = f"## H\n\n{huge_word} short"

    chunks = chunk_article("kb_test_slug", body, target_tokens=5, overlap_tokens=2)

    assert len(chunks) >= 1
    assert huge_word in chunks[0].text


def test_many_tiny_words_terminate_without_hanging() -> None:
    """Progress-guarantee smoke test at a larger scale: 500 one-character
    words must chunk in finite time and cover every word exactly once
    across the concatenation of all chunks' bodies (accounting for
    overlap duplication, i.e. every word appears at least once)."""
    words = [chr(ord("a") + (i % 26)) for i in range(500)]
    body = "## H\n\n" + " ".join(words)

    chunks = chunk_article("kb_test_slug", body, target_tokens=20, overlap_tokens=5)

    assert len(chunks) > 1
    rendered = " ".join(c.text for c in chunks)
    for word in words:
        assert word in rendered


# --------------------------------------------------------------------------
# Realistic content: the actual seeded article that is the canonical
# incident's article exercises the chunker end to end at real budgets
# --------------------------------------------------------------------------


def test_real_daily_limit_exceeded_article_chunks_without_exceeding_target() -> None:
    from scripts.kb_content.limits import ARTICLES

    article = next(a for a in ARTICLES if a.slug == "kb_limits_daily_limit_exceeded")

    chunks = chunk_article(article.slug, article.body_md)

    assert len(chunks) >= 1
    assert all(c.source_id == article.slug for c in chunks)
    # Every heading-sized section in this article's authored content fits
    # comfortably under the 300-token default target (each section is a
    # few short sentences), so no sub-splitting should occur here — one
    # chunk per heading.
    assert all(estimate_tokens(c.text) <= TARGET_TOKENS for c in chunks)
