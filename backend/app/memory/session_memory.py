"""`SessionMemory` — Layer 2 of the seven-layer memory model
(docs/09-memory-architecture.md §3): the business shape of the
`session:{id}` Redis hash, built on top of `RedisClient`
(`app/data/redis_client.py`), which only knows how to get/set the raw
fields. This module owns the policy `RedisClient`'s own docstring points
here for: capping the transcript window at 8 turns, deserializing it back
into domain `Message` objects, and running-cost accumulation.

Two judgment calls made in this file, flagged explicitly per the Batch
3.4 task brief rather than silently guessed at:

1. **Tool-result digests are formatting-only; there is no persistence
   path.** docs/09 §3 shows `session:{id}.tool_results` as
   `[{"tool", "digest", "idem", "ts"}, ...]`, but `RedisClient` (frozen,
   merged in Batch 2.1) exposes get/set only for `transcript`,
   `pending_confirm`, and `cost_usd` — there is no
   `get_tool_results`/`set_tool_results` pair. Reaching around the
   wrapper via `RedisClient.raw` to `hset` the field by hand was
   rejected: it would mean re-deriving the `session:{id}` key string
   locally, exactly the typo/namespace-collision hazard `RedisClient`'s
   own docstring says it exists to prevent by owning every prefix in one
   place. `format_tool_digest` below is therefore a pure formatter with
   no Redis write — closing the gap needs either a small `RedisClient`
   follow-up or an explicit decision to give ToolExecutor direct access.
   Separately, the `digest` *string* it produces is narrower than docs/09
   §3's hand-written example (`"balance ₹18,450"`): rendering a
   human-readable amount out of an arbitrary `ToolResult.data` needs
   per-tool knowledge (which field is "the" amount, how to format
   currency) this module doesn't have. That belongs with ToolExecutor,
   which knows each tool's output shape (docs/10-tool-calling.md §3); the
   fallback here is a truncated JSON rendering of the payload.
2. **Confirm-gate supersession policy is not implemented here.** docs/10
   §4's "a second proposal while one is pending cancels the first as
   superseded" requires writing an audit row
   (`tool_invocations.status = cancelled`, docs/12-data-models.md §4.4)
   that only `ToolExecutor` can make — this module has no Postgres
   access and no audit repo. `set_pending_confirm` below is a pure Redis
   read/replace; deciding *when* a new proposal supersedes an old one is
   the caller's job, one layer up.
3. **Transcript entries persist the `Message` wire shape, not docs/09
   §3's illustrated `{"role","text","ts","tok"}` example.**
   `append_message` stores `message.model_dump(mode="json")` — the
   frozen `app.domain.types.Message` fields (`role`, `content`,
   `tool_calls`, `tool_call_id`, `name`), not the doc's hand-drawn
   shape. Concretely: the doc's `"text"` is this type's `"content"`;
   there is no per-message `"ts"` or `"tok"` (token count) field at all.
   Reusing the already-frozen `Message` type (rather than inventing a
   second transcript-entry schema) is the deliberate choice — per-message
   token accounting, if it turns out to be needed, belongs at render
   time (`ContextBuilder`/`ContextCompressor`, docs/08), which has the
   tokenizer this module doesn't.
"""

from __future__ import annotations

import json
import time
from decimal import Decimal
from typing import Any

from app.data.redis_client import RedisClient
from app.domain.types import Message, PendingConfirm, ToolResult

# docs/09 §3: "`transcript` is capped at the last 8 turns" — the same
# 6-8 the conversation-window prompt slot renders at 600 tokens (docs/11
# §1). Eviction is drop-oldest/FIFO: turns older than the cap are not
# archived here, they exist only inside the rolling summary (docs/09
# §4), which is Phase 5 scope (docs/17-roadmap.md §1.3) and out of reach
# of this module.
_MAX_TRANSCRIPT_TURNS = 8

# docs/10 §2's format stage caps each *rendered* tool result at ~120
# tokens before it re-enters the prompt; reused here as a conservative
# character budget for the raw digest string this module produces (no
# tokenizer available at this layer, so this is a char-count proxy, not
# a token count).
_MAX_DIGEST_CHARS = 480
_TRUNCATION_SUFFIX = "…(truncated)"


class SessionMemory:
    """Wraps `RedisClient`, owning the *business* shape of `session:{id}`
    (docs/09 §3): the transcript window cap and its round-trip to
    `Message` domain objects, running-cost accumulation, and (narrowly,
    see module docstring) tool-result digest formatting. `pending_confirm`
    is a thin pass-through — `RedisClient` already speaks `PendingConfirm`
    end to end, so there is no business shape left for this layer to add.
    """

    def __init__(self, redis: RedisClient) -> None:
        self._redis = redis

    # ------------------------------------------------------------------
    # Transcript window (docs/09 §3)
    # ------------------------------------------------------------------

    async def get_window(self, session_id: str) -> list[Message]:
        """The current transcript window, oldest turn first, deserialized
        into domain `Message` objects."""
        raw = await self._redis.get_transcript_window(session_id)
        return [Message.model_validate(entry) for entry in raw]

    async def append_message(self, session_id: str, message: Message) -> None:
        """Append one message to the transcript window, then trim to the
        last `_MAX_TRANSCRIPT_TURNS` entries, drop-oldest (docs/09 §3).

        Read-modify-write across the two `RedisClient` calls — not
        atomic, but fine for Phase 2's one-worker-per-call scope
        (docs/02-system-architecture.md: the worker that owns a call is
        the only writer to its `session:{id}`), the same reasoning
        `add_cost` below relies on.
        """
        window = await self._redis.get_transcript_window(session_id)
        window.append(message.model_dump(mode="json"))
        trimmed = window[-_MAX_TRANSCRIPT_TURNS:]
        await self._redis.set_transcript_window(session_id, trimmed)

    # ------------------------------------------------------------------
    # pending_confirm (docs/10 §4) — thin pass-through; `RedisClient`
    # already speaks `PendingConfirm` end to end (see module docstring
    # judgment call #2 on why supersession policy is NOT here)
    # ------------------------------------------------------------------

    async def get_pending_confirm(self, session_id: str) -> PendingConfirm | None:
        return await self._redis.get_pending_confirm(session_id)

    async def set_pending_confirm(self, session_id: str, pending: PendingConfirm | None) -> None:
        await self._redis.set_pending_confirm(session_id, pending)

    # ------------------------------------------------------------------
    # Running cost (docs/05-agent-architecture.md §3.8's
    # `session:{id}.cost_usd` counter)
    # ------------------------------------------------------------------

    async def get_cost(self, session_id: str) -> Decimal:
        return await self._redis.get_running_cost(session_id)

    async def add_cost(self, session_id: str, delta: Decimal) -> Decimal:
        """Read-modify-write the running per-call cost counter by
        `delta`, returning the new total. Unlocked RMW: fine for Phase
        2's single-worker-per-call scope — this is not yet a documented
        concurrency-sensitive path the way the payment/limit repos are
        (their optimistic-locking rationale doesn't transfer here, since
        nothing else ever writes this session's `cost_usd` concurrently).
        """
        current = await self._redis.get_running_cost(session_id)
        new_total = current + delta
        await self._redis.set_running_cost(session_id, new_total)
        return new_total

    # ------------------------------------------------------------------
    # Tool-result digests (docs/09 §3, §7 strategy 4) — formatting only;
    # see module docstring judgment call #1 for why this doesn't persist
    # ------------------------------------------------------------------

    @staticmethod
    def format_tool_digest(result: ToolResult) -> dict[str, Any]:
        """Build the `{"tool", "digest", "idem", "ts"}` record shape
        docs/09 §3 shows for `session:{id}.tool_results` entries.

        `digest` here is a truncated JSON rendering of the result
        payload (`data` on success, `error` or `gate` otherwise) — not
        the hand-written human summary in the doc's illustrative example
        (`"balance ₹18,450"`), which needs per-tool domain knowledge this
        module doesn't have (see module docstring judgment call #1).
        """
        payload = result.data if result.data is not None else (result.error or result.gate or {})
        digest = json.dumps(payload, separators=(",", ":"), default=str)
        if len(digest) > _MAX_DIGEST_CHARS:
            digest = f"{digest[:_MAX_DIGEST_CHARS]}{_TRUNCATION_SUFFIX}"
        return {
            "tool": result.tool_name,
            "digest": digest,
            "idem": result.idempotency_key,
            "ts": int(time.time() * 1000),
        }


__all__ = ["SessionMemory"]
