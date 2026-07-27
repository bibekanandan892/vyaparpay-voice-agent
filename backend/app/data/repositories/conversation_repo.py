"""ConversationRepo — repository for `conversations` + `conversation_turns`
(docs/04-backend-architecture.md §5's mapping also names
`conversation_summaries`, which is out of Phase-2's 8-table scope,
docs/17-roadmap.md §2.2 — this repo only touches the two tables that
exist today).

`conversations` is written once at session creation and once at hang-up
(docs/12-data-models.md §4.1: "state... is updated once, at hang-up");
`conversation_turns` is append-only, one row per turn, written by the
post-call pipeline draining `session:{id}:turns` from Redis (docs/12
§7) — Phase 2's `SessionManager.end()` (a later batch) is the caller of
`append_turn`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from app.data.repositories.base import SqlAlchemyRepository
from app.models import Conversation, ConversationTurn


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
