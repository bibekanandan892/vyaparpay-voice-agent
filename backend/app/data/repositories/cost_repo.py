"""CostRepo — repository for `call_costs` (docs/04-backend-architecture.md
§5), the per-call cost ledger `CostTracker.finalize()` (docs/05-agent-
architecture.md §3.8, a later batch) writes exactly once, at hang-up
(docs/09-memory-architecture.md §8).

`upsert`, not `insert`: `finalize()` is documented as a single write per
call, but idempotent-on-retry is cheap insurance if `SessionManager.end()`
(a later batch) ever calls it twice for the same session — a second call
corrects the row instead of raising a PK-conflict `IntegrityError` that
would mask whatever caused the double call.
"""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.data.repositories.base import SqlAlchemyRepository
from app.models import CallCost


class CostRepo(SqlAlchemyRepository[CallCost]):
    model = CallCost

    async def upsert(
        self,
        session_id: str,
        *,
        stt_usd: Decimal,
        llm_dialogue_usd: Decimal,
        llm_utility_usd: Decimal,
        embeddings_usd: Decimal,
        tts_usd: Decimal,
        total_usd: Decimal,
        stt_seconds: int,
        input_tokens: int,
        cached_input_tokens: int,
        output_tokens: int,
        tts_chars: int,
        turn_infra_usd: Decimal = Decimal("0"),
    ) -> None:
        """`INSERT ... ON CONFLICT (session_id) DO UPDATE` — every
        column `call_costs` (docs/12-data-models.md §4.5) defines beyond
        the server-generated `created_at`. `total_usd` must already
        equal the component sum before this call —
        `ck_call_costs_total_usd` (app/models/orm.py) enforces that at
        flush; this method does not compute the sum itself, `CostTracker`
        owns that arithmetic (docs/05 §3.8).
        """
        stmt = pg_insert(CallCost).values(
            session_id=session_id,
            stt_usd=stt_usd,
            llm_dialogue_usd=llm_dialogue_usd,
            llm_utility_usd=llm_utility_usd,
            embeddings_usd=embeddings_usd,
            tts_usd=tts_usd,
            turn_infra_usd=turn_infra_usd,
            total_usd=total_usd,
            stt_seconds=stt_seconds,
            input_tokens=input_tokens,
            cached_input_tokens=cached_input_tokens,
            output_tokens=output_tokens,
            tts_chars=tts_chars,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=[CallCost.session_id],
            set_={
                "stt_usd": stmt.excluded.stt_usd,
                "llm_dialogue_usd": stmt.excluded.llm_dialogue_usd,
                "llm_utility_usd": stmt.excluded.llm_utility_usd,
                "embeddings_usd": stmt.excluded.embeddings_usd,
                "tts_usd": stmt.excluded.tts_usd,
                "turn_infra_usd": stmt.excluded.turn_infra_usd,
                "total_usd": stmt.excluded.total_usd,
                "stt_seconds": stmt.excluded.stt_seconds,
                "input_tokens": stmt.excluded.input_tokens,
                "cached_input_tokens": stmt.excluded.cached_input_tokens,
                "output_tokens": stmt.excluded.output_tokens,
                "tts_chars": stmt.excluded.tts_chars,
            },
        )
        await self._session.execute(stmt)
        await self._session.flush()
