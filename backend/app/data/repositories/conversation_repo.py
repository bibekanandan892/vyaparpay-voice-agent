"""ConversationRepo — repository for `conversations`,
`conversation_turns`, and `conversation_summaries`: the three tables
docs/04-backend-architecture.md §5's mapping assigns to this repo.
`conversation_summaries` was out of Phase-2's 8-table scope
(docs/17-roadmap.md §2.2) and joined it when Phase-5 Batch 1 created the
table.

`conversations` is written once at session creation and once at hang-up
(docs/12-data-models.md §4.1: "state... is updated once, at hang-up");
`conversation_turns` is append-only, one row per turn, written by the
post-call pipeline draining `session:{id}:turns` from Redis (docs/12
§7) — Phase 2's `SessionManager.end()` (a later batch) is the caller of
`append_turn`. `conversation_summaries` is written once per call by the
post-call pipeline (docs/09 §8), through `ConversationSummaryStore`
(`app/memory/`), which owns the session/transaction boundary and the
domain-type mapping this repo deliberately does not.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.data.repositories.base import SqlAlchemyRepository
from app.models import Conversation, ConversationTurn

# Imported from `app.models.orm` rather than `app.models`, and aliased,
# for two reasons stated rather than left to look like an oversight:
# the Phase-5 Batch-1 models are not re-exported by `app/models/__init__.py`
# (whose docstring's "every mapped class" claim predates them), and the
# `Row` suffix is the disambiguation `orm.py`'s own docstring prescribes
# against the frozen `app.domain.types.ConversationSummary` twin — which
# `ConversationSummaryStore` imports alongside this one.
from app.models.orm import ConversationSummary as ConversationSummaryRow


class ConversationRepo(SqlAlchemyRepository[Conversation]):
    model = Conversation

    async def create(
        self, session_id: str, user_id: str, signaling_token_hash: str
    ) -> Conversation:
        """`state` defaults to `'created'` (server_default, docs/12
        §4.1) — this method never sets it explicitly, so a fresh
        conversation always starts in the database's own default state
        rather than a value this file could drift from the schema."""
        conversation = Conversation(
            session_id=session_id, user_id=user_id, signaling_token_hash=signaling_token_hash
        )
        return await self.add(conversation)

    async def end(self, session_id: str) -> Conversation | None:
        """Sets `state='ended'`, `ended_at=now()` (docs/12 §4.1).
        Returns `None` if `session_id` doesn't exist rather than
        raising — Phase 2's `SessionManager.end()` (a later batch)
        decides whether that's an error worth surfacing."""
        conversation = await self._session.get(Conversation, session_id)
        if conversation is None:
            return None
        conversation.state = "ended"
        conversation.ended_at = datetime.now(UTC)
        await self._session.flush()
        return conversation

    async def append_turn(
        self,
        session_id: str,
        turn_no: int,
        role: str,
        *,
        latency_ms: int | None = None,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        tool_calls: list[str] | None = None,
        cost_usd: Decimal | None = None,
        trace_id: str | None = None,
        text: str | None = None,
        started_at: datetime | None = None,
    ) -> ConversationTurn:
        """One `conversation_turns` row (docs/12 §4.2), keyed on the
        composite `(session_id, turn_no)` PK. `text` stays `None` on
        every Phase-2 call — docs/12 §4.2's transcript-non-persistence
        decision — callers simply never pass it; the parameter exists so
        the column isn't permanently unreachable through this repo."""
        turn = ConversationTurn(
            session_id=session_id,
            turn_no=turn_no,
            role=role,
            latency_ms=latency_ms,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            tool_calls=tool_calls if tool_calls is not None else [],
            cost_usd=cost_usd,
            trace_id=trace_id,
            text=text,
            started_at=started_at if started_at is not None else datetime.now(UTC),
        )
        # Not self.add(turn): this repo is SqlAlchemyRepository[Conversation]
        # (one generic param), but manages two entity types — add() is
        # correctly typed for Conversation only, and ConversationTurn would
        # be a real type mismatch there (confirmed via mypy after an
        # earlier attempt to "de-duplicate" this into self.add(turn), which
        # is exactly the kind of thing that looks like harmless DRY but
        # isn't).
        self._session.add(turn)
        await self._session.flush()
        return turn

    async def list_turns(self, session_id: str) -> list[ConversationTurn]:
        """Every drained `conversation_turns` row for one session, in
        `turn_no` order. Added for `GET /v1/sessions/{id}/summary`
        (docs/13 §2.3), whose `turn_count` is a count of these rows —
        one row per turn, since the table's PK is `(session_id, turn_no)`
        (docs/12 §4.2).

        The class docstring's "append-only in Phase 2 — nothing reads one
        back" (and `base.py`'s note that this repo exposes no keyed `get`
        for turns) still holds: this is a whole-session read for the
        post-call summary, not a keyed lookup, so it doesn't reopen the
        composite-PK `get` question base.py deliberately declined.
        """
        result = await self._session.execute(
            select(ConversationTurn)
            .where(ConversationTurn.session_id == session_id)
            .order_by(ConversationTurn.turn_no)
        )
        return list(result.scalars().all())

    # ------------------------------------------------------------------
    # conversation_summaries (docs/09 §8, docs/12 §4.3)
    # ------------------------------------------------------------------

    async def upsert_summary(
        self,
        session_id: str,
        *,
        user_id: str,
        summary: str,
        resolution: str,
        intents: list[str],
        tools_used: list[str],
        turn_count: int,
        duration_s: int,
        cost_usd: Decimal,
    ) -> None:
        """One `conversation_summaries` row, upserted on the `session_id`
        primary key.

        Upsert rather than insert because docs/09 §8 requires the whole
        post-call pipeline to be retryable as a unit — "a crash
        mid-pipeline is retried whole from the still-live Redis hash" —
        which means this write must be safe to repeat. Same
        `on_conflict_do_update` shape as `CostRepo.upsert`, for the same
        reason.

        `created_at` is deliberately absent from the update set: on a
        retry the row keeps its original server-assigned timestamp rather
        than drifting to the retry's clock.

        No `commit()` — `base.py`'s rule: that boundary belongs to
        whatever opened the session (`ConversationSummaryStore`).
        """
        stmt = pg_insert(ConversationSummaryRow).values(
            session_id=session_id,
            user_id=user_id,
            summary=summary,
            resolution=resolution,
            intents=intents,
            tools_used=tools_used,
            turn_count=turn_count,
            duration_s=duration_s,
            cost_usd=cost_usd,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=[ConversationSummaryRow.session_id],
            set_={
                "user_id": stmt.excluded.user_id,
                "summary": stmt.excluded.summary,
                "resolution": stmt.excluded.resolution,
                "intents": stmt.excluded.intents,
                "tools_used": stmt.excluded.tools_used,
                "turn_count": stmt.excluded.turn_count,
                "duration_s": stmt.excluded.duration_s,
                "cost_usd": stmt.excluded.cost_usd,
            },
        )
        await self._session.execute(stmt)
        await self._session.flush()

    async def get_summary(self, session_id: str) -> ConversationSummaryRow | None:
        """The durable `conversation_summaries` row for one call, or
        `None` before the post-call pipeline has landed it.

        Explicitly NOT what `GET /v1/sessions/{id}/summary` reads. That
        endpoint (`app/api/routes/sessions.py`) gates its 404 +
        `Retry-After` on `conversations.state`, and assembles its payload
        mechanically from `conversation_turns` + `tool_invocations` — it
        never touches this table. Naming it here would suggest a wiring
        that does not exist; whether that endpoint should serve this row
        instead is a live question this method does not settle.

        Not `self.get`: that is typed for `Conversation`, this repo's one
        generic parameter — the same reason `append_turn` adds its entity
        directly rather than through `self.add`.
        """
        return await self._session.get(ConversationSummaryRow, session_id)

    async def list_summaries_for_user(
        self, user_id: str, *, limit: int
    ) -> list[ConversationSummaryRow]:
        """This merchant's past-call summaries, newest first — the read
        `idx_summaries_user` (docs/12 §4.3) exists for.

        `user_id` is a WHERE clause, not a filter applied after the fact:
        summaries are per-merchant records and docs/09 §6.2 makes
        cross-merchant reachability a security invariant. This method
        cannot return another merchant's row. It is NOT the retrieval
        path — that is `SemanticMemory` over `memory_chunks`, a different
        task; this is the plain recency read.
        """
        result = await self._session.execute(
            select(ConversationSummaryRow)
            .where(ConversationSummaryRow.user_id == user_id)
            .order_by(ConversationSummaryRow.created_at.desc())
            .limit(limit)
        )
        return list(result.scalars().all())
