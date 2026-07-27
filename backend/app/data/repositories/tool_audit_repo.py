"""ToolAuditRepo — repository for `tool_invocations`
(docs/04-backend-architecture.md §5), the one during-call synchronous
Postgres write (docs/10-tool-calling.md §2's pipeline: "one INSERT into
tool_invocations, synchronous, same transaction as the business write for
mutating tools").

`insert` takes every column so `ToolExecutor` (a later batch) can build
one call per invocation regardless of which pipeline stage produced the
row — denied at the registry, a validation retry, a gate hold, or a
terminal execution. It never opens its own session or commits; the
caller supplies the same `AsyncSession` its business write used (the
general constructor pattern every repo in this package follows, docs/04
§4), so a mutating tool's write and its audit row live or die in one
transaction (docs/04 §5: "a business mutation without an audit row is
exactly the state the tool layer is built to prevent").
"""

from __future__ import annotations

from typing import Any

from app.data.repositories.base import SqlAlchemyRepository
from app.models import ToolInvocation


class ToolAuditRepo(SqlAlchemyRepository[ToolInvocation]):
    model = ToolInvocation

    async def insert(
        self,
        *,
        session_id: str,
        tool_name: str,
        input: dict[str, Any],
        status: str,
        latency_ms: int,
        turn_no: int | None = None,
        output: dict[str, Any] | None = None,
        screen_ctx: dict[str, Any] | None = None,
        error_code: str | None = None,
        idempotency_key: str | None = None,
    ) -> ToolInvocation:
        """One row, every column `tool_invocations` (docs/12-data-
        models.md §4.4) defines beyond the server-generated
        `invocation_id`/`created_at`. `input`, not `args`, deliberately
        mirrors the column name (docs/12 §4.4) the way `app/models/orm.py`
        mirrors `text` on `ConversationTurn` — shadowing the builtin is
        the smaller cost than a name that no longer matches the schema
        at a glance.

        Deliberately no `get`/`update` beyond the inherited
        `Repository[T]` floor — this table is insert-only from the tool
        path; nothing in Phase 2 mutates an audit row after the fact.
        """
        invocation = ToolInvocation(
            session_id=session_id,
            turn_no=turn_no,
            tool_name=tool_name,
            input=input,
            output=output,
            screen_ctx=screen_ctx,
            status=status,
            error_code=error_code,
            latency_ms=latency_ms,
            idempotency_key=idempotency_key,
        )
        return await self.add(invocation)
