"""UserProfileRepo — repository for `user_profiles`
(docs/09-memory-architecture.md §5.1, the owning doc per canon §11).

docs/04-backend-architecture.md §5's repository table does not name a
`UserProfileRepo` — its list predates the Phase-5 memory tables. This repo
is the Phase-5 addition the post-call pipeline (docs/09 §8) needs: the
profile merge is the one stage that writes `user_profiles`, and the
call-setup prefetch (docs/02 §3.1) is the one stage that reads it.

**This class holds no policy.** Which facts may merge, what happens to
`open_issues`, and what validates the JSONB all live in
`app.memory.user_profile.UserProfileMemory` — the same split
`SessionMemory` has against `RedisClient`. This module knows one
statement and one row shape.

The ORM class is imported as `UserProfileRow` because
`app.domain.types.UserProfile` (the domain twin this repo's caller
returns) has the identical name; app/models/orm.py's own docstring
mandates the alias, since a plain double import silently shadows the
first. The import is from `app.models.orm` rather than `app.models`
because the package `__init__` re-exports the Phase-2 models only — the
Phase-5 memory models were deliberately not added to it.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.data.repositories.base import SqlAlchemyRepository
from app.models.orm import UserProfile as UserProfileRow


class UserProfileRepo(SqlAlchemyRepository[UserProfileRow]):
    """`get(user_id)` (inherited) is the call-setup prefetch read;
    `upsert` below is the post-call merge write. `add`/`update` (also
    inherited) are unused here — a profile row is never created by any
    path other than the merge, which cannot know in advance whether the
    row exists."""

    model = UserProfileRow

    async def get(  # noqa: A002 — `id` is the inherited base signature's name
        self, id: str, *, with_for_update: bool = False
    ) -> UserProfileRow | None:
        """The base `get`, plus an opt-in row lock.

        `UserProfileMemory.merge_post_call` is a read-modify-write over the
        whole row: it loads the profile, folds this call's extraction and
        issue changes onto it, and upserts the result. Without `SELECT ...
        FOR UPDATE` on the read, two concurrent merges for one merchant
        both read the pre-state and the second upsert overwrites the first
        entirely — not a lost field, a lost merge. `LimitRepo.submit`,
        `PaymentRepo.check_daily_limit` and `WalletRepo.debit_if_sufficient`
        all take the same lock against the same shape of race, and this
        follows that precedent rather than inventing an optimistic version
        column for one table.

        Opt-in, defaulting `False`, for the same reason it is opt-in on
        those siblings: the call-setup prefetch (docs/02 §3.1) only reads,
        and has no business holding a row lock for the length of a request.

        The bound worth stating, because a lock is easy to over-read: this
        serializes merges against an **existing** row. A row that does not
        exist cannot be locked, so two concurrent first-ever merges for one
        merchant can both see `None` and the second `upsert` still wins.
        `ON CONFLICT DO UPDATE` keeps that from erroring, not from losing
        the first merge's facts.
        """
        return await self._session.get(self.model, id, with_for_update=with_for_update)

    async def upsert(
        self,
        user_id: str,
        *,
        facts: dict[str, Any],
        preferences: dict[str, Any],
        open_issues: list[Any],
        updated_at: datetime,
        updated_by_call: str,
    ) -> None:
        """`INSERT ... ON CONFLICT (user_id) DO UPDATE` — the whole row,
        every time.

        Upsert rather than insert-or-update-by-branch because a merchant's
        first call has no profile row and every later call has one, and
        the merge stage cannot tell which it is without a second round
        trip. It is also what makes docs/09 §8's "the profile merge
        re-applied is a no-op" hold at the SQL layer: a retried pipeline
        writes the same computed row again instead of raising a PK-conflict
        `IntegrityError`.

        **`updated_at` is passed in, not left to the database.** The
        column's `server_default now()` fires on INSERT only, and
        app/models/orm.py declares no `onupdate`, so a DO UPDATE that
        omitted this column would leave `updated_at` frozen at the row's
        creation while `updated_by_call` moved to a newer session — two
        provenance fields disagreeing about the same write. Supplying it
        explicitly keeps them in step and lets the caller return the
        written value without re-reading the row.

        Honest note on that clock: this is the application's clock
        (`datetime.now(UTC)` in the calling layer), not Postgres's, which
        follows the existing repo precedent (`ConversationRepo` stamps
        `ended_at` and `started_at` the same way). `updated_at` therefore
        orders writes only as well as the writing hosts' clocks agree; it
        is provenance, not a serialization order, and nothing in this
        codebase compares it across hosts.

        The three JSONB arguments are plain JSON-able containers, already
        validated and serialized by the caller. This method does not — and
        at this layer cannot — check their shape: the columns are plain
        JSONB and Postgres accepts any JSON (app/models/orm.py's
        `UserProfile` docstring, and the test that pins it in
        tests/models/test_memory_orm.py). docs/09 §5.2's closed schema is
        a property of the write path in `UserProfileMemory`, not of this
        statement or of the table.
        """
        stmt = pg_insert(UserProfileRow).values(
            user_id=user_id,
            facts=facts,
            preferences=preferences,
            open_issues=open_issues,
            updated_at=updated_at,
            updated_by_call=updated_by_call,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=[UserProfileRow.user_id],
            set_={
                "facts": stmt.excluded.facts,
                "preferences": stmt.excluded.preferences,
                "open_issues": stmt.excluded.open_issues,
                "updated_at": stmt.excluded.updated_at,
                "updated_by_call": stmt.excluded.updated_by_call,
            },
        )
        await self._session.execute(stmt)
        await self._session.flush()
