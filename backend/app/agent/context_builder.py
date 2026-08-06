"""`ContextBuilder` — assembles the per-turn `ContextBundle`
(docs/05-agent-architecture.md §3.3; slot semantics owned by
docs/11-prompt-engineering.md §1, assembly by docs/08-context-and-events.md §5).

Phase-2 scope (plan decision #1) populated only persona, business_rules,
user_profile, conversation, and current_utterance; slots 4–7
(screen_context / recent_actions / memory_summary / knowledge) stayed at
their `""` defaults on the frozen `ContextBundle`, still rendered as empty
tag pairs by `PromptBuilder` — Phase 4/5 only start *populating* them,
never restructuring the template. Phase-4 T3 populated slots 4 and 5 for
real, from the `app/context/` pipeline T2 built (`SnapshotIngestor` /
`EventLog` / `ContextCompressor`, docs/08 §4). Phase-5 (this revision)
populates slots 6 and 7 and extends slot 3, from the memory layers
Phase-5 T2a–T2c built.

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

7. **`merchants` and `user_profiles` are both read for slot 3, and
   neither replaces the other.** This was the design question Phase 5
   had to answer, so the answer is stated here rather than inferred from
   the code:

   - `merchants` (docs/12 §3.1) is the **registration record**:
     admin-managed, KYC-backed, present from the merchant's first call,
     and not merchant-editable. `MerchantRepo`'s own docstring is the
     source of that last property.
   - `user_profiles` (docs/09 §5) is **memory**: written only by the
     post-call merge, from what the merchant *stated* out loud (Haiku
     extraction) or what a tool confirmed. It is empty on a first call
     and accumulates thereafter.

   They overlap on four fields — `UserProfileFacts` carries
   business_name / city / merchant_since / account_type, and
   `UserProfilePreferences.language` shadows `merchants
   .preferred_language`. On those, **the merchants row wins and the
   stated value is not rendered at all.** Two spellings of one fact in
   one slot is how an agent voices a contradiction, and between an
   admin-managed column and a voice→STT→LLM extraction of the same fact
   the authoritative one is not in question. (`app.domain.types
   .UserProfileFacts` already records the pair explicitly for
   `merchant_since`: the typed `DATE` lives on `merchants`, the profile
   holds "what the merchant said out loud".)

   What `user_profiles` contributes that `merchants` structurally
   cannot is **`open_issues`** — there is no such column on `merchants`,
   and docs/09 §5.1 names that field as the whole reason the layer earns
   a slot ("'Hi Rajesh — checking on your limit increase?' is a profile
   read, not a semantic search"). So slot 3 is the merchants identity
   line plus the profile's open issues, and `facts`/`preferences` are
   read (the load returns a whole row) but deliberately not rendered.

   Two consequences, stated rather than left to be discovered:

   - docs/09 §5.1 shows the profile slot rendering from `user_profiles`
     alone. It is being read here as the *memory* layer it is, layered
     over registration data that doc's example does not mention;
     docs/11 §4's rendered line is reproduced either way.
   - docs/11 §1 annotates the slot "NO balances/statuses here — those
     are tool-only", and an issue summary can carry rupee figures
     (docs/09 §5.1's canonical entry is "Daily limit increase requested:
     ₹25,000 → ₹50,000"). The two docs genuinely pull against each other
     and docs/09 owns this field, so the summary renders as stored. What
     keeps that safe is not this module: `SafetyLayer.screen_output`
     blocks any turn whose reply voices a rupee amount or reference id
     absent from that turn's tool results (docs/10 §1 invariant 1), so a
     figure the model reads here cannot be spoken without a tool call
     that returned it.

8. **Slot 6 is a per-turn Redis read; slot 7 is not a per-turn pgvector
   query.** docs/06 §4.1 spells out what the 15/40 ms `context.build`
   budget is spent on — "three pipelined Redis reads (`session:{id}`,
   `ctx:{session_id}`, rolling summary); no LLM, no network beyond
   Redis". The rolling summary is named there, so reading it here is
   inside the budget by construction. A pgvector query is not: docs/09
   §1 assigns semantic memory's read to "setup prefetch + topic-shift
   re-query", docs/02 §3.1 puts that prefetch in parallel with WS/SDP/
   ICE, and an embedding round trip plus an ANN scan on the turn path
   would blow a 40 ms p95 on its own. So `prefetch_knowledge()` runs
   retrieval once at call setup and stores the *rendered* slot at
   `ctx:{session_id}:knowledge`; `build()` reads that string.

   Honest scope limit: the topic-shift re-query docs/05 §3.7 also asks
   for is **not implemented**, because the intent classifier it gates on
   does not exist (there is no `prompts/intent.md` and nothing in
   `app/agent/` classifies intent). `prefetch_knowledge()` is the seam —
   calling it again with a fresh query is all a later batch needs — but
   today the knowledge slot is fixed for the duration of a call.

9. **An empty knowledge slot must not read as evidence.** `SemanticRepo
   .search`'s own docstring records that a truncated HNSW iterative scan
   returns short with no error and that nothing detects it, so "no rows"
   and "no relevant rows" and "retrieval never ran" are one
   indistinguishable outcome at this layer. Rendering that as
   `<knowledge></knowledge>` invites the model to treat absence as fact.
   `app.memory.slots.KNOWLEDGE_UNAVAILABLE` stands in the slot instead —
   a fixed string (so the slot stays byte-stable) that says retrieval
   produced nothing and that this is not information about the account.
   The `knowledge_prefetched` span attribute is what tells an operator
   which of the indistinguishable causes it was, since the code cannot.

10. **Memory reads degrade the slot, never the turn** — judgment call #3
    and #4's rule, extended unchanged. A Postgres failure loading the
    profile costs the open-issues tail (and, as before, the identity
    line); a Redis failure costs the summary or the knowledge slot.
    Slots 6 and 7 are both explicitly droppable under pressure (docs/08
    §5.2 rung 1 makes RAG the *first* thing to go), so degrading them is
    a correct turn, not a broken one.

The merchant row is re-read each turn, yet slot 3 must be byte-stable
within a call (docs/11 §1.1): that holds because merchants are not
self-service-editable (MerchantRepo's own docstring) — the row cannot
change mid-call. The `user_profiles` row now read beside it has the same
property for a different reason: it is written only by the post-call
merge (docs/09 §1's placement table), so no in-call write can move it.
Both are read per turn rather than once at setup, which docs/09 §1 lists
as the profile layer's read site. That deviation predates this task —
the `merchants` read has always been per-turn — and is left in place
rather than half-fixed: moving it would need per-call state this class
does not own, which is the same reason slot 7's prefetch had to land in
Redis. What this task did add is a second Postgres query per turn, on
the same session and therefore serial with the first.

## What was and was not measured against the 40 ms budget

The in-process cost of `build()` was benchmarked before and after this
change, 2,000 iterations each against `FakeRedis` and a mocked
`AsyncSession`: **p50 0.12 ms -> 0.18 ms, p95 0.16-0.24 ms -> 0.22-0.33
ms**. That is the CPU this class spends assembling — the rendering, the
token estimates, the extra `gather` members — and it is ~0.06 ms of the
15/40 ms p50/p95 budget docs/06 §4 sets.

It is emphatically **not** an end-to-end measurement, and the difference
matters: with fakes, every Redis and Postgres call returns in-process, so
the network component of the budget — the part the added reads actually
spend — is not exercised at all. There is no Docker on the development
host, so the real figure has to come from the `context.build` span in a
deployed environment. What can be said without measuring: the two new
Redis reads join the existing `asyncio.gather`, so they cost wall-clock
`max` rather than `sum` against the reads already there, and the added
Postgres query is the one genuinely serial addition — one extra round
trip on an already-checked-out connection.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import orjson
from opentelemetry.trace import Span, Status, StatusCode
from redis.exceptions import RedisError
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.context.context_compressor import TIMELINE_MAX_EVENTS, ContextCompressor
from app.context.event_log import EventLog
from app.context.redis_keys import CTX_TTL_SECONDS, _ctx_key, _ctx_knowledge_key
from app.context.token_estimate import estimate_tokens
from app.data.redis_client import RedisClient
from app.data.repositories.merchant_repo import MerchantRepo
from app.data.repositories.semantic_repo import SemanticRepo
from app.data.repositories.user_profile_repo import UserProfileRepo
from app.domain.interfaces import EmbeddingProvider
from app.domain.types import ContextBundle, Message, Session, SessionUser, UserProfile
from app.memory.semantic import SemanticMemory, prefetch_query
from app.memory.session_memory import SessionMemory
from app.memory.slots import (
    KNOWLEDGE_UNAVAILABLE,
    PROFILE_SLOT_BUDGET,
    render_knowledge_slot,
    render_open_issues,
    render_summary_slot,
)
from app.memory.summarizer import SUMMARY_TOKEN_BUDGET
from app.memory.user_profile import UserProfileMemory
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


def _decoded(value: bytes | str | None) -> str | None:
    """Narrow a `decode_responses=True` redis-py return value to `str`.

    Three lines, duplicated rather than imported from
    `app/data/redis_client.py`'s private `_as_str`, on the same reasoning
    `app/context/redis_keys.py` records for `_reject_key_delimiter`:
    reaching across a module boundary for an underscore-prefixed helper
    couples this module to that one's internals, and there is no state
    here to drift out of sync. Decoding real `bytes` rather than
    asserting them away keeps this correct against a test double that
    does not honor the flag.
    """
    return value.decode() if isinstance(value, bytes) else value


def _format_profile(merchant: Merchant) -> str:
    """docs/11 §4's compact one-line format, minus the personal name the
    schema cannot supply (judgment call #2). NO balances or statuses ever
    — docs/11 §1: those are tool-only.

    Built from `merchants` alone. The overlapping `user_profiles.facts`
    keys are deliberately not consulted here — judgment call #7 for why
    the admin-managed row wins on every field the two share.
    """
    return (
        f"Business: {merchant.business_name}, {merchant.city}. "
        f"Merchant since {merchant.merchant_since.year}. "
        f"Account type: {merchant.account_type}. "
        f"Preferred language: {merchant.preferred_language}."
    )


def _compose_profile_slot(merchant: Merchant, profile: UserProfile) -> str:
    """Slot 3 whole: the identity line, then memory's open issues.

    The issues get whatever the 200-token slot budget has left after the
    identity line, which is the right split rather than a fixed
    sub-budget: the identity line is not droppable (it is what keeps the
    agent from asking a returning merchant who they are), and docs/11 §4
    measures it at 96 tokens against the 200, so "the remainder" is
    roughly 100 tokens and shrinks only if a merchant has an unusually
    long business name.
    """
    identity = _format_profile(merchant)
    issues = render_open_issues(
        profile.open_issues, token_budget=PROFILE_SLOT_BUDGET - estimate_tokens(identity)
    )
    return f"{identity}\n{issues}" if issues else identity


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
        *,
        embeddings: EmbeddingProvider | None = None,
        settings: Settings | None = None,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._memory = session_memory
        self._redis = redis
        self._event_log = event_log
        self._compressor = compressor
        # Retrieval collaborators, needed only by `prefetch_knowledge` —
        # `build()` reads the prefetch's *output* from Redis and never
        # touches either. Optional because docs/09 §11 makes an
        # unavailable embeddings provider a supported state (drop-rung 1:
        # skip the RAG slot, no keyword fallback), and `Settings`'
        # `openai_api_key` is genuinely optional, so a worker booted
        # without it must still serve calls. `None` here is that state
        # made explicit rather than discovered as an AttributeError at
        # call setup; `prefetch_knowledge` logs and returns.
        #
        # `SemanticMemory`/`SemanticRepo` are built per prefetch rather
        # than injected whole: `SemanticRepo` takes an `AsyncSession`
        # (docs/04 §4 — one session, one transaction), so a process-long
        # instance would be a session held open for the process.
        self._embeddings = embeddings
        self._settings = settings
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
            # The verified principal, constructed here and nowhere else
            # (docs/10 §1 invariant 3's shape, the one `ToolExecutor`
            # already follows). `session.user_id` is the only identity
            # this method can see, and its provenance is a chain with no
            # request-supplied link: `POST /v1/sessions` checks
            # `body.user_id == principal.user_id` and then passes
            # `principal.user_id` — never the body field — to
            # `SessionManager.create`, which writes it to
            # `conversations.user_id`; `SessionManager.attach` reads that
            # column back into this `Session`. `SessionUser` is min_length=1
            # and frozen, so once built it cannot be widened downstream.
            #
            # `UserProfileMemory`'s own docstring is right that the type is
            # not a guarantee — nothing stops a caller building a
            # `SessionUser` from anything. What it buys is that there is
            # exactly one construction site in this class, it is this line,
            # and a test compares the whole principal the collaborators
            # received against the session's user_id.
            principal = SessionUser(user_id=session.user_id)
            try:
                # Review fix (HIGH): docs/08 §5 states the span is
                # "dominated by three pipelined Redis reads... one round
                # trip, not three" — these four reads (the three Redis
                # reads plus the independent Postgres profile read) share
                # no data dependency on each other, so awaiting them one
                # at a time was pure serialized latency with no
                # correctness reason to be sequential. `asyncio.gather`
                # runs all four concurrently, collapsing the wall-clock
                # cost from the sum of four round trips to the slowest
                # one — the effect docs/08 §5 is actually budgeting for.
                # This is concurrent dispatch, not a literal Redis
                # MULTI/pipeline batching the three `ctx:*`/session reads
                # onto one TCP round trip (the two `ctx:*` reads and the
                # session-window read live behind three separate
                # encapsulated classes — SessionMemory, RedisClient.raw,
                # EventLog — with no shared pipeline object today; that
                # would be a larger, riskier restructure than this fix's
                # scope). Each coroutine already owns its full expected
                # exception surface internally (RedisError/ValueError/
                # KeyError/TypeError, all degrade-to-empty), so
                # `asyncio.gather`'s default (no `return_exceptions`) is
                # correct here — none of the four should ever propagate,
                # and if one somehow did, failing the whole turn loudly
                # is preferable to a silent mismatch.
                #
                # Phase 5 adds two coroutines to the same gather rather
                # than two awaits after it, for the identical reason: the
                # rolling-summary read and the prefetched-knowledge read
                # have no data dependency on each other or on the four
                # above, so serializing them would be pure added latency
                # inside a 40 ms p95 budget. The Postgres profile read is
                # still ONE coroutine — it now issues two queries, but on
                # one `AsyncSession`, which is not safe to drive
                # concurrently.
                (
                    user_profile,
                    conversation,
                    screen_context,
                    recent_actions,
                    memory_summary,
                    knowledge,
                ) = await asyncio.gather(
                    self._build_user_profile(principal),
                    self._get_window(session.session_id),
                    self._get_screen_context(session.session_id),
                    self._get_recent_actions(session.session_id),
                    self._get_memory_summary(session.session_id),
                    self._get_knowledge(session.session_id, span=span),
                )
                # docs/08 §5.2: "per-slot token estimates... recorded as
                # attributes on the `context.build` span". Recorded for
                # the two slots this task added; the budget is not
                # enforced by truncation here (judgment call #8's note on
                # slot 6, `app/memory/slots.py` on slot 7).
                summary_tokens = estimate_tokens(memory_summary)
                safe_set_attribute(span, "memory_summary_tokens", summary_tokens)
                safe_set_attribute(span, "knowledge_tokens", estimate_tokens(knowledge))
                if summary_tokens > SUMMARY_TOKEN_BUDGET:
                    # Same policy as the write side (`Summarizer`'s
                    # judgment call #4): warn, never truncate — a cut here
                    # lands inside the amounts and ids docs/09 §4.2
                    # requires be preserved verbatim. Both ends warning
                    # means an over-budget summary is visible whether it
                    # was produced by this deploy's Summarizer or read
                    # back from a session an older one wrote.
                    log.warning(
                        "context.memory_summary.over_token_budget",
                        session_id=session.session_id,
                        estimated_tokens=summary_tokens,
                        budget=SUMMARY_TOKEN_BUDGET,
                    )
                return ContextBundle(
                    persona=self._persona,
                    business_rules=self._business_rules,
                    user_profile=user_profile,
                    screen_context=screen_context,
                    recent_actions=recent_actions,
                    memory_summary=memory_summary,
                    knowledge=knowledge,
                    conversation=tuple(conversation),
                    current_utterance=current_utterance,
                )
            except Exception as exc:
                span.set_status(Status(StatusCode.ERROR, description=type(exc).__name__))
                raise

    async def _build_user_profile(self, principal: SessionUser) -> str:
        """Slot 3: the merchants identity line, then the profile's open
        issues (judgment call #7 for why both, and which wins where).

        Both reads share one `AsyncSession` and therefore one pooled
        connection and one transaction — sequential, because an
        `AsyncSession` cannot be driven concurrently. Two round trips
        instead of one is the cost this task added to `context.build`.
        """
        merchant_id = principal.user_id
        # _format_profile inside the try (review MEDIUM): today every
        # column it reads is NOT NULL so it can't raise, but the module's
        # stated policy is degrade-the-slot-never-the-turn — a future
        # nullable column must hit the same fallback, not crash the turn.
        try:
            async with self._sessionmaker() as db:
                merchant = await MerchantRepo(db).get(merchant_id)
                profile = await UserProfileMemory(UserProfileRepo(db)).load(principal)
            if merchant is None:
                log.warning("context.user_profile.merchant_missing", merchant_id=merchant_id)
                return _PROFILE_UNAVAILABLE
            return _compose_profile_slot(merchant, profile)
        except (SQLAlchemyError, AttributeError, TypeError, ValueError):
            # Judgment call #3: degrade the slot, never the turn (docs/05 §3.3).
            # Unchanged tuple, wider blast radius: it now also covers the
            # `user_profiles` read. `UserProfileMemory.load` launders a
            # corrupt row per key/per entry rather than raising, and
            # `_validate_issues` trims past the 20-entry cap so the
            # whole-row construction cannot raise on it either — so a
            # corrupt row is not what is expected to arrive here. One
            # shared failure mode is still right: both rows come from the
            # same connection, so in practice they fail together.
            log.warning("context.user_profile.db_error", merchant_id=merchant_id, exc_info=True)
            return _PROFILE_UNAVAILABLE

    async def _get_memory_summary(self, session_id: str) -> str:
        """Slot 6: the rolling summary from the `session:{id}` hash.

        No summary is the expected case for turns 1–8 (docs/09 §4.1 rule
        1: while `turn_count <= 8` no summary exists), so `None` degrades
        silently to `""` — the same expected-not-failed treatment
        judgment calls #5/#6 give a missing snapshot or an empty event
        list, and exactly what docs/11 §4's worked turn-1 example renders
        (`<memory_summary></memory_summary>`).
        """
        try:
            return render_summary_slot(await self._memory.get_summary(session_id))
        except (RedisError, ValueError):
            # Same surface as `_get_window`: connection failure, an orjson
            # decode error, or pydantic's ValidationError (a subclass of
            # ValueError) on a malformed stored summary.
            log.warning("context.memory_summary.redis_error", session_id=session_id, exc_info=True)
            return ""

    async def _get_knowledge(self, session_id: str, *, span: Span) -> str:
        """Slot 7: the rendered slot `prefetch_knowledge` stored, or the
        stands-for-nothing line (judgment call #9).

        Every failure and every absence converge on the same string,
        because at this layer they are the same outcome — see judgment
        call #9. The distinction operators need is on the span
        (`knowledge_prefetched`) and in the log, not in the prompt.

        The span is passed explicitly rather than read from the ambient
        context: this coroutine runs inside an `asyncio.gather`, i.e. in
        a Task with a *copy* of the context, and depending on that copy
        carrying the right span is a subtlety no reader should have to
        verify.
        """
        prefetched: str | None = None
        try:
            prefetched = _decoded(await self._redis.raw.get(_ctx_knowledge_key(session_id)))
        except (RedisError, ValueError):
            log.warning("context.knowledge.redis_error", session_id=session_id, exc_info=True)
        safe_set_attribute(span, "knowledge_prefetched", bool(prefetched))
        return prefetched if prefetched else KNOWLEDGE_UNAVAILABLE

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

    # ------------------------------------------------------------------
    # Call-setup retrieval prefetch (docs/02 §3.1, docs/09 §6.2)
    # ------------------------------------------------------------------

    async def prefetch_knowledge(self, session: Session) -> None:
        """Run retrieval once for this call and store the rendered slot 7
        text at `ctx:{session_id}:knowledge` (judgment call #8).

        Called at call setup, in the dead time docs/02 §3.1 shows the
        prefetch occupying — while the WS connects and ICE/DTLS
        negotiates. Never called per turn.

        The query is docs/09 §6.2's setup form, "the active error code +
        screen name", read off the `ctx:{session_id}` snapshot that
        `POST /v1/sessions` already stored. Reading it here rather than
        taking it as an argument keeps one definition of where those two
        values live; `prefetch_query` drops an absent half and
        `SemanticMemory.retrieve` refuses to embed an empty query, so a
        call opened from a screen with no error and no snapshot spends no
        network hop.

        **Never raises.** docs/08 §7's rule — "the pipeline degrades
        context, never conversation" — applies with full force here: this
        runs on the path a merchant is waiting on to start a call, it
        makes a third-party network call, and the slot it fills is
        drop-rung 1, the first thing docs/08 §5.2 sheds under pressure.
        Every failure leaves the key unwritten, which `_get_knowledge`
        already renders as judgment call #9's stands-for-nothing line.
        `except Exception` for the same reason
        `_ingest_initial_screen_context` uses it (app/api/routes/
        sessions.py's 2026-08-04 audit fix): the exception type is
        genuinely irrelevant when no failure of this stage may affect the
        call. `exc_info=True` keeps the traceback for operators.
        """
        if self._embeddings is None or self._settings is None:
            # docs/09 §11's embedding-outage row, reached at wiring time
            # rather than at request time. INFO, not WARNING: a deploy
            # without an embeddings key is a supported configuration.
            log.info("context.knowledge.prefetch_skipped", session_id=session.session_id)
            return
        try:
            query = prefetch_query(*await self._prefetch_query_parts(session.session_id))
            if not query:
                log.info("context.knowledge.prefetch_empty_query", session_id=session.session_id)
                return
            principal = SessionUser(user_id=session.user_id)
            async with self._sessionmaker() as db:
                memory = SemanticMemory(self._embeddings, SemanticRepo(db, self._settings))
                # `k` and the 0.70 floor are `SemanticMemory.retrieve`'s
                # policy (docs/09 §6.2) and are deliberately not passed:
                # re-stating them here would be a second place they could
                # drift from the doc.
                results = await memory.retrieve(query, principal)
            await self._redis.raw.set(
                _ctx_knowledge_key(session.session_id),
                render_knowledge_slot(results),
                ex=CTX_TTL_SECONDS,
            )
            log.info(
                "context.knowledge.prefetched",
                session_id=session.session_id,
                result_count=len(results),
            )
        except Exception:
            log.warning(
                "context.knowledge.prefetch_failed", session_id=session.session_id, exc_info=True
            )

    async def _prefetch_query_parts(self, session_id: str) -> tuple[str | None, str | None]:
        """`(error_code, screen_name)` off the stored snapshot, or
        `(None, None)`.

        A missing or unreadable snapshot is not an error here for the
        same reason it is not in `_get_screen_context` (judgment call
        #5): a call can genuinely reach setup with nothing captured. It
        degrades to an empty query, which `prefetch_knowledge` turns into
        "no retrieval this call" rather than an embedding round trip on
        the empty string.
        """
        try:
            raw = await self._redis.raw.get(_ctx_key(session_id))
            if raw is None:
                return (None, None)
            stored = orjson.loads(raw)
            last_api = stored.get("last_api") or {}
            return (last_api.get("error_code"), stored.get("screen"))
        except (RedisError, ValueError, KeyError, TypeError, AttributeError):
            log.warning(
                "context.knowledge.prefetch_query_unavailable",
                session_id=session_id,
                exc_info=True,
            )
            return (None, None)


__all__ = ["ContextBuilder"]
