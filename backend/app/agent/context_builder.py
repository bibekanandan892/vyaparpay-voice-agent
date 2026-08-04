"""`ContextBuilder` — assembles the per-turn `ContextBundle`
(docs/05-agent-architecture.md §3.3; slot semantics owned by
docs/11-prompt-engineering.md §1, assembly by docs/08-context-and-events.md §5).

Phase-2 scope (plan decision #1) populated only persona, business_rules,
user_profile, conversation, and current_utterance; slots 4–7
(screen_context / recent_actions / memory_summary / knowledge) stayed at
their `""` defaults on the frozen `ContextBundle`, still rendered as empty
tag pairs by `PromptBuilder` — Phase 4/5 only start *populating* them,
never restructuring the template. Phase-4 T3 (this revision) populates
slots 4 and 5 for real, from the `app/context/` pipeline T2 built
(`SnapshotIngestor` / `EventLog` / `ContextCompressor`, docs/08 §4);
slots 6–7 (memory_summary / knowledge) remain Phase-5 scope.

Judgment calls made in this module, flagged per house style:

1. **Prompt files are read once at construction, not per call.** The
   prefix cache keys on byte-identical slot-1/2 bytes across every call
   (docs/11 §1.1); loading once guarantees that stability for the
   process lifetime and keeps file I/O off the 15/40 ms `context.build`
   hot path (docs/08 §5). A missing/unreadable prompt file therefore
   fails *construction* — loudly, at startup — because docs/05 §3.3's
   "degrade context, never the turn" policy is about per-turn data
   sources (DB, Redis); a missing deploy artifact is a broken deploy,
   not a degradable turn.
2. **The user_profile line carries no personal name.** docs/11 §4's
   example opens with "Rajesh Kumar." but `merchants` (docs/12 §3.1,
   app/models/orm.py) has no personal-name column — only business_name,
   city, account_type, preferred_language, merchant_since, kyc_status.
   The rendered line therefore starts at the "Business:" segment and
   otherwise follows the doc's format exactly ("Merchant since <year>"
   uses `merchant_since.year`). The personal name presumably arrives
   with the Phase-5 `user_profiles` table (docs/12's ER diagram);
   nothing in Phase 2 can supply it honestly.
3. **A missing merchant row (or a DB failure) degrades the slot, not
   the turn** (docs/05 §3.3). Both produce the same deterministic
   `_PROFILE_UNAVAILABLE` line, which stays byte-stable within the call
   (preserving slot 3's within-a-call cache role, docs/11 §1.1) and
   steers the model toward tools instead of invented facts (docs/10 §1
   invariant 1). The failure is logged, never swallowed silently.
4. **A Redis/deserialization failure degrades the window to empty**
   for the same reason: the never-drop set is system, business rules,
   current utterance, pending-confirm (docs/08 §5.2) — the conversation
   window is explicitly shrinkable, so an empty window is a degraded
   but correct turn, while an exception here would kill it.
5. **No stored `ctx:{session_id}` is the expected common case, not a
   failure, for slot 4.** Until the data-channel `ctx.*` dispatcher (T4)
   exists, a session can genuinely reach a turn having never had a
   screen snapshot ingested at all (no REST `screen_context`, no in-call
   `ctx.snapshot` yet) — that is Redis correctly reporting "nothing
   here," not an error condition. `_get_screen_context` therefore treats
   a missing key the same as judgment call #4's degraded-but-correct
   empty window: silent `""`, no warning log. A key that *is* present
   but fails to decode (`RedisError` reaching the client, corrupt JSON,
   or a stored dict missing the `received_ts`/`seq` fields
   `SnapshotIngestor` always writes) is the genuine failure case and
   logs at WARNING with `exc_info=True`, same bar as judgment call #4.
   Staleness marking and the ≤300-token budget are `ContextCompressor
   .render_snapshot_slot`'s job (docs/08 §4.3), not re-implemented here.
6. **Slot 5 mirrors slot 4's degrade rule, reading only the newest
   `TIMELINE_MAX_EVENTS` events from `EventLog` rather than the full
   200-entry cap.** `ContextCompressor.render_timeline_slot` re-slices to
   the newest N regardless, but asking `EventLog.get_events(...,
   limit=TIMELINE_MAX_EVENTS)` for only what the slot can ever render
   keeps the `context.build` Redis read proportional to the prompt
   budget instead of the full forensic log (docs/08 §4.2: "the surplus
   exists for gap forensics," which is not this hot path's concern). An
   empty event list — no events ever appended for this session — is the
   same expected-not-failed case as judgment call #5.

The merchant row is re-read each turn, yet slot 3 must be byte-stable
within a call (docs/11 §1.1): that holds because merchants are not
self-service-editable (MerchantRepo's own docstring) — the row cannot
change mid-call in Phase 2.
"""

from __future__ import annotations

import time
from pathlib import Path

import orjson
from opentelemetry.trace import Status, StatusCode
from redis.exceptions import RedisError
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.context.context_compressor import TIMELINE_MAX_EVENTS, ContextCompressor
from app.context.event_log import EventLog
from app.context.redis_keys import _ctx_key
from app.data.redis_client import RedisClient
from app.data.repositories.merchant_repo import MerchantRepo
from app.domain.types import ContextBundle, Message, Session
from app.memory.session_memory import SessionMemory
from app.models import Merchant
from app.obs.logging import get_logger
from app.obs.tracing import SPAN_CONTEXT_BUILD, get_tracer, safe_set_attribute

log = get_logger(__name__)
tracer = get_tracer(__name__)

_PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"

# Deterministic fallback for judgment call #3 — phrased to keep the model
# inside docs/10 §1 invariant 1 (account facts come from tools, never memory).
_PROFILE_UNAVAILABLE = (
    "Profile unavailable for this caller. Do not state or guess any account "
    "details; anything account-specific must come from a tool call."
)


def _load_prompt(filename: str) -> str:
    """Read one prompts/*.md file. Explicit UTF-8: the files carry ₹ and
    en-dashes, and Windows' locale default (cp1252) would corrupt them —
    which, per docs/11 §1.1, would also silently change the cached-prefix
    bytes between differently-configured hosts."""
    return (_PROMPTS_DIR / filename).read_text(encoding="utf-8").strip()


def _now_ms() -> int:
    """Epoch-ms `time.time()` wrapper — the same shape
    `app/voice/worker.py`'s `_now_epoch_ms` uses, kept local rather than
    imported since that module owns turn-loop timing, not context
    assembly. Not injected as a constructor clock (unlike
    `VoiceAgentWorker`'s `now_ms` parameter): nothing in this class's
    tests needs to freeze wall-clock time, only to control the *relative*
    offset between a fixture's `received_ts`/event `ts` and "now" —
    which a test can already do by computing its fixture timestamps off
    a real `time.time()` read at setup."""
    return int(time.time() * 1000)


def _format_profile(merchant: Merchant) -> str:
    """docs/11 §4's compact one-line format, minus the personal name the
    schema cannot supply (judgment call #2). NO balances or statuses ever
    — docs/11 §1: those are tool-only."""
    return (
        f"Business: {merchant.business_name}, {merchant.city}. "
        f"Merchant since {merchant.merchant_since.year}. "
        f"Account type: {merchant.account_type}. "
        f"Preferred language: {merchant.preferred_language}."
    )


class ContextBuilder:
    """Implements `ContextBuilderProto` (app/domain/interfaces.py).

    Dependencies are injected: the app-lifespan `async_sessionmaker`
    (docs/04 §4 — this module never opens its own engine) for the
    per-turn MerchantRepo read, the shared `SessionMemory` for the
    transcript window, the shared `RedisClient` for the raw `ctx:
    {session_id}` snapshot read (slot 4 — there is no wrapper class for
    this read side; `SnapshotIngestor` only exposes `ingest_*` writes,
    per that module's own docstring), a shared `EventLog` for the event
    timeline read (slot 5), and a shared `ContextCompressor` — stateless,
    per that class's own docstring ("one instance is safely shared") —
    that renders both into prompt-ready slot text.
    """

    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        session_memory: SessionMemory,
        redis: RedisClient,
        event_log: EventLog,
        compressor: ContextCompressor,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._memory = session_memory
        self._redis = redis
        self._event_log = event_log
        self._compressor = compressor
        # Judgment call #1: loaded once, byte-stable for the process lifetime.
        self._persona = _load_prompt("persona.md")
        self._business_rules = _load_prompt("business_rules.md")

    async def build(self, session: Session, *, current_utterance: str) -> ContextBundle:
        # docs/04 §7.2's context.build span (previously deferred — see
        # this module's git history). `session_id` is the one canon
        # attribute this call site genuinely has: there's no
        # prompt-prefix cache hit/miss signal anywhere in this class
        # (judgment call #1 loads persona/business_rules once at
        # construction, but never records a per-call hit/miss check) —
        # `cache_hit` is deliberately NOT set here rather than forced to
        # a guessed value.
        #
        # record_exception=False/set_status_on_exception=False (security
        # review LOW, applied for consistency with llm_router.py's two
        # spans, which fixed the same class of leak as a HIGH): OTel's
        # defaults would attach str(exc) + a traceback verbatim to the
        # span on any uncaught exception. Today _build_user_profile and
        # _get_window each catch their full expected exception surface
        # internally, so nothing content-bearing reaches this boundary —
        # but that's an invariant of THOSE methods' except tuples, not of
        # this span, and a future uncaught exception type (or a
        # ContextBundle field constraint whose ValidationError echoes
        # merchant data) would silently reopen the leak. The except
        # branch below records the exception's TYPE NAME only.
        with tracer.start_as_current_span(
            SPAN_CONTEXT_BUILD, record_exception=False, set_status_on_exception=False
        ) as span:
            safe_set_attribute(span, "session_id", session.session_id)
            try:
                user_profile = await self._build_user_profile(session.user_id)
                conversation = await self._get_window(session.session_id)
                screen_context = await self._get_screen_context(session.session_id)
                recent_actions = await self._get_recent_actions(session.session_id)
                return ContextBundle(
                    persona=self._persona,
                    business_rules=self._business_rules,
                    user_profile=user_profile,
                    screen_context=screen_context,
                    recent_actions=recent_actions,
                    # slots 6-7 stay at their "" defaults (Phase 5 scope)
                    conversation=tuple(conversation),
                    current_utterance=current_utterance,
                )
            except Exception as exc:
                span.set_status(Status(StatusCode.ERROR, description=type(exc).__name__))
                raise

    async def _build_user_profile(self, merchant_id: str) -> str:
        # _format_profile inside the try (review MEDIUM): today every
        # column it reads is NOT NULL so it can't raise, but the module's
        # stated policy is degrade-the-slot-never-the-turn — a future
        # nullable column must hit the same fallback, not crash the turn.
        try:
            async with self._sessionmaker() as db:
                merchant = await MerchantRepo(db).get(merchant_id)
            if merchant is None:
                log.warning("context.user_profile.merchant_missing", merchant_id=merchant_id)
                return _PROFILE_UNAVAILABLE
            return _format_profile(merchant)
        except (SQLAlchemyError, AttributeError, TypeError, ValueError):
            # Judgment call #3: degrade the slot, never the turn (docs/05 §3.3).
            log.warning("context.user_profile.db_error", merchant_id=merchant_id, exc_info=True)
            return _PROFILE_UNAVAILABLE

    async def _get_window(self, session_id: str) -> list[Message]:
        try:
            return await self._memory.get_window(session_id)
        except (RedisError, ValueError):
            # Judgment call #4. ValueError covers both orjson decode errors
            # (RedisClient.get_transcript_window) and pydantic's
            # ValidationError (Message.model_validate) — both subclass it.
            log.warning("context.conversation.window_error", session_id=session_id, exc_info=True)
            return []

    async def _get_screen_context(self, session_id: str) -> str:
        # Judgment call #5. `raw is None` — no snapshot ever ingested for
        # this session — is the expected common case until T4 wires the
        # data-channel dispatcher, so it degrades silently, matching
        # `ContextBundle.screen_context`'s Phase-2 "" default exactly.
        try:
            raw = await self._redis.raw.get(_ctx_key(session_id))
            if raw is None:
                return ""
            stored = orjson.loads(raw)
            slot = self._compressor.render_snapshot_slot(stored, now_ms=_now_ms())
            return slot.text
        except (RedisError, ValueError, KeyError, TypeError):
            # RedisError: connection/timeout. ValueError: orjson decode
            # failure (subclasses JSONDecodeError). KeyError/TypeError:
            # `render_snapshot_slot` indexes `stored["received_ts"]`
            # unconditionally — a stored value that isn't the dict shape
            # `SnapshotIngestor` always writes (KeyError on a dict missing
            # the field, TypeError if `stored` decoded to a non-dict JSON
            # value) is exactly as unexpected as a decode failure and gets
            # the same degrade-not-crash treatment.
            log.warning("context.screen_context.redis_error", session_id=session_id, exc_info=True)
            return ""

    async def _get_recent_actions(self, session_id: str) -> str:
        # Judgment call #6. An empty event list is the same expected
        # common case as judgment call #5's missing snapshot.
        try:
            events = await self._event_log.get_events(session_id, limit=TIMELINE_MAX_EVENTS)
            if not events:
                return ""
            slot = self._compressor.render_timeline_slot(events, now_ms=_now_ms())
            return slot.text
        except (RedisError, ValueError, KeyError, TypeError):
            # Same exception surface as _get_screen_context: RedisError,
            # an orjson decode failure per event (EventLog.get_events),
            # or a malformed stored event dict.
            log.warning("context.recent_actions.redis_error", session_id=session_id, exc_info=True)
            return ""


__all__ = ["ContextBuilder"]
