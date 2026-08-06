"""Prompt-slot renderers for the memory layers — the string side of
Phase-5's `ContextBuilder` wiring.

Three pure functions, one per thing memory contributes to the prompt:

- `render_open_issues` — the `user_profiles.open_issues` tail of slot 3
  (`<user_profile>`, 200 tokens, docs/09 §5.1, docs/11 §1).
- `render_summary_slot` — slot 6 (`<memory_summary>`, 250 tokens,
  docs/09 §4, docs/11 §1).
- `render_knowledge_slot` — slot 7 (`<knowledge>`, 300 tokens, top-3,
  docs/09 §6.2, docs/11 §1).

They live here rather than in `app/context/context_compressor.py`
because that class renders the *screen* slots off raw stored dicts and
owns the docs/07 §7 drop ladder; these render off memory domain types
and share none of that machinery. They are pure and side-effect free —
`ContextBuilder` owns the I/O, the span, and the logging.

---
## Where the escaping is, and where it is not

None of these functions escape slot delimiters. That is deliberate and
it is not an omission: `PromptBuilder._render_slot` escapes at the point
the tags are written, which is the only place that can guarantee it for
*every* slot including the ones these functions do not produce. Doing it
here as well would double-escape and would let a future slot skip it.

---
## The injection remedy for durable content, and the argument for it

`SafetyLayer.fence_input` neutralizes a flagged slot by replacing the
whole thing with an inert marker. That is the right shape for slots 4/5:
their content is one turn old, so a false positive costs one turn of
screen awareness and the next snapshot clears it.

It is the wrong shape for durable content. `user_profiles` is written
post-call and read on every future call, so a false positive there is
not a one-turn cost — the same stored bytes trip the same heuristic on
every subsequent call, and the merchant's profile is silently absent
from the prompt forever. `open_issues` is precisely the field docs/09
§5.1 says makes the next call feel continuous, so losing it is a
regression a merchant can perceive and we cannot see. The same holds
for a `call_summary` chunk retrieved from `memory_chunks`.

So the remedy here is **per entry, never per slot**:

- `render_open_issues` drops the individual issue whose text matched and
  renders the rest.
- `render_knowledge_slot` drops the individual chunk and renders the
  rest.

Both are lists, so a per-entry remedy is available; the blast radius of
a false positive is then one line instead of the whole layer. The
primary defense against a *forged region* remains structural (the
render-boundary escape); this heuristic is the secondary one, aimed at
imperative prose that survived into storage.

`render_summary_slot` runs **no heuristic at all**, and that is the
third decision rather than an oversight. The rolling summary is Haiku's
fold of turns the model already read verbatim as `user`/`assistant`
messages earlier in the same call — every sentence in it reached the
model through the one channel `SafetyLayer` deliberately does not
filter (`current_utterance`, that module's honesty note 2). Neutralizing
the paraphrase of speech we passed through unfiltered is incoherent, and
it is not free: the summary is contractually the only carrier of this
call's money amounts, ids and tool outcomes (docs/09 §4.2), so blanking
it on a false positive costs exactly the facts the fold exists to
preserve. The summary is also Redis-resident and dies with the call
(docs/09 §3), so nothing here is durable in the first place.

---
## Budgets

Budgets are docs/11 §1's, restated as constants next to the code that
spends them. Two different over-budget policies, each matching an
existing decision rather than inventing one:

- **Knowledge: drop whole entries, lowest-similarity first.** That is
  docs/08 §5.2's drop-rung 1 ("RAG top-3 -> top-1") applied at render
  time. `SemanticMemory.retrieve` returns nearest-first, so dropping
  from the tail drops the least similar. Each entry is additionally
  capped so three of them can fit at all — docs/09 §6.1 chunks the KB at
  ~300 tokens while docs/09 §6.2 budgets "3 x ~100-token renderings",
  which only reconciles if the *rendering* is shorter than the chunk.
- **Summary: warn, never truncate.** Not decided here — `Summarizer`
  already made this call (its judgment call #4) for the same value, and
  a mid-string cut would land inside the rupee amounts and reference ids
  docs/09 §4.2 requires be preserved verbatim. This module returns the
  text; `ContextBuilder` records the estimate and warns.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final

from app.agent.safety_layer import looks_injected
from app.context.token_estimate import CHARS_PER_TOKEN, SAFETY_MARGIN, estimate_tokens
from app.domain.types import (
    RETRIEVAL_TOP_K,
    MemoryKind,
    OpenIssue,
    RetrievedMemory,
    RollingSummary,
)

# docs/11 §1's slot budgets. `SUMMARY_SLOT_BUDGET` is not redefined here:
# `Summarizer.SUMMARY_TOKEN_BUDGET` is the same number for the same slot
# and importing it keeps one definition, so the writer and the reader
# cannot disagree about what fits.
PROFILE_SLOT_BUDGET: Final = 200
KNOWLEDGE_SLOT_BUDGET: Final = 300

_ELLIPSIS: Final = "…"

# How far back `_truncate_to_chars` may look for a word boundary.
#
# Bounded, and the bound is load-bearing rather than tidiness: an
# unbounded `rfind(" ")` walks back to the *first* space in the string,
# so a chunk whose tail has no spaces — a long identifier, a URL, a
# base64 blob, a CJK run — collapses to its first word. Found by
# tests/memory/test_slots.py, where a 400-character unbroken run turned a
# 317-character allowance into a 17-character entry and the slot then
# "fit" six chunks it should have dropped three of. Past this window the
# cut is taken mid-word, which is uglier and correct.
_WORD_BOUNDARY_WINDOW: Final = 32

# Per-entry cap, budgeted in CHARACTERS rather than tokens.
#
# The slot budget is a token figure, but the only token counter on this
# path is `estimate_tokens`' chars/3.5-with-10%-margin proxy, and
# dividing a *token* budget by `RETRIEVAL_TOP_K` overshoots: the proxy's
# `ceil` and the newlines joining the entries push `k` exactly-at-budget
# renderings one token over, which then costs a whole entry to the
# drop-to-fit loop below. Inverting the proxy once, budgeting characters,
# and subtracting the `k - 1` join characters makes "top-3 fits the slot"
# (docs/09 §6.2's claim) arithmetically true instead of nearly true.
#
# The cap applies to the WHOLE rendered entry, prefix included — a
# `[past call 0.75] ` prefix is 17 characters that have to come from
# somewhere, and taking them off the body is the only place they can.
_SLOT_CHAR_BUDGET: Final = int(KNOWLEDGE_SLOT_BUDGET * CHARS_PER_TOKEN / SAFETY_MARGIN)
_ENTRY_CHAR_BUDGET: Final = (_SLOT_CHAR_BUDGET - (RETRIEVAL_TOP_K - 1)) // RETRIEVAL_TOP_K

# The rendered prefix per retrieved chunk. docs/11 §4's worked example
# shows `[kb 0.83] ...` for a KB article and shows nothing at all for a
# call summary, so `past call` is this module's label, chosen to be
# unmistakable to the model about whose text it is reading: a summary of
# an earlier call with this same merchant, not documentation.
_KIND_LABELS: Final = {
    MemoryKind.KB_ARTICLE: "kb",
    MemoryKind.CALL_SUMMARY: "past call",
}

# docs/09 §11's embedding-outage row makes an empty RAG slot the
# *expected* degradation, and `SemanticRepo.search`'s own docstring
# records that a truncated HNSW iterative scan is indistinguishable from
# "nothing else was relevant" — nothing detects it. An empty
# `<knowledge></knowledge>` therefore carries no information about the
# world, only about retrieval, and the failure mode that matters is the
# model reading it as evidence and telling a merchant there is no record
# of something. This line is what stands in the slot instead.
#
# It states only what is true (retrieval produced nothing, for one of
# several indistinguishable reasons) and points at the remedy the rest
# of the prompt already mandates (docs/10 §1 invariant 1: account facts
# come from tools). It is a fixed string, so slot 7 stays byte-stable
# across the turns it is absent on.
KNOWLEDGE_UNAVAILABLE: Final = (
    "No knowledge-base excerpts were retrieved for this call. An empty section "
    "here is not evidence about the merchant's account or history: retrieval may "
    "have found nothing above the relevance floor, may not have run, or may have "
    "returned short. Do not tell the merchant that something does not exist, or "
    "that there is no record of it, on the strength of this section being empty — "
    "call the matching tool, or offer to check."
)


def _truncate_to_chars(text: str, max_chars: int) -> str:
    """Trim `text` to at most `max_chars`, at a word boundary, marking the
    cut with a trailing ellipsis.

    The ellipsis is inside the budget, not added on top of it, so the
    result really is `<= max_chars` — an off-by-one here would be
    invisible until three at-budget entries pushed the slot over.

    Honest limit: a word-boundary cut still cuts. A KB chunk truncated
    here loses its tail, and a rupee amount sitting in that tail is lost
    with it. That is acceptable for slot 7 specifically — retrieved
    knowledge is advisory (docs/08 §5.2 drop-rung 1) and the model is
    barred from voicing an amount no tool returned (docs/10 §1 invariant
    1) — and it is exactly why slot 6 is NOT truncated.
    """
    if len(text) <= max_chars:
        return text
    head = text[: max(1, max_chars - len(_ELLIPSIS))]
    boundary = head.rfind(" ", max(0, len(head) - _WORD_BOUNDARY_WINDOW))
    if boundary > 0:
        head = head[:boundary]
    return head.rstrip() + _ELLIPSIS


def render_open_issues(issues: Sequence[OpenIssue], *, token_budget: int) -> str:
    """The `open_issues` tail of the profile slot, newest first.

    Newest first because that is what the next call opens on ("checking
    on your limit increase?", docs/09 §5.1) and because entries beyond
    the budget are dropped from the tail — `UserProfileMemory` appends,
    so the stored tuple is oldest-first and reversing it makes the drop
    fall on the stalest issue.

    `opened_at` renders as an absolute ISO date, never a relative age.
    Slot 3 is "cached within a call" (docs/11 §1.1) and a relative age
    recomputed against `now` would change the slot's bytes on every turn,
    silently destroying the prefix-cache hit the latency and cost budgets
    both assume. The date is also what a merchant would recognize.

    Returns `""` when there is nothing to render — no issues, all of them
    screened out, or a budget too small for even one line. `""` is the
    honest rendering: unlike slot 7, an absent open issue really does mean
    "this merchant has no issue we opened", because the profile is read
    whole from one row rather than retrieved by similarity.
    """
    if token_budget <= 0:
        return ""
    kept = [issue for issue in reversed(issues) if not looks_injected(issue.summary)]
    if not kept:
        return ""

    lines = [
        f"- {issue.summary} ({issue.status}, opened {issue.opened_at.date().isoformat()})"
        for issue in kept
    ]
    header = "Open issues from earlier calls:"
    while lines and estimate_tokens("\n".join([header, *lines])) > token_budget:
        lines.pop()
    if not lines:
        return ""
    return "\n".join([header, *lines])


def render_summary_slot(summary: RollingSummary | None) -> str:
    """Slot 6's text: the rolling summary, verbatim, or `""`.

    `thru_turn` is not rendered. It is the boundary the *window* renderer
    needs so coverage and verbatim turns neither overlap nor gap
    (`RollingSummary`'s own docstring); the model gains nothing from a
    turn index and it would cost tokens in a slot whose whole job is
    carrying facts.

    No truncation and no injection screen — the module docstring argues
    both. `ContextBuilder` checks the estimate against the budget and
    warns, matching `Summarizer`'s judgment call #4 on the write side.
    """
    if summary is None:
        return ""
    return summary.text


def _render_entry(result: RetrievedMemory) -> str:
    label = _KIND_LABELS[result.kind]
    return _truncate_to_chars(
        f"[{label} {result.similarity:.2f}] {result.content.strip()}", _ENTRY_CHAR_BUDGET
    )


def render_knowledge_slot(results: Sequence[RetrievedMemory]) -> str:
    """Slot 7's text: the retrieved top-k, or `KNOWLEDGE_UNAVAILABLE`.

    Takes results already filtered by `SemanticMemory.retrieve` — the
    0.70 floor and top-`k` are that class's policy (docs/09 §6.2) and are
    not re-applied, re-derived or topped up here. This function only
    renders and enforces the slot budget.

    Nothing here re-checks tenant scoping either, and that is on purpose:
    the scope predicate lives in `SemanticRepo.search`'s SQL, and a
    Python-side filter after the fact would be a second, weaker copy of a
    rule whose whole security argument is that it has exactly one call
    site. Rows this function can see are rows the database already
    decided were in scope.

    The drop-to-fit loop is a backstop, not the normal path: at
    `RETRIEVAL_TOP_K` results the per-entry cap already guarantees a fit,
    so the loop only engages for a caller that asked `retrieve()` for a
    larger `k`. It drops from the tail, which is the least similar,
    because the repo returns nearest-first.
    """
    entries = [_render_entry(result) for result in results if not looks_injected(result.content)]
    while entries and estimate_tokens("\n".join(entries)) > KNOWLEDGE_SLOT_BUDGET:
        entries.pop()
    if not entries:
        return KNOWLEDGE_UNAVAILABLE
    return "\n".join(entries)


__all__ = [
    "KNOWLEDGE_SLOT_BUDGET",
    "KNOWLEDGE_UNAVAILABLE",
    "PROFILE_SLOT_BUDGET",
    "render_knowledge_slot",
    "render_open_issues",
    "render_summary_slot",
]
