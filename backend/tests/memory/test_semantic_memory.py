"""Tests for `SemanticMemory` — Layer 6's retrieval policy (docs/09 §6.2,
docs/05 §3.7).

Two groups, and the second is the one Batch 1 asked for by name:

1. **Policy.** Query construction, the 0.70 floor, drop-not-pad, and the
   fact that failures propagate rather than being turned into an empty
   list. `SemanticRepo` is a mock here — the SQL it builds is tested in
   `tests/data/repositories/test_semantic_repo.py`, and what matters at
   this layer is that `principal` is threaded through untouched and the
   floor is applied to what comes back.

2. **The concrete-class conformance check** (`SemanticMemoryProto` handoff
   3). Batch 1's contract-freeze test inspects the *Protocol* only, and
   its own docstring records the verified gap: an implementation that adds
   `include_all_users: bool = False` is legal structural widening, mypy
   accepts it, and every existing guard stays green. The tests under
   "Handoff 3" below inspect `SemanticMemory.retrieve` itself, so that
   escape hatch cannot be added to the class that owns the retrieval path
   without failing the build.
"""

from __future__ import annotations

import inspect
from unittest.mock import AsyncMock

import pytest

from app.data.repositories.semantic_repo import SemanticRepo
from app.domain.interfaces import SemanticMemoryProto
from app.domain.types import (
    EMBEDDING_DIM,
    RETRIEVAL_FLOOR,
    RETRIEVAL_TOP_K,
    MemoryKind,
    RetrievedMemory,
    SessionUser,
)
from app.memory.semantic import SemanticMemory, prefetch_query, turn_query
from tests.fakes import FakeEmbeddings

PRINCIPAL = SessionUser(user_id="usr_rajesh01")


def _memory(similarity: float, *, chunk_id: int = 1, content: str = "...") -> RetrievedMemory:
    return RetrievedMemory(
        chunk_id=chunk_id,
        kind=MemoryKind.KB_ARTICLE,
        source_id="kb_daily_limits",
        content=content,
        similarity=similarity,
    )


def make_memory(
    results: list[RetrievedMemory] | None = None,
) -> tuple[SemanticMemory, FakeEmbeddings, AsyncMock]:
    embeddings = FakeEmbeddings()
    repo = AsyncMock(spec=SemanticRepo)
    repo.search.return_value = results if results is not None else []
    return SemanticMemory(embeddings, repo), embeddings, repo


# --------------------------------------------------------------------------
# Query construction (docs/09 §6.2)
# --------------------------------------------------------------------------


def test_turn_query_concatenates_utterance_and_classified_intent() -> None:
    """docs/09 §6.2: "current utterance + the turn's classified intent,
    concatenated as one string and embedded"."""
    assert turn_query("how do I raise it", "limit_increase") == (
        "how do I raise it\nintent: limit_increase"
    )


def test_prefetch_query_is_error_code_then_screen_name() -> None:
    """The call-setup query, before any utterance exists — docs/09 §6.2
    gives the literal example `DAILY_LIMIT_EXCEEDED PaymentScreen`."""
    assert prefetch_query("DAILY_LIMIT_EXCEEDED", "PaymentScreen") == (
        "DAILY_LIMIT_EXCEEDED PaymentScreen"
    )


@pytest.mark.parametrize(
    ("error_code", "screen_name", "expected"),
    [
        (None, "PaymentScreen", "PaymentScreen"),
        ("DAILY_LIMIT_EXCEEDED", None, "DAILY_LIMIT_EXCEEDED"),
        ("", "PaymentScreen", "PaymentScreen"),
        ("  ", "  ", ""),
        (None, None, ""),
    ],
)
def test_prefetch_query_drops_absent_halves_instead_of_rendering_them(
    error_code: str | None, screen_name: str | None, expected: str
) -> None:
    """A call can open from a screen with no active error, or with no
    screen context at all. `f"{error_code} {screen_name}"` would embed the
    literal token "None" — pure noise in a query whose whole job is
    similarity."""
    assert prefetch_query(error_code, screen_name) == expected


# --------------------------------------------------------------------------
# Retrieval policy
# --------------------------------------------------------------------------


async def test_retrieve_embeds_the_query_and_searches_with_that_vector() -> None:
    memory, embeddings, repo = make_memory()
    embeddings.script(tuple([0.5] * EMBEDDING_DIM))

    await memory.retrieve("how do I raise it\nintent: limit_increase", PRINCIPAL)

    # One batched call with the single query text — EmbeddingProvider has
    # no single-text overload, and a caller must not invent one.
    assert embeddings.calls == [["how do I raise it\nintent: limit_increase"]]
    assert repo.search.await_args.args[0] == tuple([0.5] * EMBEDDING_DIM)


async def test_retrieve_passes_the_principal_through_untouched() -> None:
    """This class holds no SQL; its entire contribution to the security
    invariant is handing the caller's principal to the one function that
    does. A principal substituted, defaulted, or dropped here would defeat
    a correct predicate downstream."""
    memory, _, repo = make_memory()
    principal = SessionUser(user_id="usr_priya02")

    await memory.retrieve("query", principal)

    assert repo.search.await_args.args[1] is principal


async def test_retrieve_uses_the_docs_09_defaults() -> None:
    """Top-3, floor 0.70 (docs/09 §6.2) — taken from the frozen constants,
    not re-spelled here."""
    memory, _, repo = make_memory()

    await memory.retrieve("query", PRINCIPAL)

    assert repo.search.await_args.kwargs["k"] == RETRIEVAL_TOP_K == 3


async def test_retrieve_drops_below_floor_results_rather_than_padding() -> None:
    """docs/09 §6.2: "Below-floor results are dropped, not padded — a
    300-token slot of marginally related KB text is worse than an empty
    slot, because the model treats retrieved text as more authoritative
    than it is."

    So a k of 3 that finds one good match returns ONE result, never three.
    """
    memory, _, _ = make_memory(
        [
            _memory(0.91, chunk_id=1, content="relevant"),
            _memory(0.69, chunk_id=2, content="marginal"),
            _memory(0.31, chunk_id=3, content="unrelated"),
        ]
    )

    results = await memory.retrieve("query", PRINCIPAL)

    assert [result.content for result in results] == ["relevant"]


async def test_retrieve_keeps_a_result_exactly_on_the_floor() -> None:
    """The floor is inclusive: 0.70 clears it. Pinned because `>` vs `>=`
    is a one-character change no other test would catch."""
    memory, _, _ = make_memory([_memory(RETRIEVAL_FLOOR)])

    assert len(await memory.retrieve("query", PRINCIPAL)) == 1


async def test_retrieve_returns_empty_when_nothing_clears_the_floor() -> None:
    """An empty RAG slot is a valid, intended outcome — not a fallback to
    the best-of-a-bad-lot."""
    memory, _, _ = make_memory([_memory(0.69), _memory(0.55)])

    assert await memory.retrieve("query", PRINCIPAL) == []


async def test_retrieve_honours_a_caller_supplied_floor() -> None:
    memory, _, _ = make_memory([_memory(0.91), _memory(0.75)])

    assert len(await memory.retrieve("query", PRINCIPAL, floor=0.80)) == 1


async def test_retrieve_passes_a_caller_supplied_k_to_the_repo() -> None:
    """`k` is a SQL LIMIT applied after scoping, so it must reach the
    repo — trimming in Python instead would mean the database transported
    rows the caller did not ask for."""
    memory, _, repo = make_memory()

    await memory.retrieve("query", PRINCIPAL, k=5)

    assert repo.search.await_args.kwargs["k"] == 5


async def test_retrieve_skips_the_embedding_call_for_a_blank_query() -> None:
    """The prefetch case where neither an error code nor a screen name was
    available. Embedding "" would spend a network hop to retrieve whatever
    sits nearest the origin of the vector space — noise dressed as
    relevance."""
    memory, embeddings, repo = make_memory([_memory(0.99)])

    assert await memory.retrieve("   ", PRINCIPAL) == []
    assert embeddings.calls == []
    repo.search.assert_not_awaited()


async def test_retrieve_lets_an_embedding_failure_propagate() -> None:
    """docs/09 §11 makes an embedding outage drop-rung 1 — the RAG slot is
    skipped entirely. That decision belongs to the context-assembly layer
    that owns the slot; swallowing it here would make "retrieval is down"
    indistinguishable from "nothing relevant was found"."""
    repo = AsyncMock(spec=SemanticRepo)
    embeddings = AsyncMock()
    embeddings.embed.side_effect = RuntimeError("embeddings 503")
    memory = SemanticMemory(embeddings, repo)

    with pytest.raises(RuntimeError, match="embeddings 503"):
        await memory.retrieve("query", PRINCIPAL)

    repo.search.assert_not_awaited()


async def test_retrieve_lets_a_database_failure_propagate() -> None:
    memory, _, repo = make_memory()
    repo.search.side_effect = RuntimeError("connection reset")

    with pytest.raises(RuntimeError, match="connection reset"):
        await memory.retrieve("query", PRINCIPAL)


# --------------------------------------------------------------------------
# Handoff 3 — conformance of the CONCRETE class, not the Protocol
# --------------------------------------------------------------------------


def _signature(func: object) -> inspect.Signature:
    return inspect.signature(func, eval_str=True)  # type: ignore[arg-type]


def test_concrete_retrieve_exposes_no_parameter_that_could_widen_scope() -> None:
    """**The test Batch 1 could not write.** Its contract-freeze test pins
    `SemanticMemoryProto.retrieve`'s parameter set, and its own docstring
    records what that leaves open, verified rather than assumed: a
    concrete class may legally add a defaulted keyword parameter and still
    satisfy the Protocol. `include_all_users: bool = False`,
    `user_id: str | None = None`, or a `kind` filter accepting "any" would
    each reintroduce the unscoped query the required `principal` was meant
    to make unreachable — on the very class that owns the WHERE clause —
    with mypy and every existing guard green.

    Pinning the exact parameter set of the implementation is what closes
    it. This is a signature check, so it still cannot prove the SQL is
    scoped; `test_search_statement_carries_both_branches_of_the_scoping_
    predicate` does that, and the Docker-gated
    `tests/models/test_semantic_repo_pg.py` proves it against real rows.
    """
    sig = _signature(SemanticMemory.retrieve)

    assert list(sig.parameters) == ["self", "query", "principal", "k", "floor"], (
        "SemanticMemory.retrieve must expose exactly docs/05 §3.7's parameters — "
        "any extra one is a candidate scope-widener (docs/09 §6.2, docs/14)"
    )


def test_concrete_retrieve_matches_the_frozen_protocol_signature_exactly() -> None:
    """Stronger than the parameter-name check above and kept alongside it:
    equality covers annotations, defaults, and parameter kinds in one
    assertion, so `principal: SessionUser | None = None` fails here even
    though the name list would still match."""
    assert _signature(SemanticMemory.retrieve) == _signature(SemanticMemoryProto.retrieve)


def test_concrete_retrieve_requires_a_principal_positionally() -> None:
    """Belt and braces on the single parameter the invariant rests on:
    required, undefaulted, and the same `SessionUser` `ToolExecutor`
    injects (docs/10 §1 invariant 3) — not a bare user_id string a caller
    could assemble from untrusted input."""
    principal = _signature(SemanticMemory.retrieve).parameters["principal"]

    assert principal.default is inspect.Parameter.empty
    assert principal.annotation is SessionUser

    with pytest.raises(TypeError):
        SemanticMemory.retrieve(make_memory()[0], "query")  # type: ignore[call-arg]


def test_semantic_memory_exposes_no_second_retrieval_method() -> None:
    """A conformance check on `retrieve` is worth nothing if an unscoped
    `retrieve_all()` can sit beside it. The public surface is frozen to
    the one method the Protocol names."""
    public = {
        name
        for name, value in vars(SemanticMemory).items()
        if not name.startswith("_") and inspect.isfunction(value)
    }

    assert public == {"retrieve"}


# No `assert isinstance(memory, SemanticMemoryProto)`-style conformance test
# here, deliberately. `SemanticMemoryProto` is not `runtime_checkable`, and
# the repo's type gate is `mypy app` — it never type-checks `tests/`. So an
# annotated assignment (`x: SemanticMemoryProto = memory`) would execute
# without asserting anything at all: nothing checks it at runtime and
# nothing checks it at build time either. An earlier draft of this file had
# exactly that test and it was removed rather than left to look like
# coverage. `test_concrete_retrieve_matches_the_frozen_protocol_signature_
# exactly` above is the real conformance check — it compares the two
# signatures itself, which does run.
