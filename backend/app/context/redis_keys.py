"""`ctx:{session_id}` / `ctx:{session_id}:events` key helpers — the same
private-helper-plus-`_reject_key_delimiter` style `app/data/redis_client.py`
already uses for `session:{id}` and friends (docs/08-context-and-events.md
§4.1/§4.2's storage shapes; docs/12-data-models.md §7's canon keyspace).

Judgment call: these helpers live here, in `app/context/`, rather than as
new methods on `RedisClient` (`app/data/redis_client.py`). That module's own
docstring frames its key-prefix ownership as a closed, enumerated list
(`session:{id}`, `session:{id}:turns`, `idempotency:{key}`,
`signal_token:{id}`, `session_control:{id}`, `rate:{user_id}`) — `ctx:*` is
not one of them, and this task's file ownership is `backend/app/context/`
only (`app/data/redis_client.py` is not a file this task edits). Mirroring
the *style* — a private per-key helper plus the same colon-delimiter guard —
rather than extending that class keeps the two modules peers: `RedisClient`
owns the session/idempotency/signaling namespace end to end, `app/context/`
owns the `ctx:` namespace end to end, each the single place its own prefixes
can be typo'd from. Both `SnapshotIngestor` and `EventLog` reach the actual
Redis connection via `RedisClient.raw` (that property's own docstring: "the
escape hatch for callers that need the underlying client directly") rather
than duplicating connection setup.
"""

from __future__ import annotations

from typing import Final

# docs/08 §4.1: "Redis holds nothing durable; a call is minutes." Applied to
# both `ctx:{session_id}` (explicit in docs/08 §4.1) and
# `ctx:{session_id}:events` (not explicit in docs/08 §4.2, but the same
# "nothing durable" reasoning applies — a call's event timeline has no
# reason to outlive the call, and the sibling key already shares this TTL;
# `RedisClient.append_turn` applies its own session TTL the identical way,
# on every write, for `session:{id}:turns`).
CTX_TTL_SECONDS: Final = 60 * 60


def _reject_key_delimiter(component: str, *, field: str) -> str:
    """Verbatim copy of `app/data/redis_client.py`'s guard (see that
    module's own docstring for the rationale: an unvalidated `:` in a
    caller-supplied component could splice an extra key segment and
    collide across sessions). Duplicated rather than imported — importing
    a private, underscore-prefixed helper across module boundaries would
    couple this package to `redis_client.py`'s internals, and the guard
    itself is three lines with no state to drift out of sync."""
    if ":" in component:
        raise ValueError(f"{field} must not contain ':' (got {component!r})")
    return component


def _ctx_key(session_id: str) -> str:
    """`ctx:{session_id}` — the merged screen_context/v1 IR plus ingest
    metadata (docs/08 §4.1)."""
    return f"ctx:{_reject_key_delimiter(session_id, field='session_id')}"


def _ctx_events_key(session_id: str) -> str:
    """`ctx:{session_id}:events` — the LTRIM-to-200 event timeline list
    (docs/08 §4.2)."""
    return f"ctx:{_reject_key_delimiter(session_id, field='session_id')}:events"


__all__ = ["CTX_TTL_SECONDS", "_ctx_events_key", "_ctx_key", "_reject_key_delimiter"]
