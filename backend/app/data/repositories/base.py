"""Generic base fulfilling the floor of `app.domain.interfaces.Repository[T]`
(docs/04-backend-architecture.md §5) for repositories whose aggregate is
keyed by a single primary-key column that `AsyncSession.get()` accepts
directly.

Composite-PK aggregates don't fit `get(id: str)`'s single-argument shape —
`MerchantLimit` is keyed on `(merchant_id, limit_type)` and
`ConversationTurn` on `(session_id, turn_no)` (app/models/orm.py). Per
`app.domain.interfaces.Repository`'s own docstring ("this Protocol is the
floor, not the ceiling"), `LimitRepo` overrides `get` with a two-argument
signature instead, and `ConversationRepo` never exposes a keyed `get` for
turns at all (they're append-only in Phase 2 — nothing reads one back by
key). Both still inherit `add`/`update` from here unchanged, since those
operate on a whole entity instance, not a key shape.

Every subclass takes a request/transaction-scoped `AsyncSession` in its
constructor and never opens its own engine or session (docs/04 §4's DI
split: `get_db` opens exactly one `AsyncSession` per request — "one
AsyncSession = one transaction"). This class never calls `commit()` or
`rollback()`; that boundary belongs to whatever opened the session.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Base


class SqlAlchemyRepository[ModelT: Base]:
    """Concrete subclasses set the `model` class attribute to their
    mapped ORM class (e.g. `model = Merchant`) and get `get`/`add`/
    `update` for free; each then adds whatever domain-specific methods
    its aggregate needs beyond that floor (docs/04 §5: "repos with
    composite keys... add domain-specific methods beyond this minimum —
    this Protocol is the floor, not the ceiling").
    """

    model: type[ModelT]

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, id: str) -> ModelT | None:
        return await self._session.get(self.model, id)

    async def add(self, entity: ModelT) -> ModelT:
        self._session.add(entity)
        await self._session.flush()
        return entity

    async def update(self, entity: ModelT) -> ModelT:
        merged = await self._session.merge(entity)
        await self._session.flush()
        return merged
