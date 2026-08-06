"""`ConversationSummaryStore` — the durable half of memory layer 3
(docs/09-memory-architecture.md §1's table: "`Summarizer` (rolling) +
`ConversationSummaryStore` (final) | Redis field in-call → Postgres
`conversation_summaries` post-call").

`Summarizer` (`app/memory/summarizer.py`) owns the in-call rolling
summary in Redis. This owns the once-per-call row that outlives the call:
the final fold plus how the call resolved, what it was about, which tools
ran, and what it cost — the shape docs/09 §8 pins and Phase-5 Batch 1
created.

Judgment calls, flagged per house style:

1. **This owns the session/transaction boundary; `ConversationRepo` owns
   the SQL.** Same split as `CostTracker`/`CostRepo`: the repo takes a
   caller-supplied `AsyncSession` and never commits (`base.py`'s rule),
   and this class opens a short-lived session per call because the
   post-call pipeline runs outside any request-scoped `get_db` session.
   `repo_factory` is the testability seam, defaulting to the real repo.

2. **`save()` is idempotent, deliberately.** docs/09 §8: "the pipeline is
   idempotent — `session_id` is the primary key everywhere it writes...
   A crash mid-pipeline is retried whole from the still-live Redis hash."
   A `save()` that raised on a second attempt would make that documented
   retry impossible, so it upserts.

3. **This now embeds, and it does so inside `save()`'s own session —
   CLOSED by the post-call-summary-profile-wiring task, superseding the
   "no implementation yet" note this judgment call used to carry.**
   `EmbeddingProvider` had no concrete class when this was written; the
   concrete class (`app.providers.openai_embeddings.OpenAIEmbeddings`)
   shipped in Phase-5 T2a, before this task, and is what `embeddings`
   below is typed against. The chosen path is the first of the two this
   note always named as the way to close the gap: `save()` extends with
   an embed-and-insert step inside the SAME `db_session` `upsert_summary`
   already used, wrapped in `db_session.begin_nested()` (a SQL
   SAVEPOINT) — not the second path (taking over the session boundary
   entirely), because the summary upsert's own transaction shape did not
   need to change, only gain one more write inside it.

   **The insert itself goes through a new `SemanticRepo.add_call_summary()`
   method, not a `MemoryChunk` row built here.**
   `tests/data/repositories/test_semantic_repo.py`'s own
   `test_semantic_repo_is_the_only_module_in_app_that_reaches_memory_chunks`
   enforces that only `app/models/orm.py` and `semantic_repo.py` may
   import the `MemoryChunk` ORM class anywhere in `app/` — this module
   passes `add_call_summary()` the primitive fields instead
   (`source_id`/`content`/`embedding`/`user_id`) and never imports the
   ORM class itself, so the guard's "the query path stays the one
   reader" property extends to the write path this task adds.

   **Why a SAVEPOINT and not a bare try/except around the insert.** A
   caught Python exception does not, by itself, undo a poisoned SQL
   transaction: if `SemanticRepo.add_call_summary()`'s `flush()` fails at
   the DB level (a real constraint violation, not just the embeddings HTTP
   call raising before touching the DB), Postgres marks the surrounding
   transaction as aborted, and every statement after that — including
   the `db_session.commit()` this method's caller depends on to persist
   the summary row it already wrote — fails too
   (`InFailedSqlTransactionError`), silently taking the summary write
   down with it. `begin_nested()` opens a SAVEPOINT; on an exception
   inside its `async with` block, only that SAVEPOINT rolls back, and
   the outer transaction — carrying the already-flushed
   `upsert_summary` — is untouched and free to commit. This is the one
   behavioral property the whole embed-failure-isolation contract
   (docs/09 §8: "An embedding outage queues the `memory_chunks` insert
   for retry without blocking the summary row") actually rests on; a
   plain try/except around the insert would look identical on the happy
   path and silently fail this exact contract the day the failure mode
   is a DB-level one instead of a network one.

   **"Retry" is a caught-and-logged skip, not a queue.** Building an
   actual retry queue for a failed embed is out of scope here, for the
   same reason `app/voice/run.py`'s `_end_with_retry` module comment
   gives for declining to build a durable reconciliation sweep: it needs
   its own composition-root decisions (which process runs it, on what
   schedule) that this task does not own. `_embed_and_insert_chunk`
   below catches, logs a distinctly greppable
   `conversation_summary_store.embed_failed_skipped` event (mirroring
   that module's own naming convention for exactly this class of
   decision), and returns — the summary row still commits, and the
   session's call summary simply has no vector until a future batch
   builds the sweep.

   **`embeddings`/`settings` are optional, together.** `None` for both
   (the default) means this store behaves exactly as it did before this
   task: no embed attempt, no `memory_chunks` row — the same "advisory,
   drop-rung 1" posture docs/09 §11 already documents for a missing
   `OPENAI_API_KEY` elsewhere in this codebase (`app/voice/run.py`'s
   `_build_brain_stack`: `embeddings = OpenAIEmbeddings(...) if
   settings.openai_api_key else None`). The constructor raises if only
   one of the pair is supplied — a caller that passes an `embeddings`
   provider with no `settings` (needed to construct `SemanticRepo`) has
   a wiring bug, not a legitimate half-configured state, and should hear
   about it at construction rather than at the first `save()`.

4. **Nothing here classifies `resolution` or extracts `intents`.** Those
   are post-call pipeline decisions (docs/09 §8's "Summarizer (Haiku):
   final summary + resolution"). This class validates and persists what
   it is handed; `ConversationSummary`'s `Resolution` enum is what stops
   a free-form string reaching the DB CHECK.
"""

from __future__ import annotations

from collections.abc import Callable
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.data.repositories.conversation_repo import ConversationRepo
from app.data.repositories.semantic_repo import SemanticRepo
from app.domain.interfaces import EmbeddingProvider
from app.domain.types import ConversationSummary, MemoryKind
from app.domain.types import MemoryChunk as MemoryChunkDomain
from app.models.orm import ConversationSummary as ConversationSummaryRow
from app.obs.logging import get_logger

log = get_logger(__name__)

# `conversation_summaries.cost_usd` is NUMERIC(8, 4) — the rounded display
# copy, against `call_costs`'s NUMERIC(10, 6) ledger precision (docs/12
# §4.5 splits these deliberately; app/models/orm.py repeats that it is not
# a typo). Quantizing at this boundary means the value that reaches
# Postgres is the value this module computed, not whatever the driver's
# own rounding would have produced.
_DISPLAY_MONEY_QUANTUM = Decimal("0.0001")


class ConversationSummaryStore:
    """Reads and writes `conversation_summaries` rows as frozen
    `app.domain.types.ConversationSummary` values.

    Unlike `Summarizer`, this is stateless and safe to share across
    sessions — it holds a session factory, not a call's state.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        repo_factory: Callable[[AsyncSession], ConversationRepo] = ConversationRepo,
        embeddings: EmbeddingProvider | None = None,
        settings: Settings | None = None,
        semantic_repo_factory: Callable[[AsyncSession, Settings], SemanticRepo] = SemanticRepo,
    ) -> None:
        if (embeddings is None) != (settings is None):
            # Fail fast at construction (judgment call #3, module
            # docstring): a half-configured pair is a wiring bug, not a
            # legitimate drop-rung state — the legitimate one is both
            # None.
            raise ValueError(
                "ConversationSummaryStore requires embeddings and settings together, or "
                "neither — got embeddings="
                f"{embeddings!r}, settings={settings!r}"
            )
        self._session_factory = session_factory
        self._repo_factory = repo_factory
        self._embeddings = embeddings
        self._settings = settings
        self._semantic_repo_factory = semantic_repo_factory

    async def save(self, summary: ConversationSummary) -> None:
        """Persist one call's durable summary. Idempotent on `session_id`
        (judgment call #2) — re-running the post-call pipeline overwrites
        the row rather than failing.

        `created_at` on the passed value is ignored: the column is
        server-assigned (`server_default=now()`), and a retry keeps the
        original timestamp.

        Also embeds `summary.summary` and inserts the matching
        `memory_chunks` row, inside this same session (judgment call #3):
        a failure there is isolated to its own SAVEPOINT and never
        prevents this method's own commit below.
        """
        async with self._session_factory() as db_session:
            repo = self._repo_factory(db_session)
            await repo.upsert_summary(
                summary.session_id,
                user_id=summary.user_id,
                summary=summary.summary,
                resolution=summary.resolution.value,
                # tuple -> list at the ARRAY(Text) boundary: the domain
                # type is frozen and uses tuples, the column binds a list.
                intents=list(summary.intents),
                tools_used=list(summary.tools_used),
                turn_count=summary.turn_count,
                duration_s=summary.duration_s,
                cost_usd=summary.cost_usd.quantize(
                    _DISPLAY_MONEY_QUANTUM, rounding=ROUND_HALF_UP
                ),
            )
            await self._embed_and_insert_chunk(db_session, summary)
            # The repo never commits (base.py); this method opened the
            # session, so this method commits it — same rule CostTracker
            # .finalize() follows. Reached even when the embed step above
            # was skipped or failed: only its own SAVEPOINT rolled back.
            await db_session.commit()
        log.info(
            "conversation_summary_store.saved",
            session_id=summary.session_id,
            resolution=summary.resolution.value,
            turn_count=summary.turn_count,
        )

    async def _embed_and_insert_chunk(
        self, db_session: AsyncSession, summary: ConversationSummary
    ) -> None:
        """The embed-and-insert step judgment call #3 (module docstring)
        describes. Never raises: every failure path — no provider
        configured, the embeddings HTTP call itself raising, or a
        DB-level failure on the insert — is caught here and logged, so
        `save()`'s caller (`SessionManager.end()`, docs/09 §8) never sees
        it and the summary row commits regardless.
        """
        if self._embeddings is None or self._settings is None:
            log.info(
                "conversation_summary_store.embed_skipped_no_provider",
                session_id=summary.session_id,
            )
            return
        try:
            async with db_session.begin_nested():
                (vector,) = await self._embeddings.embed([summary.summary])
                # Validated through the domain type first (embedding
                # width, the call_summary/user_id-required biconditional)
                # — the same pattern scripts/seed_kb.py's `_rebuild_chunks`
                # uses before handing a row to a repo, a safety net this
                # environment cannot get from a live DB CHECK alone.
                MemoryChunkDomain(
                    kind=MemoryKind.CALL_SUMMARY,
                    source_id=summary.session_id,
                    content=summary.summary,
                    embedding=vector,
                    user_id=summary.user_id,
                )
                repo = self._semantic_repo_factory(db_session, self._settings)
                await repo.add_call_summary(
                    source_id=summary.session_id,
                    content=summary.summary,
                    embedding=vector,
                    user_id=summary.user_id,
                )
        except Exception:
            # Judgment call #3: a caught-and-logged skip, not a retry
            # queue — docs/09 §8's "embedding outage queues the
            # memory_chunks insert for retry" is honored at the scope
            # this task owns (never block the summary row), not by
            # building the queue itself.
            log.warning(
                "conversation_summary_store.embed_failed_skipped",
                session_id=summary.session_id,
                exc_info=True,
            )

    async def get(self, session_id: str) -> ConversationSummary | None:
        """One call's durable summary, or `None` if the post-call pipeline
        has not landed it yet."""
        async with self._session_factory() as db_session:
            row = await self._repo_factory(db_session).get_summary(session_id)
            return _to_domain(row) if row is not None else None

    async def list_for_user(
        self, user_id: str, *, limit: int = 10
    ) -> tuple[ConversationSummary, ...]:
        """This merchant's summaries, newest first.

        Scoped by `user_id` in the query itself (`ConversationRepo
        .list_summaries_for_user`), so another merchant's summary is
        unreachable through this method by construction — docs/09 §6.2's
        rule applied to the plain recency read as well as to retrieval.
        `limit` is required to have a default rather than being unbounded:
        an uncapped read of a merchant's whole call history has no caller
        and would be a slot-budget hazard if one appeared.
        """
        async with self._session_factory() as db_session:
            rows = await self._repo_factory(db_session).list_summaries_for_user(
                user_id, limit=limit
            )
            return tuple(_to_domain(row) for row in rows)


def _to_domain(row: ConversationSummaryRow) -> ConversationSummary:
    """ORM row -> frozen domain value. `Resolution` is re-validated here
    by Pydantic: the DB CHECK constrains the column, but a row read back
    is still just a string until something says otherwise."""
    return ConversationSummary(
        session_id=row.session_id,
        user_id=row.user_id,
        summary=row.summary,
        resolution=row.resolution,  # type: ignore[arg-type]  # str -> Resolution, validated
        intents=tuple(row.intents),
        tools_used=tuple(row.tools_used),
        turn_count=row.turn_count,
        duration_s=row.duration_s,
        cost_usd=row.cost_usd,
        created_at=row.created_at,
    )


__all__ = ["ConversationSummaryStore"]
