"""The four session-lifecycle endpoints (docs/13-api-contracts.md §2).

    POST   /v1/sessions               mint a call session + connect bundle
    POST   /v1/sessions/{id}/token    re-mint token + TURN creds (reconnect)
    DELETE /v1/sessions/{id}          explicit hang-up, idempotent
    GET    /v1/sessions/{id}/summary  post-call summary card

This is the most credential-sensitive route module in the repo: two of the
four endpoints mint live bearer material (a one-time signaling token and a
10-minute TURN HMAC pair). The rules it holds itself to:

- **Nothing here logs a token, a TURN credential, or `TURN_SECRET.`** The
  minting modules (app/auth/) don't log at all; this module logs
  session ids and outcomes only. `SessionCredentials`/`IceServer` carry
  `Field(repr=False)` on their secret fields (app/domain/voice.py's
  judgment call 8) so even an accidental f-string of a whole bundle is
  inert — but the rule is "don't", not "the model will save you".
- **The wire shape comes from `SessionCredentials.to_wire()`**, never a
  hand-rolled dict: a STUN entry must *omit* `username`/`credential`
  rather than null them, which is exactly the trap `to_wire()` exists to
  close.
- **`user_id` in the body is never trusted.** docs/13 §1.2: "No request
  body anywhere accepts a `user_id` that the server trusts — `POST
  /v1/sessions` carries one, but the server verifies it equals `sub` and
  rejects a mismatch rather than honoring it." Everything downstream is
  handed `principal.user_id`, not the body field, so even a future
  refactor that drops the equality check cannot escalate.
- **Session existence is information** (docs/13 §1.1): an unknown id and
  another merchant's id both answer `404 SESSION_NOT_FOUND`, with only
  the id the caller already supplied in `details`.

Judgment calls, flagged rather than buried:

1. **`DELETE` publishes, it does not write `conversations.state`.**
   docs/12 §4.1 gives that column exactly one update, at hang-up, and
   `SessionManager.end()` owns it — including the finalize-before-end
   invariant (that method's judgment call #9) which requires a
   `call_costs` row that only `CostTracker.finalize()` in the voice-worker
   process can have written. agent-api therefore cannot legitimately call
   `end()`; it publishes `"end"` on `session_control:{id}` and the worker
   that owns the peer connection runs finalize-then-end. The response is
   the docs' `{"session_id", "state": "ended"}` either way — it reports
   the terminal state the hang-up commits the call to, not a state read
   back from the row. A repeat DELETE re-publishes and returns the same
   body (docs/13 §2.2: "a repeat call returns the same body, not a 409"),
   which is safe because pub/sub is lossy and the worker's handler must be
   idempotent regardless.
2. **The summary is mechanical, no LLM.** docs/13 §2.3's example prose is
   a Phase-5 Summarizer output; Phase 3 assembles the same *fields* from
   `conversations` + drained `conversation_turns` + `tool_invocations` and
   templates the `summary` string. Nothing here calls a model, so the
   endpoint is deterministic and free.
3. **`SESSION_SUMMARY_PENDING` sets its own `Retry-After` header instead
   of raising.** `ErrorEnvelopeMiddleware` (app/api/middleware.py) only
   special-cases `RateLimitedError` for that header; generalizing it to
   any error carrying `retry_after` is the better fix but middleware.py
   is outside this task's file ownership, so the handler renders that one
   error body itself via `error_envelope()` and sets the header on the
   injected `Response`. Same status code, same envelope, same `error.code`
   — only the plumbing differs, and the swap is a two-line change once
   middleware is in scope.
4. **Only `POST /v1/sessions` is rate-limited.** docs/13 §1.1 budgets
   `rate:{user_id}` at "5 session creates/min" and `require_rate_limit`
   keys one shared window per user. Attaching it to the re-mint endpoint
   too would let five session creates starve a mid-call reconnect — an
   availability bug on the exact path docs/13 §6.1 designs for network
   changes. Re-mint is bounded instead by needing a valid JWT *and* a
   live session the caller owns, and each mint overwrites the previous
   digest, so at most one token per session is ever live.
5. **A `user_id != sub` mismatch answers `400 VALIDATION_SCHEMA`.**
   docs/13 §1.2 mandates the rejection but names no code, and the §1.1
   taxonomy has no 403 at all. The body genuinely fails the contract
   `protocol/schemas/session_create_request.v1.json` states for that field
   ("Must equal the JWT sub claim"), so the validation class is the honest
   one; `details.fields` names the offending field without echoing either
   value.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from typing import Any, Final

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.session_manager import SessionManager
from app.api.deps import get_db, get_principal, require_rate_limit
from app.api.errors import (
    SessionAlreadyEndedError,
    SessionNotFoundError,
    SessionSummaryPendingError,
    ValidationSchemaError,
    error_envelope,
    success_envelope,
)
from app.auth.signaling import SignalingToken, mint_signaling_token, store_signaling_token
from app.auth.turn_credentials import mint_ice_servers
from app.config import Settings
from app.context.context_compressor import ContextCompressor
from app.context.event_log import EventLog
from app.context.snapshot_ingestor import SnapshotIngestor
from app.data.redis_client import RedisClient
from app.data.repositories import ConversationRepo, ToolAuditRepo
from app.domain.types import SessionState, SessionUser, ToolInvocationStatus
from app.domain.voice import SessionCredentials
from app.models import Conversation, ConversationTurn, ToolInvocation
from app.obs.logging import get_logger

log = get_logger(__name__)

router = APIRouter(prefix="/v1/sessions", tags=["sessions"])

# The control message DELETE publishes on `session_control:{id}` for the
# voice-worker (judgment call 1). One word, no envelope: this channel
# carries operator intent, not the docs/13 §6 signaling protocol.
SESSION_CONTROL_END: Final = "end"

# docs/13 §2.3's `resolution.type` for the canonical incident's outcome.
_RESOLUTION_LIMIT_INCREASE: Final = "limit_increase_requested"
_LIMIT_INCREASE_TOOL: Final = "request_limit_increase"

_SECONDS_PER_MINUTE: Final = 60

# Defensive cap on `recent_events`, not a product rule: docs/13 §2.1 says
# the client sends "the last ~15 timeline events" and the schema sets no
# maxItems, so an unbounded array is a free memory/parse amplifier on an
# unauthenticated-adjacent surface. Generous enough never to bind on a
# real capture — the same shape as `_MAX_TURNS_PER_SESSION`
# (app/data/redis_client.py) and the tool layer's `_MAX_LIMIT_RUPEES`.
_MAX_RECENT_EVENTS: Final = 50


# --------------------------------------------------------------------------
# Request bodies (protocol/schemas/session_create_request.v1.json)
# --------------------------------------------------------------------------


class RecentEvent(BaseModel):
    """One `app_event/v1` timeline entry. Only the three fields every
    event variant carries are pinned (`protocol/schemas/app_event.v1.json`
    calls `{type, name, ts}` the docs/08 §2.1 canon); extras are allowed
    through because that schema's `oneOf` owns the per-variant shapes and
    docs/13 §9 makes new fields additive.

    `extra="allow"` is load-bearing, not laxity: it is the only thing that
    carries `api_error.status`/`code`, `nav.from`, `dialog.visible` and
    `input.value` from the wire into `EventLog` (`_persist_recent_events`),
    and `api_error`'s status/code is the single highest-value diagnostic in
    the whole pre-call timeline. Tightening this to `extra="forbid"` — the
    instinct `CreateSessionRequest` below deliberately does apply at the
    body level — would silently amputate every per-variant field and leave
    `ContextCompressor._render_event_line` rendering `api_error POST
    /payments ? ` with the status and code it needs missing."""

    model_config = ConfigDict(extra="allow")

    type: str
    name: str
    ts: int


class CreateSessionRequest(BaseModel):
    """docs/13 §2.1's request body. `extra="forbid"` mirrors the schema's
    `additionalProperties: false` — "new request fields are a server-
    contract change, never a client improvisation". All three fields are
    required (the schema's `required` list); Phase 3 clients send
    `screen_context: null` and `recent_events: []`."""

    model_config = ConfigDict(extra="forbid")

    user_id: str = Field(min_length=1)
    screen_context: dict[str, Any] | None
    recent_events: list[RecentEvent] = Field(max_length=_MAX_RECENT_EVENTS)


# --------------------------------------------------------------------------
# Request-scope accessors for the process singletons this module needs
# --------------------------------------------------------------------------


def get_redis(request: Request) -> RedisClient:
    """`app.state.redis`, the lifespan-built singleton (docs/04 §4).
    A dependency rather than a direct `request.app.state` read so tests
    can swap it through `dependency_overrides`, the same seam `get_db`
    gives the SQL side."""
    return request.app.state.redis


def get_voice_settings(request: Request) -> Settings:
    """`app.state.settings` — the signaling/TURN block (docs/04 §3)."""
    return request.app.state.settings


def get_session_manager(request: Request) -> SessionManager:
    """`SessionManager` owns its own transaction per method (that class's
    docstring), so it takes the sessionmaker, not `get_db`'s
    request-scoped session. Built per request: it holds no state beyond
    the two injected singletons.

    Typed against the concrete class rather than `SessionManagerProto`
    (which `get_llm`'s `LLMProvider` return type would suggest by
    analogy) for a specific reason: the frozen Protocol pins `create()`
    at three positional arguments, and this route passes the
    `signaling_token_hash` keyword the Protocol doesn't know about. The
    Protocol stays the contract for callers that don't mint tokens; this
    one does.
    """
    return SessionManager(request.app.state.sessionmaker, request.app.state.redis)


def get_snapshot_ingestor(redis: RedisClient = Depends(get_redis)) -> SnapshotIngestor:  # noqa: B008
    """Phase-4 T3: a fresh `SnapshotIngestor` per request, over the same
    `RedisClient` singleton `get_redis` hands out and a stateless
    `ContextCompressor` (that class's own docstring: "one instance is
    safely shared across sessions/requests", so constructing one here
    rather than caching it on `app.state` costs nothing). Depending on
    `get_redis` — rather than reading `request.app.state.redis` directly,
    the way `get_session_manager` above does — means a test that swaps
    `get_redis` via `dependency_overrides` (this module's own established
    seam) transparently gets a `SnapshotIngestor` wired to the same fake.

    Mirrors docs/08 §4's "same class, same validation, two entrypoints":
    this is the REST entrypoint's instance; `app/voice/run.py`'s
    `_build_brain_stack` builds the data-channel entrypoints' instance the
    identical way, over the process-long `redis` singleton.
    """
    return SnapshotIngestor(redis, ContextCompressor())


def get_event_log(redis: RedisClient = Depends(get_redis)) -> EventLog:  # noqa: B008
    """Phase-4 T2 follow-up: the `ctx:{session_id}:events` writer for the
    `recent_events` half of docs/08 §6's session-create sequence, built the
    same way (and for the same reasons) as `get_snapshot_ingestor` above —
    a fresh, stateless wrapper per request over the one `RedisClient`
    `get_redis` hands out, so a test that swaps `get_redis` transparently
    gets an `EventLog` on the same fake.

    Mirrors `app/voice/run.py`'s `_build_brain_stack`, which builds the
    in-call entrypoint's `EventLog` over the process-long `redis` singleton
    identically — one class, two entrypoints, exactly as `SnapshotIngestor`
    already is.
    """
    return EventLog(redis)


# --------------------------------------------------------------------------
# Validation + ownership helpers
# --------------------------------------------------------------------------


def _require_matching_user_id(body_user_id: str, principal: SessionUser) -> None:
    """docs/13 §1.2's verify-don't-honor rule (judgment call 5). Neither
    value is echoed back — the response says which field is wrong, not
    what the server thinks the right answer is."""
    if body_user_id != principal.user_id:
        raise ValidationSchemaError(
            "user_id must equal the authenticated principal",
            details={
                "fields": [
                    {"loc": ["body", "user_id"], "msg": "must equal the JWT sub claim"}
                ]
            },
        )


async def _require_own_session(
    db: AsyncSession, session_id: str, principal: SessionUser
) -> Conversation:
    """Load the session or raise the ownership-blind `404
    SESSION_NOT_FOUND` (docs/13 §1.1/§2.2: an id owned by someone else is
    deliberately indistinguishable from a wrong id)."""
    conversation = await ConversationRepo(db).get(session_id)
    if conversation is None or conversation.user_id != principal.user_id:
        raise SessionNotFoundError(
            "No session found for this id", details={"session_id": session_id}
        )
    return conversation


def _require_signaling_url(settings: Settings) -> str:
    """`SIGNALING_PUBLIC_URL` is optional-with-default in `Settings` so
    agent-api boots with no voice env (app/config.py's stated design) —
    which makes this endpoint the place that actually needs it. Raises a
    plain `RuntimeError` for the same reason `turn_credentials.py` does
    (its note 4): an operator misconfiguration, rendered to the client as
    the generic `500 INTERNAL`."""
    if not settings.signaling_public_url:
        raise RuntimeError(
            "SIGNALING_PUBLIC_URL is not configured — agent-api cannot tell a client "
            "where the voice-worker's /v1/signal WebSocket lives (docs/13 §2.1)."
        )
    return settings.signaling_public_url


# --------------------------------------------------------------------------
# The connect bundle (docs/13 §2.1)
# --------------------------------------------------------------------------


async def _issue_connect_bundle(
    *,
    session_id: str,
    minted: SignalingToken,
    settings: Settings,
    redis: RedisClient,
    now: datetime,
) -> SessionCredentials:
    """Compute the TURN pair, store the token's digest under its 5-minute
    TTL, and assemble the bundle. `expires` is the *token's* connect
    deadline, not the session's lifetime (docs/13 §2.1), so it is derived
    from the same TTL the Redis key just got — one number, two places it
    must agree.

    Ordering inside this function is deliberate on both ends: every
    fallible *computation* (misconfigured `SIGNALING_PUBLIC_URL`,
    `TURN_SECRET`, `COTURN_HOST`) happens before the Redis write, so a
    misconfigured deployment leaves no orphan token digests behind; and
    the write happens before the function returns, so the plaintext can
    never leave this process as a token the worker has no way to verify.

    Scope note: this covers Redis only. `POST /v1/sessions` inserts the
    `conversations` row *before* calling here — that is docs/13 §2.1's own
    ordering ("mint `a1f3c9` ... then mint the one-time signaling token")
    — so a misconfigured deployment does accumulate orphan `conversations`
    rows, one per failed attempt, until the config is fixed. Left as-is
    rather than reordered: the row is the session's identity and the token
    is minted *for* a session id, so inverting them would mean minting
    credentials for an id that might never be persisted.
    """
    signaling_url = _require_signaling_url(settings)
    ice_servers = mint_ice_servers(session_id, settings, now=now)
    await store_signaling_token(
        redis, session_id, minted.token_hash, ttl_s=settings.signaling_token_ttl_s
    )
    return SessionCredentials(
        session_id=session_id,
        signaling_url=signaling_url,
        signaling_token=minted.token,
        ice_servers=ice_servers,
        expires=now + timedelta(seconds=settings.signaling_token_ttl_s),
    )


# --------------------------------------------------------------------------
# Initial screen-context persistence (docs/08 §3.1 / §4.1) — Phase-4 T3
# --------------------------------------------------------------------------


def _now_ms() -> int:
    return int(time.time() * 1000)


async def _ingest_initial_screen_context(
    snapshot_ingestor: SnapshotIngestor, session_id: str, screen_context: dict[str, Any]
) -> None:
    """Best-effort persistence of the REST-supplied snapshot into
    `ctx:{session_id}` (docs/08 §3.1's "context before audio"; §4.1's REST
    entrypoint). Replaces the old `_require_supported_screen_context`
    placeholder (this route's previous docstring: "Phase 4 owns
    ScreenContext ingestion") — that stub only checked the version marker
    and never validated or stored anything; `SnapshotIngestor`'s own
    module docstring names itself as that stub's "eventual full
    replacement."

    Judgment call: neither an `IngestResult` with `accepted=False` (bad
    version marker, a schema violation, or an oversize payload that still
    fails the schema after recompression) NOR a genuine infrastructure
    failure writing to Redis may fail this call — this function only runs
    after `session_manager.create()` already returned a live session, so
    the connect bundle downstream is unaffected either way. docs/08 §7's
    closing line is explicit that "the pipeline degrades context, never
    conversation": a merchant calling in with a stale app build, a
    corrupted capture, or hitting a transient Redis blip must still be
    able to start the call, just with `ContextBuilder`'s slot 4 reading
    back empty (`ContextBuilder._get_screen_context`'s own judgment call
    #5) — identical to sending `screen_context: null`, not a new failure
    mode. Review fix: `SnapshotIngestor.ingest_initial_snapshot`'s final
    step (`_write_ctx`) is an unguarded `redis.raw.set(...)` with no
    try/except of its own — a connection drop/timeout there previously
    propagated uncaught through this route into `ErrorEnvelopeMiddleware`,
    turning an already-successful session creation (the `conversations`
    row is already committed by this point) into a client-visible 500.
    That Redis failure is caught here (originally as `RedisError`; see the
    audit-fix note below for why the guard is now broader).
    `SnapshotIngestor` already logs every REJECTION path at WARNING
    internally (its `_reject_*` helpers, each carrying `session_id` and a
    `reason`) with a stable, dedicated event name per check — logging
    again here would only duplicate that signal under a third name, so
    this function logs nothing on a normal rejection; it logs its own
    WARNING only for the genuine-infrastructure-failure case this fix
    adds, and a single INFO line on acceptance, mirroring this route's
    existing `session_created` log.

    Audit fix (2026-08-04): the guard above was `except RedisError`, which
    covered only the `_write_ctx` failure mode it was written for and left
    a second, non-Redis crash path wide open. `ingest_initial_snapshot`
    runs its size cap BEFORE schema validation, and the oversize branch
    hands the payload to `ContextCompressor`'s drop ladder, whose rungs
    call `component.get("role")` on each entry of `components`. A payload
    that is both over the 8 KiB cap AND structurally malformed (e.g.
    `components` holding strings rather than objects — a buggy or
    outdated client build, not a crafted attack) therefore raised
    `AttributeError` straight through this guard and turned an
    already-committed session creation into a client-visible 500 —
    precisely the failure this function exists to prevent, reached by a
    different door. Reproduced against the real ASGI stack before fixing.
    The guard is now `except Exception`: for THIS function the exception
    type is genuinely irrelevant — docs/08 §7's rule is absolute ("the
    pipeline degrades context, never conversation"), and there is no
    failure mode of context ingestion that should be allowed to fail a
    call the merchant is trying to start. `exc_info=True` keeps the real
    traceback in the logs, so a broadened catch here hides nothing from
    operators; the in-call data-channel path (`ContextDispatcher.run`)
    already made exactly this call for exactly this reason.
    """
    try:
        result = await snapshot_ingestor.ingest_initial_snapshot(
            session_id, screen_context, received_at_ms=_now_ms()
        )
    except Exception:
        log.warning("session_screen_context_ingest_failed", session_id=session_id, exc_info=True)
        return
    if result.accepted:
        log.info(
            "session_screen_context_ingested",
            session_id=session_id,
            outcome=result.outcome.value,
        )


# --------------------------------------------------------------------------
# Pre-call timeline persistence (docs/08 §4.2 / §6) — Phase-4 T2 follow-up
# --------------------------------------------------------------------------


async def _persist_recent_events(
    event_log: EventLog, session_id: str, recent_events: list[dict[str, Any]]
) -> None:
    """Best-effort persistence of the REST-supplied `recent_events` timeline
    into `ctx:{session_id}:events` — the second of the two writes docs/08
    §6's session-create sequence diagram shows, and docs/13 §2.1's "write
    `ctx:a1f3c9` **and the events list**".

    Until this landed, this route accepted `recent_events`, handed them to
    `SessionManager.create()` (which "accepts and ignores" them, per its own
    docstring), and nothing ever called `EventLog.append` — so
    `ContextBuilder`'s slot-5 timeline read back empty for the opening turns
    of every call, which is precisely the "what were they just doing"
    context this field exists to carry. The previous version of this route's
    `create_session` docstring named that gap as deliberately deferred; this
    function is that deferral being paid off.

    Order is the contract, and it is the caller's, not this function's.
    `session_create_request.v1.json` pins `recent_events` as **oldest
    first** and this function appends in the order it is given, because
    `EventLog.append` is `RPUSH` — so list position *is* chronological
    order, and `ContextCompressor.render_timeline_slot` (which slices
    `events[-15:]` for "the newest 15") reads correctly only if that
    invariant holds all the way from the client. Nothing here re-sorts by
    `ts`: a server-side sort would silently paper over a client that got the
    ordering wrong, and the Android side (`AppStateManager.sessionCreateBody`)
    now reverses `EventTracker.recent()`'s newest-first buffer at the one
    place that speaks this wire, which is where that fix belongs.

    Judgment call — the guard, and why it is `except Exception`: identical
    reasoning to `_ingest_initial_screen_context` above, and deliberately
    the same shape rather than a narrower `RedisError`. This function only
    runs after `session_manager.create()` has already committed the
    `conversations` row, so by the time it can fail the session exists and
    the caller is owed its connect bundle. docs/08 §7's rule is absolute —
    "the pipeline degrades context, never conversation" — and there is no
    failure mode of timeline persistence (a Redis blip, an orjson encode
    failure on some future payload shape) that should be allowed to fail a
    call the merchant is trying to start. The degraded outcome is a session
    whose slot-5 timeline reads back empty, which is exactly what a client
    sending `recent_events: []` already produces — not a new failure mode.
    `exc_info=True` keeps the real traceback in the logs, so the broad catch
    hides nothing from operators.

    Audit fix (2026-08-05) — `event_log.append_many`, not a loop over
    `event_log.append`. The loop shape cost up to `_MAX_RECENT_EVENTS` (50)
    calls to a 3-round-trip method — up to 150 sequential awaited Redis
    round trips blocking the `201` on exactly the "merchant is trying to
    start a support call" path docs/13 §2.1 says the ergonomics matter for.
    `append_many` pipelines the whole batch (`RPUSH` all events, `LTRIM`,
    `EXPIRE`) into one round trip, the same tool `enforce_rate`
    (`app/data/redis_client.py`) already uses for the identical reason.

    This does change one thing the loop-shaped version of this docstring
    used to claim: `append_many`'s pipeline is a Redis `MULTI`/`EXEC`
    transaction, so a failure now yields *nothing* written rather than a
    truncated oldest-first prefix. That is still exactly the already-
    accepted degraded outcome, not a new failure mode: a fully-empty
    `ctx:{session_id}:events` reads back identically to a client that sent
    `recent_events: []`, which this function already treats as fine. What
    changed is which *empty-or-full* outcome a mid-write failure produces,
    not whether a failure here can fail the call — it still cannot.
    """
    if not recent_events:
        return
    try:
        await event_log.append_many(session_id, recent_events)
    except Exception:
        log.warning("session_recent_events_persist_failed", session_id=session_id, exc_info=True)
        return
    log.info(
        "session_recent_events_persisted", session_id=session_id, event_count=len(recent_events)
    )


# --------------------------------------------------------------------------
# Summary assembly (docs/13 §2.3) — mechanical, no LLM (judgment call 2)
# --------------------------------------------------------------------------


def _format_duration(duration_s: int) -> str:
    minutes, seconds = divmod(duration_s, _SECONDS_PER_MINUTE)
    return f"{minutes}m {seconds}s" if minutes else f"{seconds}s"


def _resolution(invocations: list[ToolInvocation]) -> dict[str, Any] | None:
    """docs/13 §2.3's `resolution`, pulled from the most recent successful
    `request_limit_increase` audit row. `output` is that tool's
    `RequestLimitIncreaseOut` dumped to JSON by `ToolExecutor`
    (app/agent/tool_executor.py), so `request_id`/`eta_hours` are the
    tool's own contract (docs/10 §3.1) rather than a shape invented here.
    Returns `None` when the call reached no such resolution — the field
    stays present-and-null so clients can branch on it."""
    for invocation in reversed(invocations):
        if invocation.tool_name != _LIMIT_INCREASE_TOOL:
            continue
        if invocation.status != ToolInvocationStatus.OK.value:
            continue
        output = invocation.output or {}
        reference = output.get("request_id")
        if reference is None:
            continue
        return {
            "type": _RESOLUTION_LIMIT_INCREASE,
            "reference": reference,
            "eta_hours": output.get("eta_hours"),
        }
    return None


def _summary_text(
    *,
    duration_s: int,
    turn_count: int,
    tools_used: list[str],
    resolution: dict[str, Any] | None,
) -> str:
    """The template `summary` string (judgment call 2). Deterministic and
    fact-only: every clause restates a number this endpoint already
    returns as a structured field, so the prose can never disagree with
    the data beside it — the Phase-5 Summarizer replaces this wholesale
    rather than being approximated here."""
    parts = [f"Call lasted {_format_duration(duration_s)} over {turn_count} turns."]
    parts.append(f"Tools used: {', '.join(tools_used)}." if tools_used else "No tools were used.")
    if resolution is not None:
        eta_hours = resolution.get("eta_hours")
        eta = f", ETA {eta_hours} hours" if eta_hours is not None else ""
        parts.append(
            f"Submitted a daily-limit increase, reference {resolution['reference']}{eta}."
        )
    return " ".join(parts)


def _summary_payload(
    conversation: Conversation,
    ended_at: datetime,
    turns: list[ConversationTurn],
    invocations: list[ToolInvocation],
) -> dict[str, Any]:
    """docs/13 §2.3's `data` object. Deliberately absent, per that
    section: cost fields (`call_costs` is internal) and the transcript
    (never persisted — docs/12 §4.2; the summary *is* the record).

    `ended_at` is passed in rather than re-read off `conversation` so the
    not-null narrowing lives at the caller's pending gate — the one place
    that actually establishes it — instead of an assert down here.
    """
    duration_s = int((ended_at - conversation.started_at).total_seconds())
    actions = [{"tool": i.tool_name, "status": i.status} for i in invocations]
    resolution = _resolution(invocations)
    # dict.fromkeys, not set(): first-use order, so the sentence reads in
    # the order the call actually used the tools.
    tools_used = list(dict.fromkeys(i.tool_name for i in invocations))
    return {
        "session_id": conversation.session_id,
        "started_at": conversation.started_at.isoformat(),
        "duration_s": duration_s,
        "turn_count": len(turns),
        "summary": _summary_text(
            duration_s=duration_s,
            turn_count=len(turns),
            tools_used=tools_used,
            resolution=resolution,
        ),
        "resolution": resolution,
        "actions": actions,
    }


# --------------------------------------------------------------------------
# Endpoints
# --------------------------------------------------------------------------


@router.post("", status_code=201, dependencies=[Depends(require_rate_limit)])
async def create_session(
    body: CreateSessionRequest,
    principal: SessionUser = Depends(get_principal),  # noqa: B008
    session_manager: SessionManager = Depends(get_session_manager),  # noqa: B008
    redis: RedisClient = Depends(get_redis),  # noqa: B008
    settings: Settings = Depends(get_voice_settings),  # noqa: B008
    snapshot_ingestor: SnapshotIngestor = Depends(get_snapshot_ingestor),  # noqa: B008
    event_log: EventLog = Depends(get_event_log),  # noqa: B008
) -> dict[str, Any]:
    """docs/13 §2.1. Order of server-side effects, as that section pins
    them: validate the whole body -> rate-limit (the `require_rate_limit`
    dependency, which runs before this body) -> mint the session ->
    store the hashed one-time token -> compute the TURN pair -> return.

    Phase-4 T3: the session mint is followed by a best-effort
    `SnapshotIngestor.ingest_initial_snapshot()` call for a present
    `screen_context` (`_ingest_initial_screen_context`'s own docstring
    covers the accepted/rejected judgment call in full — short version:
    a rejection degrades the session's screen-context slot, never this
    response). It runs AFTER `session_manager.create()`, not before,
    because `SnapshotIngestor` writes keyed by `session_id`, which does
    not exist until the session is minted — unlike the old
    `_require_supported_screen_context` placeholder this replaces, which
    ran (and could reject) before any session existed at all.

    Both of docs/08 §6's session-create writes now happen here, in that
    diagram's own order: the snapshot into `ctx:{session_id}`, then the
    timeline into `ctx:{session_id}:events` (`_persist_recent_events`,
    same after-the-mint placement and the same degrade-never-fail guard,
    for the same reason). Neither can fail this response.

    Still deliberately absent, and deliberately not faked: the speculative
    context prefetch docs/13 §2.1 also lists (no prefetcher exists yet).
    Additive later without changing this response.
    """
    _require_matching_user_id(body.user_id, principal)

    minted = mint_signaling_token()
    # One `model_dump()` per event, reused for both the (ignoring)
    # `SessionManager.create()` argument and the `EventLog` write, so the
    # two can never see differently-shaped copies of the same timeline.
    # `RecentEvent` is `extra="allow"`, so this carries the per-variant
    # fields (`nav.from`, `api_error.status`/`code`, `dialog.visible`,
    # `input.value`) straight through to Redis — docs/08 §2.1's per-type
    # table is what makes the timeline diagnostic rather than decorative.
    recent_events = [event.model_dump() for event in body.recent_events]
    session = await session_manager.create(
        principal.user_id,
        body.screen_context,
        recent_events,
        signaling_token_hash=minted.token_hash,
    )
    if body.screen_context is not None:
        await _ingest_initial_screen_context(
            snapshot_ingestor, session.session_id, body.screen_context
        )
    await _persist_recent_events(event_log, session.session_id, recent_events)
    credentials = await _issue_connect_bundle(
        session_id=session.session_id,
        minted=minted,
        settings=settings,
        redis=redis,
        now=datetime.now(UTC),
    )
    log.info("session_created", session_id=session.session_id, user_id=principal.user_id)
    return success_envelope(credentials.to_wire())


@router.post("/{session_id}/token")
async def remint_session_token(
    session_id: str,
    principal: SessionUser = Depends(get_principal),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
    redis: RedisClient = Depends(get_redis),  # noqa: B008
    settings: Settings = Depends(get_voice_settings),  # noqa: B008
) -> dict[str, Any]:
    """docs/13 §2.2's reconnect path: a fresh one-time token and fresh
    TURN credentials for a session whose state is intact (docs/13 §6.1 —
    the client calls this after a network change killed its WebSocket,
    then reconnects and re-offers with `iceRestart`).

    The new digest overwrites the old one, so the token this replaces
    stops verifying immediately — a re-mint is also a revoke. An ended
    session is `409 SESSION_ALREADY_ENDED`: there is nothing left to
    connect to, and minting a live credential for a dead call would be a
    small credential leak with no upside.
    """
    conversation = await _require_own_session(db, session_id, principal)
    if conversation.state == SessionState.ENDED.value:
        raise SessionAlreadyEndedError(
            "This session has already ended", details={"session_id": session_id}
        )

    credentials = await _issue_connect_bundle(
        session_id=session_id,
        minted=mint_signaling_token(),
        settings=settings,
        redis=redis,
        now=datetime.now(UTC),
    )
    log.info("signaling_token_reminted", session_id=session_id)
    return success_envelope(credentials.to_wire())


@router.delete("/{session_id}")
async def end_session(
    session_id: str,
    principal: SessionUser = Depends(get_principal),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
    redis: RedisClient = Depends(get_redis),  # noqa: B008
) -> dict[str, Any]:
    """docs/13 §2.2's explicit hang-up — idempotent by contract: a repeat
    call returns the same body, never a `409`, because hang-up races the
    in-band `bye` and aiortc's own teardown constantly and every path
    converges on the same terminal state.

    See judgment call 1 for why this publishes rather than writing
    `conversations.state` itself. `subscribers=0` is logged, not raised:
    pub/sub is lossy and a worker that already tore the call down is the
    normal reason nobody is listening.
    """
    await _require_own_session(db, session_id, principal)
    subscribers = await redis.publish_session_control(session_id, SESSION_CONTROL_END)
    log.info("session_end_requested", session_id=session_id, subscribers=subscribers)
    return success_envelope(
        {"session_id": session_id, "state": SessionState.ENDED.value}
    )


@router.get("/{session_id}/summary")
async def get_session_summary(
    session_id: str,
    response: Response,
    principal: SessionUser = Depends(get_principal),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> dict[str, Any]:
    """docs/13 §2.3's post-call summary card, assembled mechanically
    (judgment call 2).

    The pending gate is `SessionManager.end()` having *committed*: that
    method flips `state`/`ended_at` and drains `session:{id}:turns` into
    `conversation_turns` in one transaction, so `state == 'ended'` is
    exactly the point at which the turn rows this endpoint counts are
    visible. Reading a half-drained session is therefore not possible —
    it either sees the whole post-call write or answers `404
    SESSION_SUMMARY_PENDING` with `Retry-After: 2` (judgment call 3).
    """
    conversation = await _require_own_session(db, session_id, principal)
    ended_at = conversation.ended_at
    if conversation.state != SessionState.ENDED.value or ended_at is None:
        pending = SessionSummaryPendingError(
            "The post-call summary for this session is not ready yet",
            details={"session_id": session_id},
        )
        response.status_code = pending.status_code
        response.headers["Retry-After"] = str(pending.retry_after)
        return error_envelope(pending)

    turns = await ConversationRepo(db).list_turns(session_id)
    invocations = await ToolAuditRepo(db).list_for_session(session_id)
    return success_envelope(_summary_payload(conversation, ended_at, turns, invocations))


__all__ = ["SESSION_CONTROL_END", "router"]
