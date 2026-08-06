"""Heading-aware chunker for `kb_articles.body_md` (docs/09-memory-
architecture.md §6.1: "Heading-aware, ~300-token chunks, 50-token
overlap"). A small, pure, independently-testable function — deliberately
not inlined into `scripts/seed_kb.py`'s control flow, so the chunking
*algorithm* can be tested against known token counts without a database,
an embedding provider, or an event loop anywhere near it.

**The concrete shape of "heading-aware."** `body_md` is split at markdown
headings (`^#+\\s`) first, so a chunk boundary never falls mid-section
*when the section itself fits the token budget* — a section that already
fits ~300 tokens becomes exactly one chunk, whole, never merged with a
neighboring section even if the merge would still be under budget. That
choice is what makes the boundary rule simple to state and to test: one
heading, one chunk, unless the section is oversized. It is also what
makes the seeded corpus's chunk count line up with docs/09 §6.1's "~40
articles -> ~180 chunks" estimate — this package's content is authored at
roughly 4-5 headings per article for exactly that reason (see
`scripts/kb_content/`'s module docstrings).

**Sub-splitting an oversized section.** When a section's heading-plus-body
text exceeds `target_tokens`, it is packed into multiple chunks by a
greedy word-level packer: add whole words until the *next* word would push
the chunk over budget, close the chunk, then start the next one `overlap_
tokens` worth of trailing words *before* the point the previous chunk
ended — a classic sliding window. That carries the last ~50 tokens of one
chunk into the start of the next, so a fact sitting at a chunk boundary
(a number, a threshold, an SLA) is findable from a query that embeds
either side of the cut. The heading itself is repeated as a prefix on every
sub-chunk of its section, so a mid-section fragment retrieved on its own
still carries the topic it belongs to, not just a body-text fragment with
no context.

Both budgets are expressed in the same `chars/3.5` proxy used everywhere
else in this codebase a token count is estimated on a hot path with no
real tokenizer available (`app.context.token_estimate.estimate_tokens`) —
reused here, not re-derived, exactly as the task calls for. This module
never imports anything from `app/` beyond that one pure function, so it
carries no database, HTTP, or async dependency at all.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.context.token_estimate import estimate_tokens

# docs/09 §6.1's own numbers, verbatim. Kept as named constants (not
# re-spelled at each call site) so `scripts/seed_kb.py` and this module's
# tests both read the same two numbers.
TARGET_TOKENS = 300
OVERLAP_TOKENS = 50

# Markdown ATX headings only (`#` through `######`) — the heading style
# every article in `scripts/kb_content/` uses exclusively. Setext-style
# (`Heading\n=====`) is out of scope: nothing in this codebase's seeded
# content ever produces it, and adding a second heading grammar to detect
# would be speculative generality for content that doesn't exist.
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")


@dataclass(frozen=True)
class Chunk:
    """One chunk ready to become a `memory_chunks` row's `content`, paired
    with the article slug it came from — `MemoryChunk.source_id`
    (docs/09 §6.1). Carries no embedding: `scripts/seed_kb.py` embeds the
    whole batch of `.text` values in one call and zips the vectors back
    against these positionally, exactly as `EmbeddingProvider.embed`'s
    contract requires.
    """

    source_id: str
    text: str


@dataclass(frozen=True)
class _Section:
    """One heading-delimited slice of `body_md`. `heading` is the raw
    heading line (e.g. `"## What DAILY_LIMIT_EXCEEDED means"`), or `""`
    for a headingless preamble before the first heading — a shape this
    module's own content never produces (every article in
    `scripts/kb_content/` opens directly with a heading) but handles
    correctly anyway, since a chunker that silently drops a headingless
    preamble on someone else's content is a worse failure than one that
    never runs into it here."""

    heading: str
    body: str


def _split_sections(body_md: str) -> list[_Section]:
    """Split on every markdown heading line, keeping the heading with the
    section it introduces. A headingless run of lines before the first
    heading becomes one `_Section` with `heading=""`; an article with no
    headings at all becomes a single section covering the whole body."""
    sections: list[_Section] = []
    current_heading = ""
    current_lines: list[str] = []

    def _flush() -> None:
        body = "\n".join(current_lines).strip()
        if current_heading or body:
            sections.append(_Section(heading=current_heading, body=body))

    for line in body_md.splitlines():
        match = _HEADING_RE.match(line)
        if match is not None:
            _flush()
            current_heading = line.strip()
            current_lines = []
        else:
            current_lines.append(line)
    _flush()
    return sections


def _section_text(section: _Section) -> str:
    if not section.heading:
        return section.body
    if not section.body:
        return section.heading
    return f"{section.heading}\n\n{section.body}"


def _pack_with_overlap(
    section: _Section, *, target_tokens: int, overlap_tokens: int
) -> list[str]:
    """Greedy word-level sliding-window pack for one oversized section's
    body, each sub-chunk prefixed with the section's heading.

    Progress is guaranteed even in pathological inputs: the inner packing
    loop always accepts at least one word (the `current_words and ...`
    guard only fires once `current_words` is non-empty, so a single word
    whose own token estimate exceeds `target_tokens` still gets emitted
    rather than looping forever), and the rewind for the next chunk's
    overlap is capped at `len(current_words) - 1` words — strictly fewer
    than the words just consumed — so the read position advances by at
    least one word every iteration.
    """
    words = section.body.split()
    if not words:
        return [section.heading] if section.heading else []

    def render(chunk_words: list[str]) -> str:
        joined = " ".join(chunk_words)
        return f"{section.heading}\n\n{joined}" if section.heading else joined

    chunks: list[str] = []
    i = 0
    while i < len(words):
        current_words: list[str] = []
        while i < len(words):
            candidate = [*current_words, words[i]]
            if current_words and estimate_tokens(render(candidate)) > target_tokens:
                break
            current_words = candidate
            i += 1
        chunks.append(render(current_words))
        if i >= len(words):
            break

        # Trailing overlap: walk backward from the end of this chunk,
        # accumulating words until their rendered text reaches
        # `overlap_tokens`, then rewind `i` so the next chunk starts with
        # them again.
        overlap_words: list[str] = []
        for word in reversed(current_words):
            candidate_overlap = [word, *overlap_words]
            if overlap_words and estimate_tokens(" ".join(candidate_overlap)) > overlap_tokens:
                break
            overlap_words = candidate_overlap
        rewind = min(len(overlap_words), len(current_words) - 1)
        i -= rewind

    return chunks


def chunk_article(
    slug: str,
    body_md: str,
    *,
    target_tokens: int = TARGET_TOKENS,
    overlap_tokens: int = OVERLAP_TOKENS,
) -> list[Chunk]:
    """Heading-aware chunking of one article's `body_md` into `Chunk`s
    ready for embedding, per docs/09 §6.1. See the module docstring for
    the boundary rule and overlap mechanics; this function only wires
    `_split_sections` and `_pack_with_overlap` together and stamps every
    resulting chunk with `slug` as `source_id`.

    An empty or whitespace-only `body_md` returns `[]` rather than one
    empty chunk — nothing in `scripts/kb_content/` produces this, but an
    empty `memory_chunks.content` would fail `MemoryChunk`'s own
    `max_length`-but-not-`min_length` validation ambiguously, so it is
    refused here instead, at the layer that can say why.
    """
    if not body_md.strip():
        return []

    chunks: list[Chunk] = []
    for section in _split_sections(body_md):
        whole = _section_text(section)
        if estimate_tokens(whole) <= target_tokens:
            chunks.append(Chunk(source_id=slug, text=whole))
        else:
            for text in _pack_with_overlap(
                section, target_tokens=target_tokens, overlap_tokens=overlap_tokens
            ):
                chunks.append(Chunk(source_id=slug, text=text))
    return chunks


__all__ = ["OVERLAP_TOKENS", "TARGET_TOKENS", "Chunk", "chunk_article"]
