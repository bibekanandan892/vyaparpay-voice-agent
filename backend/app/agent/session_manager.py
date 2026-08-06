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
2. **`signaling_token_hash` is supplied by whoever owns the token.**
   Phase 3's `POST /v1/sessions` (app/api/routes/sessions.py) mints the
   real one-time signaling token, passes `create()` only its SHA-256
   digest for the NOT NULL column (docs/12 §4.1), and separately writes
   the same digest to `signal_token:{id}` in Redis — the copy the
   voice-worker actually verifies and burns (docs/13 §5, §6.2). The
   parameter is keyword-only with a `None` default solely so the callers
   that predate the endpoint keep working against `SessionManagerProto`'s
   frozen three-argument signature (scripts/demo_cli.py, the E2E
   harness): omitting it mints a real token via `app/auth/signaling.py`
   and immediately discards the plaintext, so the stored digest has no
   reachable preimage and no candidate token can ever hash-match it —
   the same fail-closed property the deleted `_mint_placeholder_token_hash`
   provided, without a second minting function to keep in sync. This
   class still knows nothing about Redis keys or HTTP (judgment call #3's
   discipline): it consumes a digest, it never stores or serves one.
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
10. **`end()`'s post-call MEMORY pipeline (docs/09 §8: final summary +
    resolution/intents, its embed, the profile merge) is wired here too,
    after the primary transaction above — post-call-summary-profile-
    wiring task.** `Summarizer.fold_pending()` and
    `UserProfileMemory.merge_post_call()` were both fully built and
    tested in earlier Phase-5 batches with zero callers (grepped, `app/`
    tree-wide, confirmed) — this is the wiring. The new collaborators
    (`SummaryPipelineDeps`, below) are optional and keyword-only,
    defaulting to `None`: `app/api/routes/sessions.py`'s
    `get_session_manager()` builds a `SessionManager` only to call
    `.create()` (that route's own judgment call #1 explains why it can
    never legitimately call `.end()` — the finalize-before-end
    invariant needs the voice-worker's per-call `CostTracker`) — forcing
    that call site to thread through an `LLMRouter`/`Settings`/
    `ConversationSummaryStore` it will never exercise would be
    unjustified complexity for a dependency this pipeline never reaches
    from there. `UserProfileMemory` needs no such bundle: it wraps
    `UserProfileRepo(db)`, built inline inside its own short transaction
    exactly the way `ConversationRepo`/`CostRepo` already are above —
    only the *summary* stage (final fold, embed) has real external
    collaborators (an LLM router, an embeddings provider) to inject.
11. **Resolution/intents classification is deterministic, not a second
    LLM call — a real design decision, argued, not defaulted into.**
    docs/09 §8's mermaid diagram pairs "Summarizer (Haiku): final
    summary + resolution" as one labeled stage, which reads as
    resolution riding the same Haiku call that produces the summary
    text. Two things outweighed following that reading literally: (a)
    `Summarizer` is an already-shipped, already-tested, in-production
    component for the *in-call rolling* summary — changing its prompt
    schema to also emit structured resolution/intents for a *post-call-
    only* concern risks regressing a reviewed path for a use it was not
    designed to serve, and (b) checking first (per this task's own
    instruction) turned up grounded, tool-confirmed signals that need no
    inference at all: `EndReason.ESCALATED` is an exact, structural
    mirror of `Resolution.ESCALATED` (the caller already decided this);
    `SessionMemory`'s `pending_confirm` field (docs/10 §4's gate state)
    is a real, load-bearing signal for "abandoned" the task's own brief
    pointed at; and `ToolAuditRepo.list_for_session()`'s `status` column
    settles "did anything tool-confirmed complete" without guessing.
    `_classify_resolution`/`_classify_intents` (module-level, pure,
    tested without a model — the same shape `summarizer.py`'s own
    `should_fold`/`summary_thru_turn` take) are the result. `intents` is
    likewise derived from which tools were invoked (`_TOOL_INTENTS`)
    rather than freeform-classified, in the spirit of
    `app/memory/user_profile.py`'s own house rule against LLM inference
    where a tool-confirmed or structural fact already answers the
    question.
12. **A fresh per-call `Summarizer`'s buffer is empty today, and that is
    not a rare edge case — it is the only reachable one.** Grepped the
    whole `app/` tree: nothing constructs a `Summarizer` in production
    code, and `ConversationManager`'s turn loop never calls
    `observe_turn()`/`kick()` — the in-call rolling-fold wiring this
    class's `Summarizer.fold_pending()` seam assumes upstream is a
    separate, unbuilt task, out of this one's scope (this task's brief
    is explicit that the new logic belongs inside `end()`, not
    `ConversationManager`). Consequently `fold_pending(thru_turn=N)` on
    the fresh `Summarizer` `SummaryPipelineDeps.summarizer_factory`
    produces will return `None` for every call until that separate
    wiring lands — `_save_summary` falls back to whatever
    `SessionMemory.get_summary()` already holds (the last in-call fold,
    if one ever ran) and, failing that too, skips the summary/embed
    stage with a distinctly greppable log rather than fabricating text.
    Stated plainly because it bears on the milestone this task serves
    (docs/17 §2.5's "returning-caller proof"): until the rolling-fold
    wiring lands separately, no call in this codebase produces a real
    summary through this path, so nothing is ever embedded, and the
    milestone cannot be demonstrated end-to-end from this task alone.
13. **Profile `extraction` is deferred, deliberately, not silently
    dropped.** `UserProfileMemory.merge_post_call(extraction=...)` needs
    a Haiku-based extraction step against a closed JSON schema — real,
    separate new work (a new prompt + provider call, mirroring
    `UserProfileMemory`'s own description of how extraction works) this
    task does not build. `extraction=None` is passed explicitly, which
    that method's own docstring names as the legitimate "nothing new
    stated this call" value, not a placeholder for missing work.
    `opened_issues` — tool-confirmed, in scope — is derived from
    `ToolAuditRepo.list_for_session()`'s `request_limit_increase`
    successes (`_opened_issues_from_invocations`); `resolved_issue_ids`
    is never populated because no tool in today's registry closes an
    issue (there is no cancel/approve-confirmation tool), so there is
    nothing tool-confirmed to point at.
14. **The whole post-call memory stage runs AFTER the primary
    transaction above has committed, and its failures are caught,
    logged, and never re-raised.** Two reasons, not one: first, docs/09
    §8's "each stage is independently skippable" — a summary-save
    failure must not prevent the profile merge, and vice versa, so each
    is caught separately, not as one all-or-nothing block. Second, and
    more load-bearing: by the time this stage runs, judgment call #7's
    duplicate-end gate has ALREADY flipped `conversations.state` to
    `'ended'` in the committed primary transaction — so if this stage's
    exception were instead allowed to propagate out of `end()`,
    `app/voice/run.py`'s `_end_with_retry` would retry `end()` itself,
    and that retry would immediately hit the duplicate-end no-op and
    return without ever re-attempting the failed memory stage. Letting
    the exception propagate would therefore not buy a real retry — it
    would only turn an ordinary, already-logged memory-pipeline hiccup
    into a spurious `_end_with_retry` backoff-and-retry cycle and,
    on exhaustion, a misleading `session_end_failed_after_finalize`
    ERROR log that names a session stuck as `in_call` when it is not (it
    already ended cleanly; only its memory pipeline degraded). Building
    a real retry/reconciliation path for a mid-pipeline crash is the
    same out-of-scope call `app/voice/run.py`'s `_end_with_retry` module
    comment already made for the analogous `call_costs`-orphan case —
    "it needs its own composition-root decisions... this wiring task
    does not own" — applied here to the memory pipeline instead.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Final
from uuid import uuid4

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.errors import ResourceNotFoundError
from app.auth.signaling import mint_signaling_token
from app.data.redis_client import RedisClient
from app.data.repositories.conversation_repo import ConversationRepo
from app.data.repositories.cost_repo import CostRepo
from app.data.repositories.tool_audit_repo import ToolAuditRepo
from app.data.repositories.user_profile_repo import UserProfileRepo
from app.domain.types import (
    ConversationSummary,
    EndReason,
    PendingConfirm,
    Resolution,
    Session,
    SessionState,
    SessionUser,
    ToolInvocationStatus,
)
from app.memory.conversation_summary_store import ConversationSummaryStore
from app.memory.session_memory import SessionMemory
from app.memory.summarizer import Summarizer
from app.memory.user_profile import IssueOpen, UserProfileMemory
from app.models import CallCost, Conversation, ToolInvocation
from app.obs.logging import get_logger

log = get_logger(__name__)

# Judgment call #1 (module docstring): 12 hex chars, not the canon
# example's collision-prone 6.
_SESSION_ID_HEX_CHARS = 12


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


@dataclass(frozen=True)
class SummaryPipelineDeps:
    """The external collaborators `SessionManager.end()` needs to run
    docs/09 §8's summary/embed stage (judgment call #10, module
    docstring) — grouped separately from the base `(sessionmaker, redis)`
    constructor args because only the call sites that actually reach
    `end()` need them (`app/api/routes/sessions.py` never does).

    `summarizer_factory` returns a FRESH `Summarizer` on every call,
    never a shared instance — mirrors that class's own "one instance per
    call" contract. `SessionManager` cannot reach the real per-call
    instance a live call would have used (nothing constructs one in
    production yet, judgment call #12), so the factory is the seam that
    makes this testable without coupling to that gap: production wiring
    builds a real closure over its own `LLMRouter`/`SessionMemory`/
    `CostTracker`, tests inject a factory returning a scripted double.
    """

    summarizer_factory: Callable[[], Summarizer]
    conversation_summary_store: ConversationSummaryStore


# docs/09 §8's diagram pairs "Summarizer (Haiku): final summary +
# resolution" as one stage; judgment call #11 (module docstring) is why
# this codebase classifies resolution deterministically instead. The
# three checks below are ordered by how unambiguous the signal is: an
# `EndReason` the caller already decided, a structural Redis field, then
# a tool-audit read — never a guess.
_TOOL_INTENTS: Final[dict[str, str]] = {
    "request_limit_increase": "limit_increase",
    "get_wallet_balance": "balance_check",
    "get_payment_status": "payment_status",
}

# The only mutating (CONFIRM_REQUIRED-tier) tool in today's registry
# (verified via `app/tools/` — grep, three tool modules total). A second
# mutating tool needs its own entry in `_opened_issues_from_invocations`;
# nothing here generalizes across tools automatically, deliberately —
# YAGNI over guessing a shape no second tool has shown yet.
_LIMIT_INCREASE_TOOL: Final = "request_limit_increase"


def _classify_resolution(
    *,
    reason: EndReason,
    pending_confirm: PendingConfirm | None,
    invocations: Sequence[ToolInvocation],
) -> Resolution:
    """Judgment call #11 (module docstring): deterministic, not a second
    LLM call. Checked in this order because the first two are exact,
    structural facts the pipeline already has — nothing here infers
    anything a merchant said:

    1. `reason is EndReason.ESCALATED` -> `ESCALATED`. `end()`'s own
       caller already decided this; mirroring it is not a guess.
    2. A `pending_confirm` still held in Redis at end-of-call ->
       `ABANDONED` — a mutating action was proposed (docs/10 §4's gate)
       and the call ended before it was affirmed, executed, or
       superseded.
    3. Any tool invocation that succeeded (`status == 'ok'`) ->
       `RESOLVED` — something tool-confirmed completed this call.
    4. Otherwise -> `PENDING` — the default: no escalation, no live
       gate, and nothing tool-confirmed finished (a purely informational
       call, or every tool attempt errored/was denied).
    """
    if reason is EndReason.ESCALATED:
        return Resolution.ESCALATED
    if pending_confirm is not None:
        return Resolution.ABANDONED
    if any(inv.status == ToolInvocationStatus.OK.value for inv in invocations):
        return Resolution.RESOLVED
    return Resolution.PENDING


def _classify_intents(invocations: Sequence[ToolInvocation]) -> tuple[str, ...]:
    """One intent per distinct tool INVOKED (attempted, not only
    succeeded — a denied or errored attempt still tells the next call
    what the merchant was trying to do), first-invocation order
    (`dict.fromkeys`, the same order-preserving-dedup idiom
    `app/api/routes/sessions.py::_summary_payload` already uses for its
    own `tools_used`). `_TOOL_INTENTS` maps today's three registered
    tools; an unmapped future tool falls back to its own name rather
    than being silently dropped."""
    return tuple(
        dict.fromkeys(_TOOL_INTENTS.get(inv.tool_name, inv.tool_name) for inv in invocations)
    )


def _tools_used(invocations: Sequence[ToolInvocation]) -> tuple[str, ...]:
    """Distinct tool names invoked this call, first-invocation order —
    mechanical, no classification (module docstring: the easy,
    audit-sourced half of the resolution/intents stage)."""
    return tuple(dict.fromkeys(inv.tool_name for inv in invocations))


def _opened_issues_from_invocations(invocations: Sequence[ToolInvocation]) -> tuple[IssueOpen, ...]:
    """Tool-confirmed `opened_issues` for `UserProfileMemory
    .merge_post_call` (judgment call #13, module docstring). A
    successful `request_limit_increase` becomes one `IssueOpen`, keyed
    by the tool's own `request_id` — a stable identifier the tool
    minted, not one this module invents."""
    issues: list[IssueOpen] = []
    for inv in invocations:
        if inv.tool_name != _LIMIT_INCREASE_TOOL or inv.status != ToolInvocationStatus.OK.value:
            continue
        output = inv.output or {}
        request_id = output.get("request_id")
        if not request_id:
            continue
        input_ = inv.input or {}
        current = input_.get("current_limit")
        requested = input_.get("requested_limit")
        summary = (
            f"Daily limit increase requested: ₹{current:,} → ₹{requested:,}"
            if isinstance(current, int) and isinstance(requested, int)
            else f"Daily limit increase requested (request_id={request_id})"
        )
        issues.append(
            IssueOpen(
                id=str(request_id), summary=summary, status=str(output.get("status") or "pending")
            )
        )
    return tuple(issues)


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
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        redis: RedisClient,
        *,
        summary_pipeline: SummaryPipelineDeps | None = None,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._redis = redis
        self._session_memory = SessionMemory(redis)
        # Judgment call #10 (module docstring): optional, keyword-only —
        # a caller whose SessionManager never reaches end() (e.g.
        # app/api/routes/sessions.py) has nothing to wire.
        self._summary_pipeline = summary_pipeline

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
        *,
        signaling_token_hash: str | None = None,
    ) -> Session:
        """Mint a session id and insert the `conversations` anchor row
        (docs/05 §3.1, docs/12 §4.1). `screen_context`/`recent_events`
        are accepted and ignored — always `None`/`[]` in Phase 3 per
        `SessionManagerProto`'s docstring and
        `protocol/schemas/session_create_request.v1.json`; the
        ScreenContext/AppEvent pipelines are Phase 4. `state` is owned by
        the column's server_default (`'created'`), never set here.

        `signaling_token_hash` is the SHA-256 digest of the one-time
        signaling token the *caller* minted and is storing in Redis
        (judgment call #2). Omitting it mints a real token here and drops
        the plaintext on the floor, producing a preimage-free digest for
        the NOT NULL column — the pre-REST callers' path."""
        session_id = _mint_session_id()
        started_at = datetime.now(UTC)  # judgment call #4 (module docstring)
        token_hash = (
            signaling_token_hash
            if signaling_token_hash is not None
            else mint_signaling_token().token_hash
        )
        async with self._transaction() as db:
            await ConversationRepo(db).create(session_id, user_id, token_hash)
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
                call_cost = await CostRepo(db).get(session_id)
                if call_cost is None:
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

                # Read in the same transaction (cheap, read-only) so the
                # post-call memory stage below — judgment call #10 —
                # doesn't need a second session just for this: it drives
                # both the resolution/intents classification (#11) and
                # the profile merge's opened_issues (#13).
                invocations = await ToolAuditRepo(db).list_for_session(session_id)

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

        # docs/09 §8's post-call memory pipeline — judgment call #10/#14
        # (module docstring). Runs after the transaction above already
        # committed; never raises, so a memory-pipeline hiccup cannot
        # turn a successful call-end into a caller-visible failure or a
        # spurious _end_with_retry cycle (judgment call #14 explains why
        # a retry here would not even help). `call_cost` (the whole row,
        # not `.total_usd` eagerly) is passed through so that attribute
        # access happens inside `_save_summary`'s own try/except, not
        # here unguarded — the summary stage is the only one that needs
        # it, and it must not be able to abort the profile-merge stage
        # too.
        await self._run_post_call_memory(
            session_id=session_id,
            user_id=conversation.user_id,
            reason=reason,
            turn_count=len(turn_records),
            duration_s=duration_s if duration_s is not None else 0,
            call_cost=call_cost,
            invocations=invocations,
        )

    # ------------------------------------------------------------------
    # Post-call memory pipeline (docs/09 §8) — judgment calls #10-#14
    # ------------------------------------------------------------------

    async def _run_post_call_memory(
        self,
        *,
        session_id: str,
        user_id: str,
        reason: EndReason,
        turn_count: int,
        duration_s: int,
        call_cost: CallCost,
        invocations: Sequence[ToolInvocation],
    ) -> None:
        """The summary/embed stage (judgment call #10's optional
        `SummaryPipelineDeps`) and the profile-merge stage (needs no
        extra collaborator), each independently caught so one failing
        never blocks the other — docs/09 §8's "each stage is
        independently skippable" applied at this granularity."""
        intents = _classify_intents(invocations)
        tools_used = _tools_used(invocations)

        if self._summary_pipeline is not None:
            try:
                await self._save_summary(
                    pipeline=self._summary_pipeline,
                    session_id=session_id,
                    user_id=user_id,
                    reason=reason,
                    turn_count=turn_count,
                    duration_s=duration_s,
                    call_cost=call_cost,
                    intents=intents,
                    tools_used=tools_used,
                    invocations=invocations,
                )
            except Exception:
                log.warning(
                    "session_manager.post_call_summary_failed",
                    session_id=session_id,
                    exc_info=True,
                )
        else:
            log.info(
                "session_manager.post_call_summary_pipeline_not_configured",
                session_id=session_id,
            )

        try:
            await self._merge_profile(
                session_id=session_id, user_id=user_id, invocations=invocations
            )
        except Exception:
            log.warning(
                "session_manager.post_call_profile_merge_failed",
                session_id=session_id,
                exc_info=True,
            )

    async def _save_summary(
        self,
        *,
        pipeline: SummaryPipelineDeps,
        session_id: str,
        user_id: str,
        reason: EndReason,
        turn_count: int,
        duration_s: int,
        call_cost: CallCost,
        intents: tuple[str, ...],
        tools_used: tuple[str, ...],
        invocations: Sequence[ToolInvocation],
    ) -> None:
        """Fold the final summary (judgment call #12's honest ceiling),
        classify resolution (#11), and save it — `ConversationSummaryStore
        .save()` owns the embed step and its own failure isolation.
        `call_cost.total_usd` is read here, inside this stage's own
        try/except (its caller's), rather than eagerly by `end()` —
        deliberate: the profile-merge stage needs no cost figure at all
        and must not be able to fail over one."""
        pending_confirm = await self._session_memory.get_pending_confirm(session_id)
        resolution = _classify_resolution(
            reason=reason, pending_confirm=pending_confirm, invocations=invocations
        )

        summarizer = pipeline.summarizer_factory()
        rolling = await summarizer.fold_pending(session_id, thru_turn=turn_count)
        if rolling is None:
            # Judgment call #12: the fresh Summarizer's buffer is empty
            # today (nothing wires observe_turn()/kick() into the turn
            # loop yet) — fall back to whatever the last in-call fold
            # already wrote, so this picks up real content for free the
            # moment that separate wiring lands.
            rolling = await self._session_memory.get_summary(session_id)
        if rolling is None:
            log.info(
                "session_manager.post_call_summary_skipped_no_content", session_id=session_id
            )
            return

        summary = ConversationSummary(
            session_id=session_id,
            user_id=user_id,
            summary=rolling.text,
            resolution=resolution,
            intents=intents,
            tools_used=tools_used,
            turn_count=turn_count,
            duration_s=duration_s,
            cost_usd=call_cost.total_usd,
        )
        await pipeline.conversation_summary_store.save(summary)

    async def _merge_profile(
        self, *, session_id: str, user_id: str, invocations: Sequence[ToolInvocation]
    ) -> None:
        """The profile-merge stage (judgment call #13): tool-confirmed
        `opened_issues` only; `extraction` is deferred (`None`, the
        method's own documented "nothing new stated" value)."""
        opened_issues = _opened_issues_from_invocations(invocations)
        principal = SessionUser(user_id=user_id)
        async with self._transaction() as db:
            await UserProfileMemory(UserProfileRepo(db)).merge_post_call(
                principal,
                session_id=session_id,
                extraction=None,
                opened_issues=opened_issues,
            )


__all__ = ["SessionManager", "SummaryPipelineDeps"]
