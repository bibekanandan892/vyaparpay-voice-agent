"""SemanticMemory — Layer 6, the Business Knowledge retriever (docs/09
§6.2, docs/05 §3.7). Implements `app.domain.interfaces.SemanticMemoryProto`.

The split with `SemanticRepo` is deliberate and is where the security
argument lives. This class owns *policy* — embed the query, drop anything
below the similarity floor — and owns no SQL at all. `SemanticRepo.search()`
owns the scoped query, composing `memory_scope(principal)`, and is the only
code in `app/` that reads `memory_chunks`. Splitting it that way means the
tenant predicate has exactly one call site to review, and this class cannot
weaken it even by accident: it has no query to write.

`principal` is threaded straight through to that predicate, required and
undefaulted at both levels, the same `SessionUser` `ToolExecutor` injects
(docs/10 §1 invariant 3).

**Scope boundary.** This is the read surface only. Rendering these results
into the ~300-token RAG slot is `ContextBuilder`'s knowledge slot (docs/08),
a later task — nothing here touches it. Indexing (KB seed, post-call embed)
belongs to `SemanticRepo`, per `SemanticMemoryProto`'s docstring, and is
also a later task.

**Failure behavior is not caught here.** docs/09 §11 makes an embedding
outage drop-rung 1: the RAG slot is skipped entirely, no keyword-search
fallback. That decision belongs to the context-assembly layer that owns the
slot and can tell "no relevant memory" apart from "retrieval is down" — so
an embedding or database error propagates out of `retrieve()` rather than
being turned into an empty list here, which would make those two cases
indistinguishable.
"""

from __future__ import annotations

from app.data.repositories.semantic_repo import SemanticRepo
from app.domain.interfaces import EmbeddingProvider
from app.domain.types import (
    RETRIEVAL_FLOOR,
    RETRIEVAL_TOP_K,
    RetrievedMemory,
    SessionUser,
)


def turn_query(utterance: str, intent: str) -> str:
    """docs/09 §6.2's per-turn query: "current utterance + the turn's
    classified intent, concatenated as one string and embedded".

    The doc gives the example inline as `"how do I raise it \\n intent:
    limit_increase"`. The spaces flanking the `\\n` there are prose
    typography, not part of the string, so this joins with a bare newline;
    the doc's own text ("concatenated as one string") is what is being
    implemented, and neither spacing is distinguishable to an embedding
    model anyway. Recorded because it is a place where the code and a
    literal reading of the doc differ by two characters.

    Re-query timing (topic shift only, not every turn) is the caller's —
    docs/09 §6.2 assigns it to the turn loop, and nothing here is
    stateful enough to know an intent changed.
    """
    return f"{utterance}\nintent: {intent}"


def prefetch_query(error_code: str | None, screen_name: str | None) -> str:
    """docs/09 §6.2's call-setup query, used before any utterance exists:
    "the active error code + screen name (`DAILY_LIMIT_EXCEEDED
    PaymentScreen`)" — the speculative prefetch docs/08 §1 references.

    Either half may be absent at setup (a call opened from a screen with
    no active error, or with no screen context at all), so empty parts are
    dropped rather than rendered as the string "None" — which would be a
    token of pure noise in the embedded query. Two absent halves give an
    empty string; `retrieve()` refuses to embed that (see its docstring)
    rather than spending a network hop on a query with no content.
    """
    return " ".join(part.strip() for part in (error_code, screen_name) if part and part.strip())


class SemanticMemory:
    """Implements `app.domain.interfaces.SemanticMemoryProto`.

    Both collaborators are injected: `EmbeddingProvider` protocol-typed (so
    the vendor is swappable and tests drive a stub), and the concrete
    `SemanticRepo` rather than a second protocol — the whole point of the
    repo is that it is the *only* implementation that may query the table,
    and an interface here would invite a second one.
    """

    def __init__(self, embeddings: EmbeddingProvider, repo: SemanticRepo) -> None:
        self._embeddings = embeddings
        self._repo = repo

    async def retrieve(
        self,
        query: str,
        principal: SessionUser,
        *,
        k: int = RETRIEVAL_TOP_K,
        floor: float = RETRIEVAL_FLOOR,
    ) -> list[RetrievedMemory]:
        """Top-`k` chunks above `floor`, scoped to the KB plus this
        principal's own call summaries (docs/09 §6.2).

        Below-floor results are **dropped, never padded**: docs/09 §6.2
        reasons that a 300-token slot of marginally related text is worse
        than an empty slot, because the model treats retrieved text as
        more authoritative than it is. So this returns fewer than `k`
        results, or none at all, whenever that is what cleared the bar.

        An empty or whitespace-only query returns `[]` without an
        embedding call. That is the prefetch case where neither an error
        code nor a screen name was available (`prefetch_query`) — there is
        nothing to be similar *to*, and embedding the empty string would
        spend a network hop to retrieve whatever happens to sit nearest
        the origin of the vector space, which is noise dressed as
        relevance.
        """
        if not query.strip():
            return []

        # One-element list: `EmbeddingProvider` is batched by construction
        # and has no single-text overload (see its docstring). The provider
        # guarantees positional alignment, so [0] is this query's vector.
        vectors = await self._embeddings.embed([query])
        results = await self._repo.search(vectors[0], principal, k=k)

        # The repo already applied `k` as a SQL LIMIT after scoping, so
        # this is a filter only — never a re-slice, and never a top-up.
        return [result for result in results if result.similarity >= floor]


__all__ = ["SemanticMemory", "prefetch_query", "turn_query"]
