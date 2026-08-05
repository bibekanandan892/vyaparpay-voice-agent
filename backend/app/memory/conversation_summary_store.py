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

3. **This does not embed anything.** docs/09 §8's pipeline writes the
   summary row *and* a `memory_chunks` row with `kind='call_summary'`,
   and docs/04's transaction-boundary note wants both in one transaction.
   The embed is a separate task with no implementation yet
   (`EmbeddingProvider` is a Protocol with no concrete class). Stating
   the consequence plainly rather than implying otherwise: **a summary
   saved through this class today has no vector, and the one-transaction
   property docs/04 asks for is not delivered by this file** — closing
   that needs the embed task to either extend `save()` with an
   embed-and-insert step inside the same session, or to take over the
   session boundary entirely.

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

from app.data.repositories.conversation_repo import ConversationRepo
from app.domain.types import ConversationSummary
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
    ) -> None:
        self._session_factory = session_factory
        self._repo_factory = repo_factory

    async def save(self, summary: ConversationSummary) -> None:
        """Persist one call's durable summary. Idempotent on `session_id`
        (judgment call #2) — re-running the post-call pipeline overwrites
        the row rather than failing.

        `created_at` on the passed value is ignored: the column is
        server-assigned (`server_default=now()`), and a retry keeps the
        original timestamp.
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
            # The repo never commits (base.py); this method opened the
            # session, so this method commits it — same rule CostTracker
            # .finalize() follows.
            await db_session.commit()
        log.info(
            "conversation_summary_store.saved",
            session_id=summary.session_id,
            resolution=summary.resolution.value,
            turn_count=summary.turn_count,
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
