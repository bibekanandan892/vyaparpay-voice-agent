"""`EventLog` (docs/08-context-and-events.md §4.2) — the append-only
`ctx.event` timeline store. `ctx.event` payloads (`app_event/v1`, docs/08
§2.1) `RPUSH` onto `ctx:{session_id}:events`, then `LTRIM` to the newest
200 entries.

Deliberately no validation here — see docs/08 §4's own opening line,
"Three components, one direction of flow: validate → store → shape." The
"validate" stage is entirely `SnapshotIngestor`'s (docs/08 §4.1's four
checks are explicitly scoped to "every inbound snapshot or delta", not
events); `EventLog`'s own section (§4.2) never mentions a check. Giving this
class its own schema-validation pass would blur that already-decided
pipeline-stage split, and would also require it to know how to react to a
malformed event (drop? count-metric? gap-recovery round trip? — none of
which docs/08 §4.2 assigns it). A caller (T3's data-channel dispatcher) that
wants validated events can run them through
`app.context.schema_validation.validate(event, schema_validation.load_schema
("app_event.v1"))` itself before calling `append`.
"""

from __future__ import annotations

from typing import Any

import orjson

from app.context.redis_keys import CTX_TTL_SECONDS, _ctx_events_key
from app.data.redis_client import RedisClient

# docs/08 §4.2: "RPUSH + LTRIM to the newest 200 entries... 200 covers a
# pathological churn burst without becoming a session archive."
EVENTS_MAX_LEN = 200


class EventLog:
    """Wraps `RedisClient` the same way `app/memory/session_memory.py`
    does — DI'd in, reached via `.raw` for the list commands
    `RedisClient`'s own typed surface doesn't cover (see
    `app/context/redis_keys.py`'s module docstring for why the `ctx:*`
    namespace lives here rather than as new `RedisClient` methods)."""

    def __init__(self, redis: RedisClient) -> None:
        self._redis = redis

    async def append(self, session_id: str, event: dict[str, Any]) -> None:
        """`RPUSH` one `app_event/v1` payload, then `LTRIM` to the newest
        `EVENTS_MAX_LEN` entries (drop-oldest, matching
        `RedisClient.append_turn`'s identical cap-and-trim shape for
        `session:{id}:turns`) and refresh the key's TTL."""
        key = _ctx_events_key(session_id)
        await self._redis.raw.rpush(key, orjson.dumps(event).decode())
        await self._redis.raw.ltrim(key, -EVENTS_MAX_LEN, -1)
        await self._redis.raw.expire(key, CTX_TTL_SECONDS)

    async def append_many(self, session_id: str, events: list[dict[str, Any]]) -> None:
        """Review fix (MEDIUM, audit-MEDIUMs review of `_persist_recent_events`):
        that caller loops [append] once per `POST /v1/sessions` `recent_events`
        entry — up to `session_create_request.v1.json`'s `_MAX_RECENT_EVENTS`
        (50) — and [append] is itself three sequential Redis round trips, so a
        full-size batch was up to 150 sequential awaits blocking the `201`
        response on exactly the "merchant is trying to start a support call"
        path docs/13 §2.1 says the ergonomics matter for.

        A single `redis.pipeline(transaction=True)` batches all of it into one
        round trip — the same tool `enforce_rate` already uses
        (`app/data/redis_client.py`) for the identical reason. One `RPUSH` with
        every event as a variadic argument (not N single-value `RPUSH`s inside
        the pipeline) keeps this a single command in the pipeline rather than
        `len(events)` of them; `LTRIM`/`EXPIRE` still run once, after, exactly
        as [append] does for one event.

        No-ops on an empty list rather than pipelining zero commands — an
        empty `RPUSH` is invalid, and "nothing to persist" needs no round
        trip at all, matching the caller's own no-key-written behavior for
        `recent_events: []` before this method existed.

        [append] itself is untouched and still the right call for its one
        other caller (`ContextDispatcher`, `app/voice/context_dispatch.py`):
        an in-call `ctx.event` arrives one message at a time over the data
        channel, so there is never a batch to pipeline there — the round-trip
        cost this fix removes is a REST-body-shaped problem, not a per-event
        one."""
        if not events:
            return
        key = _ctx_events_key(session_id)
        payloads = [orjson.dumps(event).decode() for event in events]
        async with self._redis.raw.pipeline(transaction=True) as pipe:
            pipe.rpush(key, *payloads)
            pipe.ltrim(key, -EVENTS_MAX_LEN, -1)
            pipe.expire(key, CTX_TTL_SECONDS)
            await pipe.execute()

    async def get_events(
        self, session_id: str, *, limit: int | None = None
    ) -> list[dict[str, Any]]:
        """All stored events, oldest first (RPUSH order), or just the
        newest `limit` when given — the shape `ContextCompressor`'s
        `render_timeline_slot` expects (docs/08 §5's "newest 15" slot)."""
        if limit is not None and limit <= 0:
            # -0 == 0 in Python, which would otherwise silently fall
            # through to `lrange(key, 0, -1)` (the whole list) instead of
            # the empty result a `limit=0` caller actually asked for.
            return []
        key = _ctx_events_key(session_id)
        start = -limit if limit is not None else 0
        raw = await self._redis.raw.lrange(key, start, -1)
        return [orjson.loads(entry) for entry in raw]


__all__ = ["EVENTS_MAX_LEN", "EventLog"]
