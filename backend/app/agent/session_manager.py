"""`SessionManager` — session lifecycle owner (docs/05-agent-architecture.md
§3.1), implementing the frozen `SessionManagerProto`
(app/domain/interfaces.py): `create` at session start, `attach`/`heartbeat`
during the call, `end` exactly once at hang-up, which runs the Phase-2
slice of the post-call drain (docs/09-memory-architecture.md §8): the
`session:{id}:turns` records out of Redis into `conversation_turns`
(docs/12-data-models.md §4.2).

Judgment calls made in this file, flagged explicitly per house style
rather than silently guessed at:

1. **Session-id format: 12 lowercase hex chars from `uuid4().hex`, not
   the canon's 6.** docs/12 §4.1's worked example (`'a1f3c9'`) shows 6
   hex chars, but 6 chars is 24 bits — ~50% birthday-collision odds by
   ~4.8k sessions on a PRIMARY KEY column. 12 chars (48 bits) keeps ids
   short and log/grep-friendly while making collisions negligible at any
   demo or pilot scale. Hex can never contain `:`, so ids compose safely
   with `RedisClient`'s colon-delimited keyspace (its
   `_reject_key_delimiter` guard).
2. **`signaling_token_hash` is a deterministic placeholder.** The column
   is NOT NULL (docs/12 §4.1, app/models/orm.py) but Phase 2 has no
   signaling and mints no token (that's Phase 3, docs/17). The constant
   below is the SHA-256 of a public sentinel string — a well-formed
   64-hex digest so the row is production-shaped, identical on every
   Phase-2 row and greppable back to this one definition, so it can
   never be mistaken for (or collide meaningfully with) a real one-time
   token's hash.
3. **`create` does not write the `session:{id}` Redis hash.** docs/09 §3
   says the hash is "created by agent-api at POST /v1/sessions", but the
   frozen `RedisClient` exposes no create-the-hash primitive — every
   field write (transcript, pending_confirm, cost) creates the hash
   lazily and stamps the 24h TTL. Reaching around the wrapper via
   `RedisClient.raw` to `hset` a `state` field was rejected for exactly
   the reason `SessionMemory`'s docstring already documents: it would
   re-derive the key string locally, the typo/namespace hazard
   `RedisClient` exists to prevent.
4. **The `Session` returned by `create` carries an app-side
   `started_at`.** The row's `started_at` is a server_default `now()`
   that SQLAlchemy does not load back on flush (no eager refresh), so
   this method returns `datetime.now(UTC)` captured at insert time. The
   two can differ by milliseconds; nothing in Phase 2 reads the value
   back on this path (attach/end re-read the row and get the DB truth).
5. **`EndReason` is recorded via structlog, not schema.** `conversations`
   has no reason/resolution column (docs/12 §4.1 — `resolution` lives in
   `conversation_summaries`, Phase-5 scope), so `end` emits the
   `call_ended` event from docs/05 §3.10's catalog with `reason` as a
   field instead of inventing a column.
6. **Drained turn-record schema is pinned here, keys = column names.**
   `session:{id}:turns` records are produced by CostTracker (Batch 4.3,
   docs/12 §4.2), which doesn't exist yet — this drain is the schema's
   first consumer, so it pins the contract: keys named exactly like
   `conversation_turns` columns (`turn_no` + `role` required; the rest
   optional), `cost_usd` as a string/number, `started_at` as ISO-8601
   (what orjson emits for a datetime). A record's `text` key, if one
   ever appears, is deliberately ignored — docs/12 §4.2's
   transcript-non-persistence rule. A malformed record (missing
   `turn_no`/`role`) raises and rolls back the whole drain rather than
   silently skipping rows — docs/09 §8's pipeline is "retried whole"
   from the still-live Redis hash inside the 24h TTL window, so aborting
   cleanly is the recovery path, not data loss.
7. **Idempotency of `end` is the `conversations.state` gate.** The
   duplicate-end check, the state flip, and the drain share one
   transaction: a second `end` sees `state='ended'` and no-ops (docs/05
   §3.1: "a double hang-up ... fires the post-call pipeline once, not
   twice"), while a crash mid-drain rolls the flip back so the retry
   re-runs the whole drain (docs/09 §8). Concurrent double-`end` from
   two processes isn't defended (no row lock) — Phase 2 is a
   single-process CLI harness; the real voice-worker split is Phase 3.
8. **Redis keys are never deleted here** — they expire on their own 24h
   TTL (docs/09 §8's "Redis keys left to expire"), which is also what
   keeps the retry-whole property in #6/#7 possible.
9. **`end` enforces the finalize-before-end ordering invariant (security
   review, HIGH) by checking for a `call_costs` row before draining.**
   `CostTracker.finalize()` (app/agent/cost_tracker.py) is documented as
   the sole writer of `session:{id}:turns` in Redis, and it unconditionally
   upserts a `call_costs` row (`CostRepo.upsert`) as its last step, on
   every call including zero-turn ones (verified: `demo_cli.py` calls
   `cost_tracker.finalize()` before `session_manager.end()` on every exit
   path, no exceptions). A `call_costs` row is therefore a reliable,
   always-present witness that `finalize()` already ran for this
   `session_id`. If a future caller (e.g. a Phase-3 `POST /v1/sessions`
   flow) called `end()` first, the old behavior was to silently drain an
   empty/stale turn list into Postgres — permanently losing per-turn
   cost/audit data with no error. `end()` now calls `CostRepo(db).get
   (session_id)` (the inherited `SqlAlchemyRepository.get` — `call_costs`
   is keyed by `session_id` alone, so no new CostRepo method is needed)
   before the drain, and raises a plain `RuntimeError` if no row exists:
   this isn't an HTTP-facing error (no route calls `end()` yet — only the
   CLI harness and tests do), so it deliberately isn't a typed `AppError`
   (app/api/errors.py) routed through `ErrorEnvelopeMiddleware`; a bug
   this loud should crash the caller, not degrade into a spoken apology
   the way `ConversationManager.on_stt_final`'s critical-path failures do
   (that class's own judgment call #5) — there is no live call to keep
   talking to at this point, only a caller that got the ordering wrong
   and needs to know immediately. The check runs inside the same
   transaction as the drain, after the duplicate-end no-op check (#7) so
   a second `end()` on an already-ended session still short-circuits
   before this new check ever runs.
"""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import uuid4

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.errors import ResourceNotFoundError
from app.data.redis_client import RedisClient
from app.data.repositories.conversation_repo import ConversationRepo
from app.data.repositories.cost_repo import CostRepo
from app.domain.types import EndReason, Session, SessionState
from app.models import Conversation
from app.obs.logging import get_logger

log = get_logger(__name__)

# Judgment call #1 (module docstring): 12 hex chars, not the canon
# example's collision-prone 6.
_SESSION_ID_HEX_CHARS = 12

def _mint_placeholder_token_hash() -> str:
    """Judgment call #2, hardened after security review (HIGH): the NOT
    NULL `signaling_token_hash` column gets a PER-ROW random digest with
    no known preimage — `sha256(uuid4().bytes)` where the input is
    discarded — instead of the earlier shared constant derived from a
    committed plaintext. With the constant, a future Phase-3 verifier
    doing the natural `sha256(candidate) == row.signaling_token_hash`
    would have authenticated the publicly-known plaintext against EVERY
    Phase-2 row; with a preimage-free per-row value, no candidate token
    can ever hash-match, so the naive comparison fails closed by
    construction. Phase 3's real token minting replaces this function
    outright."""
    return hashlib.sha256(uuid4().bytes).hexdigest()


def _mint_session_id() -> str:
    return uuid4().hex[:_SESSION_ID_HEX_CHARS]


def _to_domain(conversation: Conversation) -> Session:
    """ORM row -> frozen `Session` domain type (docs/12 §4.1 mirror)."""
    return Session(
        session_id=conversation.session_id,
        user_id=conversation.user_id,
        state=SessionState(conversation.state),
        started_at=conversation.started_at,
        ended_at=conversation.ended_at,
    )


def _parse_cost_usd(value: Any) -> Decimal | None:
    # Via str() so a JSON number that arrived as float doesn't smuggle
    # binary-float noise into the NUMERIC(10,6) column.
    if value is None:
        return None
    parsed = Decimal(str(value))
    # Security-review MEDIUM: Decimal happily parses "NaN"/"Infinity"
    # (and 1e400 arrives from orjson as float('inf')); Postgres >= 14
    # would INSERT them silently — no CHECK guards this column — and one
    # such row poisons every SUM()/AVG() over cost_usd. Keep it inside
    # the documented malformed-record-aborts-the-drain contract instead.
    if not parsed.is_finite():
        raise ValueError(f"cost_usd must be finite, got {value!r}")
    return parsed


def _parse_optional_int(value: Any, *, field: str) -> int | None:
    # Security-review note: turn_no was cast, its int siblings weren't —
    # same explicit coercion for all of them, so a malformed value hits
    # the abort path here instead of deep inside the driver.
    if value is None:
        return None
    if isinstance(value, bool):  # bool is an int subclass; reject it
        raise ValueError(f"{field} must be an integer, got {value!r}")
    return int(value)


def _parse_started_at(value: Any) -> datetime | None:
    # ISO-8601 per judgment call #6 — orjson's native datetime encoding.
    return None if value is None else datetime.fromisoformat(value)


class SessionManager:
    """docs/05 §3.1. Composes `ConversationRepo` (the durable
    `conversations`/`conversation_turns` stub, docs/12 §4.1-4.2) and
    `RedisClient` (the `session:{id}:turns` drain source, docs/09 §8).

    Dependencies are injected — the lifespan-built `async_sessionmaker`
    and `RedisClient` singletons (docs/04 §4's DI split); this class
    never opens its own engine/pool. Phase 2 has no FastAPI request
    scope on this path (the caller is the CLI harness), so each method
    owns its own one-session-one-transaction boundary, mirroring
    `app/api/deps.py::get_db`'s commit-on-success/rollback-on-raise
    shape (docs/04 §5) — the repos themselves never commit.
    """

    def __init__(
        self, sessionmaker: async_sessionmaker[AsyncSession], redis: RedisClient
    ) -> None:
        self._sessionmaker = sessionmaker
        self._redis = redis

    @asynccontextmanager
    async def _transaction(self) -> AsyncIterator[AsyncSession]:
        """One `AsyncSession` = one transaction (docs/04 §5), same shape
        as `get_db` but scoped to a SessionManager method instead of a
        request."""
        async with self._sessionmaker() as db:
            try:
                yield db
            except Exception:
                await db.rollback()
                raise
            else:
                await db.commit()

    @staticmethod
    async def _get_existing(db: AsyncSession, session_id: str) -> Conversation:
        conversation = await ConversationRepo(db).get(session_id)
        if conversation is None:
            raise ResourceNotFoundError(
                "No session found for this id", details={"session_id": session_id}
            )
        return conversation

    async def create(
        self,
        user_id: str,
        screen_context: dict[str, Any] | None,
        recent_events: list[dict[str, Any]],
    ) -> Session:
        """Mint a session id and insert the `conversations` anchor row
        (docs/05 §3.1, docs/12 §4.1). `screen_context`/`recent_events`
        are accepted and ignored — always `None`/`[]` in Phase 2 per
        `SessionManagerProto`'s docstring; the ScreenContext/AppEvent
        pipelines are Phase 3/4. `state` is owned by the column's
        server_default (`'created'`), never set here."""
        session_id = _mint_session_id()
        started_at = datetime.now(UTC)  # judgment call #4 (module docstring)
        async with self._transaction() as db:
            await ConversationRepo(db).create(
                session_id, user_id, _mint_placeholder_token_hash()
            )
        return Session(
            session_id=session_id,
            user_id=user_id,
            state=SessionState.CREATED,
            started_at=started_at,
            ended_at=None,
        )

    async def attach(self, session_id: str) -> Session:
        """Phase-2 no-op stub per `SessionManagerProto`: there is no
        voice-worker/peer-connection to attach (docs/05 §3.1's real
        attach is Phase 3), so this only loads and returns the session —
        deliberately no `created -> in_call` transition, which belongs
        to the real signaling attach. Raises `ResourceNotFoundError`
        for an unknown id."""
        async with self._transaction() as db:
            conversation = await self._get_existing(db, session_id)
            return _to_domain(conversation)

    async def heartbeat(self, session_id: str) -> None:
        """Liveness ping — no TTL touch (docs/05 §3.1: the session TTL
        is "never refreshed to infinity"; if the worker dies the session
        expires on its own). Phase 2 has no liveness accounting to
        update, so all this needs to do is existence-check the session
        so a caller pinging a bogus id fails loudly instead of
        heartbeating into the void."""
        async with self._transaction() as db:
            await self._get_existing(db, session_id)

    async def end(self, session_id: str, reason: EndReason) -> None:
        """Idempotent close (docs/05 §3.1) + the Phase-2 slice of the
        post-call drain (docs/09 §8): mark the conversation ended
        (`ConversationRepo.end`), then drain `session:{id}:turns` into
        `conversation_turns` rows in list order — gate, flip, and drain
        in one transaction (judgment calls #6-#8, module docstring).
        `text` is never passed through (docs/12 §4.2); Redis keys are
        left to expire on TTL.

        Before draining, requires a `call_costs` row for this
        `session_id` (judgment call #9, module docstring) — the
        always-present signal that `CostTracker.finalize()` already ran.
        Its absence means a caller invoked `end()` before `finalize()`,
        which would otherwise silently drain an empty/stale turn list;
        that ordering bug raises a `RuntimeError` here instead."""
        try:
            async with self._transaction() as db:
                repo = ConversationRepo(db)
                conversation = await self._get_existing(db, session_id)
                if conversation.state == SessionState.ENDED.value:
                    # info, not debug (review LOW): docs/05 §3.1 treats a
                    # double hang-up as an expected, operationally real
                    # condition — it should be visible at production levels.
                    log.info(
                        "session_end_duplicate", session_id=session_id, reason=reason.value
                    )
                    return

                # Judgment call #9: finalize-before-end invariant. A
                # missing call_costs row means CostTracker.finalize()
                # never ran for this session_id — draining now would
                # silently commit an empty/stale turn list with no other
                # signal that anything went wrong.
                if await CostRepo(db).get(session_id) is None:
                    raise RuntimeError(
                        "SessionManager.end() called before CostTracker.finalize() for "
                        f"session_id={session_id!r} — no call_costs row exists yet, so "
                        "draining session:{id}:turns now would silently lose all "
                        "per-turn cost/audit data for this call. Call "
                        "cost_tracker.finalize(session_id) before session_manager.end()."
                    )

                await repo.end(session_id)

                turn_records = await self._redis.get_turns(session_id)
                for record in turn_records:
                    await repo.append_turn(
                        session_id,
                        int(record["turn_no"]),
                        str(record["role"]),
                        latency_ms=_parse_optional_int(
                            record.get("latency_ms"), field="latency_ms"
                        ),
                        input_tokens=_parse_optional_int(
                            record.get("input_tokens"), field="input_tokens"
                        ),
                        output_tokens=_parse_optional_int(
                            record.get("output_tokens"), field="output_tokens"
                        ),
                        tool_calls=record.get("tool_calls"),
                        cost_usd=_parse_cost_usd(record.get("cost_usd")),
                        trace_id=record.get("trace_id"),
                        started_at=_parse_started_at(record.get("started_at")),
                    )

                duration_s: int | None = None
                if conversation.started_at is not None and conversation.ended_at is not None:
                    duration_s = int(
                        (conversation.ended_at - conversation.started_at).total_seconds()
                    )
        except Exception:
            # Code-review MEDIUM: the abort path deserves the same
            # visibility as the success path — docs/09 §8's "retried
            # whole" recovery only happens if something notices. The
            # rollback already happened in _transaction(); re-raise after
            # logging, never swallow.
            log.warning(
                "call_end_drain_failed",
                session_id=session_id,
                reason=reason.value,
                exc_info=True,
            )
            raise

        # docs/05 §3.10's `call_ended` event, emitted only after the
        # transaction committed — a rolled-back end never claims to have
        # ended anything. `resolution`/`call_cost_usd` from the catalog
        # are Summarizer/CostTracker outputs (Phase 5 / Batch 4.3) and
        # absent from the Phase-2 slice.
        log.info(
            "call_ended",
            session_id=session_id,
            reason=reason.value,
            turn_count=len(turn_records),
            duration_s=duration_s,
        )


__all__ = ["SessionManager"]
