"""Redis access module — the process's one async client pool, plus typed
helpers keyed by the canon keyspace (docs/12-data-models.md §7, docs/04-
backend-architecture.md §5's "Redis access module" paragraph).

Callers never assemble `session:{id}`, `session:{id}:turns`,
`idempotency:{key}`, `signal_token:{id}`, `session_control:{id}`, or
`rate:{user_id}` strings by hand — `RedisClient` (and the standalone
`enforce_rate` below) own every prefix internally, so a typo can't
silently read/write the wrong namespace and every key stays greppable
back to one definition (docs/04 §5's stated design).

Two things this module deliberately is NOT:

- It is not `SessionMemory` (`app/memory/session_memory.py`, Batch 3.4).
  That module owns the *business* shape of the session hash — capping the
  transcript window at 8 turns, formatting tool-result digests, deciding
  when the running cost actually gets bumped. This module only knows how
  to get/set the Redis-level fields; policy for what goes into them lives
  one layer up.
- It is not the rate-limit call site. `enforce_rate()` here is the
  sliding-window primitive docs/04 §6 pins verbatim; wiring it into a
  FastAPI route dependency is `app/api/deps.py`'s job (Batch 3.3).

Docs/04 §6's pseudocode names the limit-exceeded exception `RateLimited`.
The frozen Batch-1 error hierarchy (`app/api/errors.py`) defines the same
concept as `RateLimitedError(retry_after: int)` — it is reused verbatim
below rather than redefined, per the plan's single-error-vocabulary rule.
"""

from __future__ import annotations

import time
from decimal import Decimal
from typing import Any
from uuid import uuid4

import orjson
from redis.asyncio import Redis

from app.api.errors import RateLimitedError
from app.config import Settings
from app.domain.types import PendingConfirm, RollingSummary

# docs/12 §7: idempotency:{key} TTL is 24h — the demo's same-day replay
# window, independent of the session TTL (SESSION_TTL_SECONDS) it happens
# to currently equal.
_IDEMPOTENCY_TTL_SECONDS = 24 * 60 * 60

# Defensive cap on session:{id}:turns (review finding): this list has no
# product reason to ever hold more than a few dozen entries (even a long
# canonical call is ~15 turns), but nothing upstream of this module
# currently enforces a max turn count — ToolExecutor/ConversationManager
# (later batches) own that policy. LTRIM-ing here bounds worst-case memory
# from a stuck retry loop or an abusively long-lived session without
# affecting any real call.
_MAX_TURNS_PER_SESSION = 500


def _reject_key_delimiter(component: str, *, field: str) -> str:
    """Every key this module builds is colon-delimited; a component that
    itself contains `:` could collide across sessions/tools/turns (e.g. a
    crafted `tool_name` splicing in an extra segment). `tool_name` in
    particular traces back to unconstrained LLM tool-call output — nothing
    upstream (ToolExecutor doesn't exist yet) currently validates it
    against the tool registry, so this module — which claims to own every
    prefix so "a typo can't silently read/write the wrong namespace" —
    enforces it here rather than trusting that promise to hold once a
    caller exists.
    """
    if ":" in component:
        raise ValueError(f"{field} must not contain ':' (got {component!r})")
    return component


def _session_key(session_id: str) -> str:
    return f"session:{_reject_key_delimiter(session_id, field='session_id')}"


def _session_turns_key(session_id: str) -> str:
    return f"session:{_reject_key_delimiter(session_id, field='session_id')}:turns"


def _signal_token_key(session_id: str) -> str:
    # docs/13 §6.2 / §5: the hashed one-time signaling token agent-api
    # writes at mint and the voice-worker burns at WS accept. Its TTL is
    # the connect deadline itself, so this key intentionally has no
    # companion "expires_at" field to drift from.
    return f"signal_token:{_reject_key_delimiter(session_id, field='session_id')}"


def _session_control_channel(session_id: str) -> str:
    # Pub/sub CHANNEL, not a key — the REST hang-up (DELETE /v1/sessions/{id})
    # publishes here and the voice-worker that owns the peer connection
    # reacts. Same colon-delimited namespace discipline as the keys above.
    return f"session_control:{_reject_key_delimiter(session_id, field='session_id')}"


def _idempotency_key(session_id: str, tool_name: str, turn_no: int) -> str:
    # Literal format per the plan: f"{session_id}:{tool_name}:{turn_no}",
    # namespaced under "idempotency:" (docs/12 §7, docs/10 §4.2).
    session_id = _reject_key_delimiter(session_id, field="session_id")
    tool_name = _reject_key_delimiter(tool_name, field="tool_name")
    return f"idempotency:{session_id}:{tool_name}:{turn_no}"


def _as_str(value: bytes | str | None) -> str | None:
    """Narrow a `decode_responses=True` redis-py return value to `str`.

    `Redis.hget`/`.get()`'s stubs return `bytes | str | None` because
    redis-py doesn't parametrize the return type on the `decode_responses`
    constructor flag; `RedisClient.from_settings` always constructs its
    client with `decode_responses=True`, so every value really is
    `str | None` at runtime. Decoding an actual `bytes` value here (rather
    than asserting it away) keeps this correct even against a test double
    that doesn't honor the flag.
    """
    return value.decode() if isinstance(value, bytes) else value


class RedisClient:
    """Typed wrapper around one `redis.asyncio.Redis` connection pool.

    Construct via `RedisClient.from_settings(settings)` in production
    (`app/main.py`'s lifespan, Batch 3.3, stores the result on
    `app.state.redis`). `__init__` taking a raw client directly is the
    testability seam — tests inject a fake here instead of a real
    connection (see `tests/data/test_redis_client.py`).
    """

    def __init__(self, redis: Redis, *, session_ttl_seconds: int) -> None:
        self._redis = redis
        self._session_ttl_seconds = session_ttl_seconds

    @classmethod
    def from_settings(cls, settings: Settings) -> RedisClient:
        # .get_secret_value() only here, at the point of use — same rule
        # as database_url/openrouter_api_key (app/config.py), never cached
        # unwrapped on self.
        redis = Redis.from_url(settings.redis_url.get_secret_value(), decode_responses=True)
        return cls(redis, session_ttl_seconds=settings.session_ttl_seconds)

    @property
    def raw(self) -> Redis:
        """Escape hatch for callers that need the underlying client
        directly — namely `enforce_rate()` below, whose pinned signature
        (docs/04 §6) takes a raw `Redis`, not this wrapper. Prefer the
        typed methods on this class for everything else."""
        return self._redis

    async def close(self) -> None:
        await self._redis.aclose()

    # ----------------------------------------------------------------
    # session:{id} hash (docs/09 §3) — the transcript window,
    # pending_confirm, and running cost fields (Batch 2.1), plus the
    # rolling-summary pair added by Phase-5 Batch 2b. SessionMemory owns
    # the business shape on top of these; the remaining documented fields
    # (tool_results digests, turn_count, state, ...) still have no
    # accessor here — see SessionMemory's module docstring.
    # ----------------------------------------------------------------

    async def get_transcript_window(self, session_id: str) -> list[dict[str, Any]]:
        raw = _as_str(await self._redis.hget(_session_key(session_id), "transcript"))
        return orjson.loads(raw) if raw else []

    async def set_transcript_window(
        self, session_id: str, turns: list[dict[str, Any]]
    ) -> None:
        key = _session_key(session_id)
        await self._redis.hset(key, "transcript", orjson.dumps(turns).decode())
        await self._redis.expire(key, self._session_ttl_seconds)

    async def get_pending_confirm(self, session_id: str) -> PendingConfirm | None:
        raw = _as_str(await self._redis.hget(_session_key(session_id), "pending_confirm"))
        return PendingConfirm.model_validate_json(raw) if raw else None

    async def set_pending_confirm(
        self, session_id: str, pending: PendingConfirm | None
    ) -> None:
        """`pending=None` clears the field (a superseding proposal or a
        resolved gate, docs/10 §4) rather than writing an empty string —
        `get_pending_confirm` must see "absent", not "present but blank".
        """
        key = _session_key(session_id)
        if pending is None:
            await self._redis.hdel(key, "pending_confirm")
            return
        await self._redis.hset(key, "pending_confirm", pending.model_dump_json())
        await self._redis.expire(key, self._session_ttl_seconds)

    async def get_summary(self, session_id: str) -> RollingSummary | None:
        """The rolling summary and its coverage boundary (docs/09 §3's
        `summary` / `summary_thru_turn` fields), or `None` when no fold has
        landed yet — the state every call is in until turn 9 (docs/09
        §4.1 rule 1).

        A half-written pair (one field present, the other missing) is a
        `None` too, not a partial summary: `set_summary` writes the two
        fields as two `HSET`s, so a crash between them is possible, and a
        summary whose boundary is unknown cannot be rendered without
        risking an overlap or a gap against the verbatim window. Losing
        one fold's compression is the cheap failure; double-narrating
        three turns to a merchant is not.
        """
        key = _session_key(session_id)
        text = _as_str(await self._redis.hget(key, "summary"))
        thru = _as_str(await self._redis.hget(key, "summary_thru_turn"))
        if not text or not thru:
            return None
        return RollingSummary(text=text, thru_turn=int(thru))

    async def set_summary(self, session_id: str, summary: RollingSummary) -> None:
        """Write both halves of the pair. There is deliberately no
        `summary=None` clear arm (unlike `set_pending_confirm`): nothing
        in docs/09 §4 un-summarizes a call.

        **This method enforces no ordering.** It `HSET`s whatever
        `thru_turn` it is handed, with no compare against the stored
        value, so it cannot stop an older boundary being written after a
        newer one. That monotonicity is caller-side discipline in
        `Summarizer` — its already-covered check and its single-flight
        guard — and that discipline only covers folds routed through
        `Summarizer.kick()`; concurrent direct `fold_pending()` calls
        would bypass it. Enforcing it here would need a Lua CAS on the
        two fields, which no caller has asked for.
        """
        key = _session_key(session_id)
        await self._redis.hset(key, "summary", summary.text)
        await self._redis.hset(key, "summary_thru_turn", str(summary.thru_turn))
        await self._redis.expire(key, self._session_ttl_seconds)

    async def get_running_cost(self, session_id: str) -> Decimal:
        raw = _as_str(await self._redis.hget(_session_key(session_id), "cost_usd"))
        return Decimal(raw) if raw else Decimal("0")

    async def set_running_cost(self, session_id: str, cost_usd: Decimal) -> None:
        key = _session_key(session_id)
        await self._redis.hset(key, "cost_usd", str(cost_usd))
        await self._redis.expire(key, self._session_ttl_seconds)

    # ----------------------------------------------------------------
    # session:{id}:turns list (docs/12 §7) — per-turn metric records
    # (turn_no, role, latency_ms, tokens, cost), distinct from the
    # transcript window above; CostTracker (Batch 4.3) appends, the
    # post-call pipeline drains this into `conversation_turns`.
    # ----------------------------------------------------------------

    async def append_turn(self, session_id: str, turn: dict[str, Any]) -> None:
        key = _session_turns_key(session_id)
        await self._redis.rpush(key, orjson.dumps(turn).decode())
        # Defensive cap, not a product limit — see _MAX_TURNS_PER_SESSION.
        # LTRIM keeps the *last* N entries (0-indexed from the end), so a
        # capped session loses its oldest turns first, never the most
        # recent ones the post-call drain would look at first.
        await self._redis.ltrim(key, -_MAX_TURNS_PER_SESSION, -1)
        await self._redis.expire(key, self._session_ttl_seconds)

    async def get_turns(self, session_id: str) -> list[dict[str, Any]]:
        raw = await self._redis.lrange(_session_turns_key(session_id), 0, -1)
        return [orjson.loads(r) for r in raw]

    # ----------------------------------------------------------------
    # idempotency:{key} string (docs/12 §7, docs/10 §4.2)
    # ----------------------------------------------------------------

    async def get_idempotency(
        self, session_id: str, tool_name: str, turn_no: int
    ) -> str | None:
        raw = await self._redis.get(_idempotency_key(session_id, tool_name, turn_no))
        return _as_str(raw)

    async def set_idempotency(
        self, session_id: str, tool_name: str, turn_no: int, value: str
    ) -> None:
        await self._redis.set(
            _idempotency_key(session_id, tool_name, turn_no),
            value,
            ex=_IDEMPOTENCY_TTL_SECONDS,
        )

    # ----------------------------------------------------------------
    # signal_token:{id} string (docs/13 §6.2) — the SHA-256 digest of the
    # one-time signaling token. Policy (format, entropy, verify-and-burn
    # ordering) lives in app/auth/signaling.py; this class only owns the
    # key name and the two Redis commands, the same split the module
    # docstring draws against SessionMemory.
    # ----------------------------------------------------------------

    async def set_signaling_token_hash(
        self, session_id: str, token_hash: str, *, ttl_s: int
    ) -> None:
        """`SET signal_token:{id} <digest> EX ttl_s`. The TTL is the
        connect deadline (docs/13 §6.2: "enforced by the Redis key's own
        expiry... no code path can forget to check it"), so a
        non-positive TTL would mint a token that is either immortal
        (Redis rejects `EX 0`) or already dead — refused here rather
        than surfacing as a driver error at an unrelated call site.
        """
        if ttl_s <= 0:
            raise ValueError(f"ttl_s must be positive, got {ttl_s}")
        await self._redis.set(_signal_token_key(session_id), token_hash, ex=ttl_s)

    async def take_signaling_token_hash(self, session_id: str) -> str | None:
        """`GETDEL` — read and delete in ONE atomic command, returning the
        stored digest or `None` if the key was absent (never minted,
        already burned, or expired).

        Atomicity is the whole point: a `GET` followed by a `DEL` lets two
        concurrent WebSocket accepts both read the same digest and both
        succeed, which defeats the one-time property docs/13 §6.2 requires.
        The verification itself is `app/auth/signaling.verify_and_burn`'s
        job — this method deliberately returns the digest rather than a
        boolean so the comparison stays in one place, next to the hashing.
        """
        return _as_str(await self._redis.getdel(_signal_token_key(session_id)))

    # ----------------------------------------------------------------
    # session_control:{id} pub/sub (docs/13 §2.2's explicit hang-up)
    # ----------------------------------------------------------------

    async def publish_session_control(self, session_id: str, message: str) -> int:
        """Publish one control message (e.g. `"end"`) on
        `session_control:{session_id}`; returns the number of subscribers
        that received it.

        Fire-and-forget by nature: Redis pub/sub has no persistence, so a
        return of 0 means "no voice-worker is currently listening for this
        session", not "the call failed to end". `DELETE /v1/sessions/{id}`
        is idempotent precisely because this channel is lossy — the caller
        logs the subscriber count rather than treating 0 as an error.
        """
        return int(await self._redis.publish(_session_control_channel(session_id), message))


async def enforce_rate(redis: Redis, user_id: str, *, limit: int, window_s: int = 60) -> None:
    """Sliding-window rate limiter, reproduced verbatim from docs/04 §6 —
    a sorted-set of `{now}:{uuid}` members scored by their own timestamp,
    trimmed to the trailing `window_s` on every call. This is a true
    sliding window, not a fixed-window counter: a fixed window lets a
    caller fire 2x the limit across a window boundary, while evicting by
    score on every check holds the limit continuously (docs/04 §6).

    Raises `RateLimitedError` (the frozen hierarchy's name for docs/04
    §6's `RateLimited`) when this call pushes the in-window count over
    `limit`; the caller (a route dependency, Batch 3.3) is responsible for
    letting that propagate into the `429 RATE_LIMITED` envelope.
    """
    key, now = f"rate:{_reject_key_delimiter(user_id, field='user_id')}", time.time()
    async with redis.pipeline(transaction=True) as p:
        p.zremrangebyscore(key, 0, now - window_s)
        p.zadd(key, {f"{now}:{uuid4().hex}": now})
        p.zcard(key)
        p.expire(key, window_s)
        _, _, count, _ = await p.execute()
    if count > limit:
        raise RateLimitedError(retry_after=window_s)


__all__ = ["RedisClient", "enforce_rate"]
