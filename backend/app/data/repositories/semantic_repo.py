"""SemanticRepo — the **only** code in this application that queries
`memory_chunks` (docs/04-backend-architecture.md §5, docs/09 §6.2).

That exclusivity is the point, and it is the first of the three things
`SemanticMemoryProto`'s docstring hands to this batch. docs/09 §6.2 states
the tenant-scoping rule as SQL — `kind = 'kb_article' OR user_id =
:session_user` — and says it "lives in the one function that issues the
query". `search()` below is that function: it takes a `SessionUser` with no
default, composes `app.models.orm.memory_scope(principal)` rather than
re-deriving the OR, and there is no other method here (or anywhere in
`app/`) that reads the table. An unscoped semantic search therefore has no
method to call.

**How strong that actually is, stated plainly.** Python has no mechanism
that prevents someone from writing `select(MemoryChunk)` in a new module
tomorrow, or `session.execute(text("SELECT ... FROM memory_chunks"))`.
What this file buys is that doing so is a visible new call site rather
than a forgotten keyword argument, and
`tests/data/repositories/test_semantic_repo.py` fails the build when it
appears. The guard is a review aid backed by a test, not a language-level
guarantee — do not read it as more.

**Writes are absent on purpose — until the post-call-summary-profile-
wiring task, which is the batch this note anticipated.** `SemanticMemoryProto`
assigns indexing (KB seed, post-call embed + insert) to this repo rather
than to the retriever, so that a later batch adding a write path widens
*this* class and not the read interface the scoping argument depends on
staying narrow. `add_call_summary()` below is that write path: the ONE
new method, added for the ONE new caller
(`app/memory/conversation_summary_store.py`'s post-call embed step) —
`get()` is still refused, and `search()`'s read-side scoping argument is
untouched. `add`/`update` remain the plain inherited
`SqlAlchemyRepository` ones; `add_call_summary` is a thin, validated
wrapper over `add`, not a third write path.

**Why the `MemoryChunk` ORM row is built here and nowhere else.**
`tests/data/repositories/test_semantic_repo.py`'s own
`test_semantic_repo_is_the_only_module_in_app_that_reaches_memory_chunks`
enforces that this file — plus `app/models/orm.py`, which declares the
table — is the only place in `app/` allowed to import
`app.models.orm.MemoryChunk`. A caller that constructed the ORM row
itself and handed it to a generic `add()` would be a second reader of
that name; `add_call_summary()` takes the caller's primitive fields
instead (`source_id`/`content`/`embedding`/`user_id`) and builds the row
internally, so `app/memory/conversation_summary_store.py` never imports
the ORM class at all.
"""

from __future__ import annotations

from typing import Final

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.data.repositories.base import SqlAlchemyRepository
from app.domain.types import Embedding, MemoryKind, RetrievedMemory, SessionUser
from app.models.orm import MemoryChunk, memory_scope

# `SET LOCAL` takes no bind parameters, so both values are interpolated
# into SQL text. Neither is user-influenced (they are `Settings` fields,
# not request data), but they are validated anyway before interpolation —
# a config value reaching a SQL string unvalidated is the shape of the bug
# even when today's value happens to be safe.
_ITERATIVE_SCAN_MODES: Final = frozenset({"", "off", "relaxed_order", "strict_order"})

# Cosine distance and cosine similarity are exact complements (`similarity
# = 1 - distance`), but the subtraction is float arithmetic, so a vector
# identical to the query can land a hair outside [-1.0, 1.0] and trip
# `RetrievedMemory`'s range validator. Snap only within this epsilon of a
# bound; anything further out is a real error (an L2 or inner-product
# operator misread as cosine, say) and is left to raise.
_SIMILARITY_EPSILON: Final = 1e-9


class SemanticRepo(SqlAlchemyRepository[MemoryChunk]):
    """Takes the request/transaction-scoped `AsyncSession` every repo takes
    (docs/04 §4: "one AsyncSession = one transaction"), plus `Settings` for
    the HNSW query-time knobs `search()` sets. Never commits or rolls back
    — that boundary belongs to whatever opened the session.
    """

    model = MemoryChunk

    def __init__(self, session: AsyncSession, settings: Settings) -> None:
        super().__init__(session)
        self._settings = settings

    async def get(self, id: str) -> MemoryChunk:
        """Refused. The inherited implementation is
        `session.get(MemoryChunk, id)` — a primary-key fetch with no
        principal and no `memory_scope`, i.e. a cross-tenant read
        primitive sitting on the one class that claims to be the only
        scoped path to this table. It was unreachable only because
        nothing in `app/` constructs this repo yet; a security review
        pointed out that stops being true the moment a later batch wires
        it, and "safe because nobody calls it" is not a property the code
        enforces.

        Raising rather than scoping is deliberate. A scoped keyed fetch
        would need a `principal` argument, which changes the signature
        the `Repository` Protocol pins and buys nothing today — no caller
        wants one. If a later batch genuinely needs to fetch a chunk by
        id, it should add a named method that takes a principal, which is
        a visible new call site rather than a silently inherited one.
        """
        raise NotImplementedError(
            "SemanticRepo.get() is not available: a primary-key fetch bypasses "
            "memory_scope() and would read across merchants. Use search(), or add "
            "a principal-scoped fetch method if one is genuinely needed."
        )

    async def add_call_summary(
        self, *, source_id: str, content: str, embedding: Embedding, user_id: str
    ) -> MemoryChunk:
        """Insert one `kind='call_summary'` row — the post-call embed
        stage's write path (docs/09 §8), and the module docstring's "the
        batch this note anticipated" write. The sole caller is
        `ConversationSummaryStore._embed_and_insert_chunk`.

        `user_id` is required here, not optional the way
        `app.domain.types.MemoryChunk`'s own field is (that type also
        accepts `None`, for a `kb_article`) — this method only ever
        builds a `call_summary` row, which `ck_memory_chunks_user_scope`
        requires a non-null `user_id` for, so a narrower signature here
        is what stops a caller passing `None` by mistake and silently
        writing a row unreachable by its own owner (docs/09 §6.1's
        biconditional, restated on `MemoryChunk`'s own docstring).

        Deliberately does not re-validate through
        `app.domain.types.MemoryChunk` itself — the caller already does
        (the same "validate through the domain type first" pattern
        `scripts/seed_kb.py`'s `_rebuild_chunks` uses), and duplicating
        it here would check the same invariants twice for no new
        coverage. What this method owns is the ORM construction: it is
        the one place in `app/` allowed to import the `MemoryChunk`
        class (see the module docstring).
        """
        return await self.add(
            MemoryChunk(
                kind=MemoryKind.CALL_SUMMARY.value,
                source_id=source_id,
                content=content,
                embedding=list(embedding),
                user_id=user_id,
            )
        )

    async def search(
        self, embedding: Embedding, principal: SessionUser, *, k: int
    ) -> list[RetrievedMemory]:
        """docs/09 §6.2's scoped top-k cosine query — the single call site
        of the security invariant.

        Returns the `k` nearest chunks the principal is allowed to see,
        nearest first, scored as cosine *similarity* (higher is better).
        The 0.70 floor is not applied here: `SemanticMemory.retrieve()`
        owns that policy, this owns the SQL.

        ---
        **The HNSW post-filter hazard, and what is done about it.**

        pgvector's HNSW index collects candidates by vector distance first
        and only then applies the `WHERE` clause. `memory_chunks.user_id`
        is unindexed, so with a plain ANN scan the engine walks the graph
        for roughly `ef_search` nearest rows *globally*, hands them up, and
        the scoping predicate deletes the ones belonging to other
        merchants. A merchant whose own summaries all sit outside that
        global candidate set gets **zero of their own rows back** — not an
        error, just an empty RAG slot that reads as "no relevant memory".
        pgvector's own README puts a number on it: at the default
        `ef_search` of 40, a condition matching 10% of rows returns about
        4 rows on average.

        Three levers exist. This method uses two of them:

        1. **`hnsw.iterative_scan`** (`SET LOCAL`, below) is the only one
           that raises the ceiling instead of merely moving it: the scan
           resumes and fetches further candidates until `k` rows have
           *passed the filter*. `strict_order` rather than
           `relaxed_order` because top-k and the 0.70 floor are both
           expressed in similarity order — `relaxed_order` is documented
           to return rows slightly out of distance order, which inside a
           `LIMIT k` lets a worse row displace a better one and silently
           changes which three chunks reach the prompt.
        2. **`hnsw.ef_search`** raised above pgvector's default of 40
           widens the initial candidate set, which helps and is cheap, but
           on its own it only moves the cliff — for any `ef_search` there
           is a corpus where a merchant's rows fall past it. Its ceiling
           is 1000, enforced by pgvector.
        3. A **partial index** is the third documented lever and is not
           usable here: the scoped predicate's second branch is
           per-merchant, so it would need one partial index per merchant,
           unbounded in the number of merchants.

        **Iterative scan is a mitigation, not a guarantee — say so.** It
        stops at `hnsw.max_scan_tuples` (default 20000) or when the
        candidate list exceeds `hnsw.scan_mem_multiplier * work_mem`,
        whichever comes first, and returns whatever it has. So a merchant
        with very few summaries inside a very large foreign corpus can
        still come back short. Neither bound is set here (both are left at
        their pgvector defaults) and nothing in this method detects
        hitting one: a truncated scan is indistinguishable from a genuine
        "nothing else was relevant". What this method fixes is the common
        case — the ~40-candidate cliff — not the general problem, and the
        general problem is bounded by tuning those two knobs at the
        deployment, not by code here.

        **Over-fetching and filtering in Python is forbidden**, and this
        method does not do it: a larger global top-k pulls other
        merchants' call summaries into application memory, which is
        exactly the exposure the predicate exists to prevent. The `LIMIT`
        below is the final `k`, applied by the database *after* the scope.

        **Both settings are `SET LOCAL`** — scoped to the current
        transaction and reverted at its end, so this method cannot leak
        planner state into unrelated queries on a pooled connection.
        `SET LOCAL` outside a transaction block is a no-op with a Postgres
        WARNING; SQLAlchemy's `AsyncSession` autobegins on the first
        statement, so the `SET LOCAL`s here open the transaction the
        `SELECT` then runs inside. A caller that deliberately runs this
        session in autocommit would silently lose both knobs.

        **Honest scale caveat** (docs/09 §6.1): at the demo's ~200 rows
        the planner picks a sequential scan and none of this is exercised
        — a seq scan applies the filter directly and has no recall
        problem. Everything above matters from the moment the corpus is
        big enough for the index to be chosen, which is also the moment it
        would otherwise start silently dropping merchants' own memories.
        """
        await self._apply_hnsw_settings()

        distance = MemoryChunk.embedding.cosine_distance(list(embedding)).label("distance")
        statement = (
            select(
                MemoryChunk.id,
                MemoryChunk.kind,
                MemoryChunk.source_id,
                MemoryChunk.content,
                MemoryChunk.user_id,
                distance,
            )
            # The security invariant. Composed, never hand-written — see
            # memory_scope()'s docstring for why the predicate has exactly
            # one definition.
            .where(memory_scope(principal))
            .order_by(distance)
            .limit(k)
        )
        result = await self._session.execute(statement)
        return [
            RetrievedMemory(
                chunk_id=row.id,
                kind=MemoryKind(row.kind),
                source_id=row.source_id,
                content=row.content,
                user_id=row.user_id,
                similarity=_to_similarity(row.distance),
            )
            for row in result
        ]

    async def _apply_hnsw_settings(self) -> None:
        """`SET LOCAL` the two HNSW knobs `search()`'s docstring explains.

        `hnsw.iterative_scan` is skipped entirely when configured empty,
        because it does not exist before pgvector 0.8.0 and the behaviour
        of setting it on such a server is genuinely ugly: pgvector reserves
        the `hnsw` GUC prefix, so once its library is loaded into the
        backend an unknown `hnsw.*` name is `ERROR: invalid configuration
        parameter name`, which aborts the surrounding transaction and
        takes retrieval down entirely. Before the library has loaded on
        that connection — it loads lazily, on first use of a pgvector
        function — the same statement instead succeeds as a placeholder
        and is discarded when the library does load. Which of the two
        happens depends on whether anything has touched a vector on that
        pooled connection yet, so on an old server this fails
        *nondeterministically*. The empty-string escape hatch exists
        because that is not a failure mode worth debugging in production.

        Nothing here probes the server version; that is an operator
        decision recorded in config. (On a supported server the ordering
        is not a problem in the other direction: a `SET` that precedes the
        library load lands on the real GUC once pgvector defines it.)

        The two range checks below mirror pgvector's own, so a bad config
        value fails at the first search with a message naming the field
        rather than as a Postgres error mid-transaction.
        """
        # Both values are validated before either is executed. The
        # interleaved order (check ef_search, SET it, then check mode) left
        # one statement already sent when an invalid mode raised — harmless,
        # since SET LOCAL dies with the transaction the caller rolls back,
        # but it contradicted this docstring's "fails at the first search"
        # framing by half-applying first. Validate, then act.
        ef_search = int(self._settings.memory_hnsw_ef_search)
        if not 1 <= ef_search <= 1000:
            raise ValueError(
                f"memory_hnsw_ef_search must be in 1..1000, got {ef_search} — "
                "that is pgvector's own range for hnsw.ef_search, and a value "
                "outside it is rejected by Postgres mid-transaction"
            )

        mode = self._settings.memory_hnsw_iterative_scan
        if mode not in _ITERATIVE_SCAN_MODES:
            raise ValueError(
                f"memory_hnsw_iterative_scan must be one of "
                f"{sorted(_ITERATIVE_SCAN_MODES)}, got {mode!r}"
            )

        await self._session.execute(text(f"SET LOCAL hnsw.ef_search = {ef_search}"))
        if mode:
            await self._session.execute(text(f"SET LOCAL hnsw.iterative_scan = {mode}"))


def _to_similarity(distance: float) -> float:
    """Cosine distance (lower is better, what pgvector's `<=>` returns) to
    cosine similarity (higher is better, what the 0.70 floor is expressed
    in). Getting this backwards would invert the floor comparison and admit
    exactly the marginal results the floor exists to drop.

    The epsilon snap covers float error at the bounds only — see
    `_SIMILARITY_EPSILON`. Values further out are returned unchanged and
    `RetrievedMemory`'s validator rejects them, which is the intended
    outcome: a distance that cannot be a cosine distance means the wrong
    operator was used, and that should be loud.
    """
    similarity = 1.0 - distance
    if 1.0 < similarity <= 1.0 + _SIMILARITY_EPSILON:
        return 1.0
    if -1.0 - _SIMILARITY_EPSILON <= similarity < -1.0:
        return -1.0
    return similarity


__all__ = ["SemanticRepo"]
