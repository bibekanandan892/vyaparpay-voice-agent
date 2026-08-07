"""voice-worker entrypoint — `python -m app.voice.run`, exactly as
docker-compose.yml's voice-worker service invokes it (docs/04 §1's second
entrypoint): config, logging/tracing init, the `/v1/signal` signaling
server on the configured port, graceful shutdown on SIGTERM — and, since
T5.1, the REAL per-call wiring: every accepted call gets a `CallSession`
(app/voice/call_session.py) running `VoiceAgentWorker` over DeepgramStt /
ElevenLabsTts / SileroVad and a per-call `ConversationManager` brain.
Since Phase-4 T4, every call also gets a `ContextDispatcher`
(app/voice/context_dispatch.py) over a process-long `SnapshotIngestor`/
`EventLog` pair built here from the same `redis` client — the
composition-root pattern this module already uses for every other
process-long collaborator (`stack`, `deps_of`).

Judgment calls, flagged per house style:

1. **Voice providers are constructed lazily, on the first call, never at
   startup.** config.py's optional-key design (its own module docstring:
   "the voice worker fail-fasts on the subset it needs") meets the test
   requirement that this process boots with no voice env at all: startup
   only needs the Phase-2 required settings, and a call arriving without
   DEEPGRAM/ELEVENLABS keys fails that call's peer setup loudly
   (signaling answers `INTERNAL`, the provider constructors' own
   fail-fast messages land in the log) while the process keeps serving.
   The built `CallDeps` is cached — the providers are process-long by
   their own contracts; only SileroVad is per-call (call_session
   judgment call 2).
2. **The brain factory attaches, then builds.** Per call it awaits
   `SessionManager.attach(session_id)` — the session row was created by
   agent-api's `POST /v1/sessions`, so attach is a load-and-return (the
   Phase-2 stub is sufficient: the worker needs the `Session` domain
   object for `ConversationManager`, and no state transition is required
   for the turn machine to run) — then builds a per-call
   `ConversationManager` + `CostTracker` over the process-long
   collaborator stack (mirroring scripts/demo_cli.py's composition, the
   one other real composition root).
3. **`PlaceholderCallSession` survives as a deps-less shim.**
   tests/voice/test_run.py (frozen: existing tests are not modified in
   T5.1) pins its constructor shape and lazy-egress contract; the class
   is now one `super().__init__(..., deps=None)` over `CallSession`, so
   the pinned behavior keeps covering the real shared wiring. `main()`
   no longer constructs it. Flagged for deletion whenever that test
   file is allowed to move to `CallSession` directly.
4. **The worker's own ICE config is STUN-only via `COTURN_HOST`** (srflx,
   docs/06 §2's candidate table), empty → host-only. The worker never
   allocates TURN relay for itself: the client holds the HMAC relay
   credentials (docs/13 §7) and one relayed side is sufficient for the
   pair to connect.
5. **SIGTERM/SIGINT via loop signal handlers**, with a `signal.signal`
   fallback where the loop API is unavailable (Windows dev hosts); the
   container path (compose → Linux) always takes the loop route.
   Shutdown order: stop accepting, `close_all()` (bye + peer teardown
   per session), then close http/engine/redis.
6. **Per-turn spans are the worker's** (docs/04 §7.2's Phase-3 rows —
   `turn` with `endpoint_ms`, `stt.final`); this module only initializes
   the provider via `setup_observability`, as before.
"""

from __future__ import annotations

import asyncio
import signal
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import httpx
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from websockets.asyncio.server import serve

import app.tools  # noqa: F401 -- import side effect: registers the Phase-2 tools
from app.agent.context_builder import ContextBuilder
from app.agent.conversation_manager import ConversationManager
from app.agent.cost_tracker import CostTracker
from app.agent.llm_router import LLMRouter
from app.agent.prompt_builder import PromptBuilder
from app.agent.safety_layer import SafetyLayer
from app.agent.session_manager import SessionManager, SummaryPipelineDeps
from app.agent.tool_executor import ToolExecutor
from app.config import Settings, get_settings
from app.context.context_compressor import ContextCompressor
from app.context.event_log import EventLog
from app.context.snapshot_ingestor import SnapshotIngestor
from app.data.engine import create_engine_and_sessionmaker
from app.data.redis_client import SESSION_CONTROL_END, RedisClient
from app.domain.types import EndReason
from app.domain.voice import IceServer
from app.memory.conversation_summary_store import ConversationSummaryStore
from app.memory.session_memory import SessionMemory
from app.memory.summarizer import Summarizer
from app.obs.logging import configure_logging, get_logger
from app.obs.tracing import setup_observability
from app.providers.deepgram import DeepgramStt
from app.providers.deepgram_tts import DeepgramTts
from app.providers.elevenlabs import ElevenLabsTts
from app.providers.openai_embeddings import OpenAIEmbeddings
from app.providers.openrouter import OpenRouterLLM
from app.tools.registry import configure as configure_tools
from app.tools.registry import registry
from app.voice.call_session import CallDeps, CallSession
from app.voice.peer_session import SendSignal
from app.voice.signaling import SIGNALING_PATH, SignalingServer
from app.voice.silero import SileroVad
from app.voice.worker import BuiltBrain

log = get_logger(__name__)

# Bound on the call-setup retrieval prefetch (see `_make_brain_factory`).
# Chosen here against docs/02 §3.1's ≤1.5 s p50 call-setup budget, which
# the prefetch runs concurrently with; no doc fixes this number.
KNOWLEDGE_PREFETCH_TIMEOUT_S = 1.0

# post-call-pipeline-wiring HIGH fix: bounded retry for
# `SessionManager.end()` specifically — NOT `finalize()` — when it fails
# after `finalize()` has already committed. `end()` is cheap and safe to
# retry immediately (docs/05 §3.1: a second `end()` on an already-ended
# session no-ops; the finalize-before-end check it re-runs is a no-op too,
# since the `call_costs` row this call just wrote is still there). Total
# worst-case added delay (0.5s + 1.0s = 1.5s of backoff across 3 attempts)
# stays comfortably inside `CallSession._CLOSE_TIMEOUT_S` (5.0s) so a
# retry in flight is not itself cancelled mid-attempt by the bounded
# teardown wait it's running inside of. This does not cover a genuinely
# down database for minutes — that needs a durable reconciliation sweep
# (a call_costs row with no matching ended conversation, retried outside
# any live process), which is out of scope here: it needs its own
# composition-root decisions (which process runs it, on what schedule)
# this wiring task does not own. What this DOES guarantee is that an
# ordinary transient blip does not permanently wedge the session, and
# that an unrecovered failure is loud and distinguishable from routine
# worker-crash noise (see `_end_with_retry`'s log event below).
_END_RETRY_ATTEMPTS = 3
_END_RETRY_BACKOFF_S = 0.5


class PlaceholderCallSession(CallSession):
    """T3.1's transport-only per-call wiring, now a deps-less
    `CallSession` (judgment call 3): media is terminated honestly, the
    fan-outs are drained by no-ops, and the downlink speaks paced
    silence. Kept solely because tests/voice/test_run.py pins this
    name and constructor shape; `main()` wires `CallSession` with real
    `CallDeps` instead."""

    def __init__(
        self, session_id: str, send_signal: SendSignal, *, ice_servers: tuple[IceServer, ...]
    ) -> None:
        super().__init__(session_id, send_signal, ice_servers=ice_servers, deps=None)


@dataclass(frozen=True)
class _BrainStack:
    """The process-long brain collaborators (judgment call 2) —
    everything per-call construction composes from."""

    session_manager: SessionManager
    session_memory: SessionMemory
    context_builder: ContextBuilder
    prompt_builder: PromptBuilder
    tool_executor: ToolExecutor
    safety_layer: SafetyLayer
    llm_router: LLMRouter


def _build_summary_pipeline(
    settings: Settings,
    sessionmaker: async_sessionmaker[AsyncSession],
    redis: RedisClient,
    session_memory: SessionMemory,
    llm_router: LLMRouter,
    embeddings: OpenAIEmbeddings | None,
) -> SummaryPipelineDeps:
    """post-call-summary-profile-wiring task: `SessionManager.end()`'s
    summary/embed stage (that class's judgment call #10) needs an
    `LLMRouter` (to fold the final summary) and a `ConversationSummaryStore`
    (to save + embed it) — collaborators no earlier task built a
    `SessionManager` with, since nothing called `end()`'s new stage before
    this task wired it.

    `summarizer_factory` builds a FRESH `Summarizer` per call —
    `SessionManager`'s own judgment call #12, updated by the
    summarizer-turn-loop-wiring task: `_make_brain_factory` below now DOES
    construct a real per-call `Summarizer` and thread it through to
    `end()` as `real_summarizer`, so in the one real production call
    path this factory is a fallback that is not actually reached
    (`_save_summary` prefers `real_summarizer` when one is supplied).
    It stays because `SessionManagerProto`'s `end()` is still callable
    with none — a future caller that does not have a live per-call
    instance to offer, and the existing `tests/agent/test_session_manager
    .py` suite that exercises this exact fallback deliberately, on its
    own terms. Its `cost_tracker` is a throwaway `CostTracker`, never
    `finalize()`d: it would only matter if this factory's `Summarizer`
    were ever actually folded through, and it deliberately is not, on
    the one path that reaches it today.

    `conversation_summary_store` embeds through the SAME `embeddings`
    instance `ContextBuilder`'s retrieval prefetch already uses above —
    `None` when `OPENAI_API_KEY` is unset, which
    `ConversationSummaryStore.__init__` accepts as "no embed, ever" (that
    class's judgment call #3), the identical drop-rung-1 posture the
    prefetch already has.
    """
    conversation_summary_store = ConversationSummaryStore(
        sessionmaker, embeddings=embeddings, settings=settings if embeddings else None
    )

    def summarizer_factory() -> Summarizer:
        return Summarizer(
            # Same documented Protocol wart as llm_router's own
            # construction below — LLMRouterProto vs. the real class.
            llm_router,  # type: ignore[arg-type]
            session_memory,
            cost_tracker=CostTracker(settings, session_factory=sessionmaker, redis=redis),
        )

    return SummaryPipelineDeps(
        summarizer_factory=summarizer_factory,
        conversation_summary_store=conversation_summary_store,
    )


def _build_brain_stack(
    settings: Settings,
    http: httpx.AsyncClient,
    sessionmaker: async_sessionmaker[AsyncSession],
    redis: RedisClient,
) -> _BrainStack:
    """Mirrors scripts/demo_cli.py's `_build_collaborators` — the same
    singleton stack, wired for the voice worker process."""
    llm_provider = OpenRouterLLM(http, settings)
    configure_tools(sessionmaker)
    session_memory = SessionMemory(redis)
    # Phase-4 T3: ContextBuilder's slot-4/5 deps — EventLog wraps the same
    # `redis` singleton, ContextCompressor is stateless (its own docstring:
    # "one instance is safely shared across sessions/requests").
    #
    # Phase-5: `embeddings`/`settings` are the call-setup retrieval
    # prefetch's collaborators (slot 7). `OpenAIEmbeddings` takes the same
    # pooled `http` client `OpenRouterLLM` above does and is process-long
    # by its own contract. It is constructed only when a key is
    # configured: `Settings.openai_api_key` is optional so this worker
    # boots without one (config.py's stated design), and passing `None`
    # here is how ContextBuilder is told retrieval is unavailable — the
    # supported drop-rung-1 state of docs/09 §11, not a silent failure.
    # Slot 6 needs no new dependency: the rolling summary is a field of
    # the `session_memory` already wired above.
    embeddings = OpenAIEmbeddings(http, settings) if settings.openai_api_key else None
    context_builder = ContextBuilder(
        sessionmaker,
        session_memory,
        redis,
        EventLog(redis),
        ContextCompressor(),
        embeddings=embeddings,
        settings=settings,
    )
    safety_layer = SafetyLayer(registry)
    tool_executor = ToolExecutor(
        registry=registry, safety=safety_layer, redis=redis, sessionmaker=sessionmaker
    )
    # The documented interfaces.py Protocol wart (async-generator vs
    # coroutine spelling) — same ignore as demo_cli's construction.
    llm_router = LLMRouter(llm_provider, settings)  # type: ignore[arg-type]
    summary_pipeline = _build_summary_pipeline(
        settings, sessionmaker, redis, session_memory, llm_router, embeddings
    )
    session_manager = SessionManager(sessionmaker, redis, summary_pipeline=summary_pipeline)
    return _BrainStack(
        session_manager=session_manager,
        session_memory=session_memory,
        context_builder=context_builder,
        prompt_builder=PromptBuilder(),
        tool_executor=tool_executor,
        safety_layer=safety_layer,
        llm_router=llm_router,
    )


async def _end_with_retry(
    session_manager: SessionManager,
    session_id: str,
    reason: EndReason,
    *,
    real_summarizer: Summarizer | None = None,
) -> None:
    """post-call-pipeline-wiring HIGH fix: `SessionManager.end()` failing
    AFTER `CostTracker.finalize()` already committed leaves `call_costs`
    populated but `conversations.state` never flips to `ended` — the
    session is stuck reporting `in_call` forever (`GET
    /v1/sessions/{id}/summary` 404s forever, and the phantom `in_call`
    state also reopens the reconnect-token gate in `sessions.py`'s
    `/token` endpoint for a session whose finalize+end never actually
    finished). A transient DB blip is the common cause and is cheap and
    safe to retry immediately (module docstring, `_END_RETRY_ATTEMPTS`);
    an unrecovered failure after exhausting retries is logged as its own
    ERROR-level event — distinct from a generic worker-crash log — so it
    is greppable/alertable rather than indistinguishable from routine
    shutdown noise, before re-raising so the existing task-failure
    visibility (`CallSession._log_worker_crash`) still sees it too.

    `real_summarizer` (summarizer-turn-loop-wiring task) is threaded
    straight through to every retry attempt of `end()` unchanged — it is
    the same real per-call `Summarizer` on every attempt, never
    reconstructed, since a retry here is recovering from a transient
    Postgres blip, not from anything about the summarizer itself
    (session_manager.py judgment call #12)."""
    for attempt in range(1, _END_RETRY_ATTEMPTS + 1):
        try:
            await session_manager.end(session_id, reason, real_summarizer=real_summarizer)
            return
        except Exception:
            if attempt >= _END_RETRY_ATTEMPTS:
                log.error(
                    "voice_worker.session_end_failed_after_finalize",
                    session_id=session_id,
                    reason=reason.value,
                    attempts=attempt,
                    exc_info=True,
                )
                raise
            log.warning(
                "voice_worker.session_end_retry",
                session_id=session_id,
                reason=reason.value,
                attempt=attempt,
                exc_info=True,
            )
            await asyncio.sleep(_END_RETRY_BACKOFF_S * attempt)


def _make_brain_factory(
    settings: Settings,
    sessionmaker: async_sessionmaker[AsyncSession],
    redis: RedisClient,
    stack: _BrainStack,
) -> Callable[[str], Awaitable[BuiltBrain]]:
    """Judgment call 2: attach, then build the per-call brain — and, since
    the post-call-pipeline-wiring task, this call's finalize+end hook
    bundled with it as a `BuiltBrain` (worker.py). Both are built here,
    together, because both close over the SAME per-call `CostTracker`
    constructed a few lines below: `CostTracker.finalize()` reads that
    one instance's in-memory accumulators, not a fresh lookup keyed by
    `session_id` (cost_tracker.py has no such lookup), so the closure
    that finalizes it must be minted in the same place and carried down
    to `VoiceAgentWorker` alongside the `ConversationManager` it
    also feeds.

    Since the summarizer-turn-loop-wiring task, the same reasoning
    extends to a per-call `Summarizer`: built here, sharing this SAME
    `cost_tracker` (so a fold's ~$0.0023, docs/09 §4.1, lands in the one
    `CostTracker` instance `finalize()` will actually read — this class's
    own judgment call #5 argues for it directly), and handed to
    `ConversationManager` as its `summarizer` constructor argument
    (that class's judgment call #13) so the turn loop's
    `observe_turn()`/`kick()` calls buffer into and fold through the
    real thing, not a throwaway. `on_call_ended` closes over it too, for
    the orphaned-fold fix below."""

    async def build(session_id: str) -> BuiltBrain:
        session = await stack.session_manager.attach(session_id)
        # Judgment call 7 (Phase-5): the call-setup retrieval prefetch
        # (docs/02 §3.1) runs here, once, between attach and the first
        # turn. This is the dead time that diagram puts it in — the brain
        # is built while the client is still negotiating ICE/DTLS, and
        # `VoiceAgentWorker` does not run a turn until media flows.
        #
        # Awaited, not `create_task`d, and bounded: awaiting keeps the
        # ordering deterministic (either the slot is ready before turn 1
        # or it demonstrably was not) and avoids a detached task whose
        # failure would surface nowhere, while the bound keeps a slow
        # embeddings vendor from eating the ≤1.5 s p50 setup budget
        # docs/02 §3.1 owns. The 1 s figure is a choice made here against
        # that budget, not a number any doc fixes.
        #
        # `prefetch_knowledge` never raises (its own docstring), so the
        # only thing that can escape is the timeout, and on timeout the
        # call proceeds with an empty knowledge slot — drop-rung 1,
        # docs/08 §5.2. What is NOT claimed: that the slot is populated
        # before the first word. A timeout, a missing key, or a merchant
        # whose screen carried no error all leave it empty, and
        # `ContextBuilder` renders that honestly rather than as absence
        # of fact.
        try:
            await asyncio.wait_for(
                stack.context_builder.prefetch_knowledge(session),
                timeout=KNOWLEDGE_PREFETCH_TIMEOUT_S,
            )
        except TimeoutError:
            log.warning("voice_worker.knowledge_prefetch_timeout", session_id=session_id)
        cost_tracker = CostTracker(settings, session_factory=sessionmaker, redis=redis)
        # summarizer-turn-loop-wiring task: same `cost_tracker` instance
        # `conversation_manager` below is holding (this function's own
        # docstring). Built with the DEFAULT `task_factory`
        # (`asyncio.create_task`) deliberately — `Summarizer`'s own class
        # docstring documents that passing the call's `TaskGroup.
        # create_task` instead would need one to already exist, and
        # `VoiceAgentWorker.run()` builds this whole brain via
        # `_brain_factory()` BEFORE it opens `async with
        # asyncio.TaskGroup() as tg:` (worker.py) — there is no group to
        # pass yet. `on_call_ended`'s `summarizer.drain()` below is the
        # other documented fix for the same hazard, and it has no such
        # ordering conflict.
        summarizer = Summarizer(
            stack.llm_router,  # type: ignore[arg-type]
            stack.session_memory,
            cost_tracker=cost_tracker,
        )
        conversation_manager = ConversationManager(
            session=session,
            context_builder=stack.context_builder,
            prompt_builder=stack.prompt_builder,
            # Same documented Protocol wart as _build_brain_stack.
            llm_router=stack.llm_router,  # type: ignore[arg-type]
            tool_executor=stack.tool_executor,
            safety_layer=stack.safety_layer,
            cost_tracker=cost_tracker,
            summarizer=summarizer,
            session_memory=stack.session_memory,
            tool_registry=registry,
        )

        async def on_call_ended(reason: EndReason) -> None:
            """The finalize-before-end pipeline (docs/09 §8; docs/05 §3.1's
            `SessionManager.end()` raises if `finalize()` has not already
            written a `call_costs` row) — run exactly once, from
            `VoiceAgentWorker.run()`'s `finally` (worker.py), for every
            hangup path. Closes over `cost_tracker`, `summarizer`, and
            `session_id` above and `stack.session_manager` (process-long),
            which is why this closure — not a fresh SessionManager/
            CostTracker/Summarizer lookup — is what travels to the worker.

            `summarizer.drain()` runs FIRST, before `finalize()` —
            the orphaned-fold fix `Summarizer`'s own class docstring
            asks for: the turn loop's last `kick()` may still have a
            fold running in the background (default `task_factory`,
            see above) when the call ends, and that fold calls
            `cost_tracker.record_turn()` (via `_consume`) on this SAME
            `cost_tracker` when it completes. Draining BEFORE
            `finalize()` guarantees that call already happened, so the
            fold's ~$0.0023 (docs/09 §4.1) is in the running total
            `finalize()` upserts into `call_costs` — never orphaned onto
            a call whose cost row already committed. `drain()` never
            raises (its own docstring) and is a no-op when nothing is in
            flight, so this costs nothing on the (overwhelmingly common)
            call that never fires a fold at hang-up.

            `end()` gets a bounded retry (`_end_with_retry`, HIGH fix)
            because it is the one that can leave a session permanently
            wedged as `in_call` if it fails after `finalize()` already
            committed — `finalize()` itself is NOT retried here: a
            partial failure part-way through its own turn-record loop
            would double-append already-flushed Redis records on retry
            (cost_tracker.py's `_turn_records_flushed` flag only guards
            against re-entry AFTER a clean completion), which is a
            different, deeper problem this task does not own fixing.
            `real_summarizer=summarizer` (session_manager.py judgment
            call #12) is how `end()`'s final `fold_pending()` reaches
            this call's actually-buffered tail turns instead of a fresh,
            empty instance — by the time this runs, `drain()` above has
            already let any in-flight fold shed what it covered, so the
            buffer holds exactly the turns still owed a fold."""
            await summarizer.drain()
            await cost_tracker.finalize(session_id)
            await _end_with_retry(
                stack.session_manager, session_id, reason, real_summarizer=summarizer
            )

        return BuiltBrain(brain=conversation_manager, on_call_ended=on_call_ended)

    return build


def _lazy_call_deps(
    settings: Settings, brain_factory: Callable[[str], Awaitable[BuiltBrain]]
) -> Callable[[], CallDeps]:
    """Judgment call 1: providers are built on the first call and cached
    for the process; a missing voice key fails THAT call loudly while
    the worker keeps serving."""
    cache: list[CallDeps] = []

    def build() -> CallDeps:
        if not cache:
            # TTS provider chosen by config; Settings' Literal already
            # rejected any value other than these two.
            tts = (
                DeepgramTts(settings)
                if settings.tts_provider == "deepgram"
                else ElevenLabsTts(settings)
            )
            cache.append(
                CallDeps(
                    settings=settings,
                    # Both ignores are the documented interfaces.py/voice.py
                    # Protocol wart (`async def ... -> AsyncIterator` reads
                    # as coroutine-returning while the real implementations
                    # are async generators) — the same convention as
                    # demo_cli's LLMRouter construction; the worker handles
                    # both shapes at runtime (speech/stt_supervisor).
                    stt=DeepgramStt(settings),  # type: ignore[arg-type]
                    tts=tts,  # type: ignore[arg-type]
                    vad_factory=SileroVad,
                    brain_factory=brain_factory,
                )
            )
        return cache[0]

    return build


def _worker_ice_servers(settings: Settings) -> tuple[IceServer, ...]:
    """Judgment call 4: coturn as STUN for srflx, or host-only."""
    if not settings.coturn_host:
        return ()
    return (IceServer(urls=(f"stun:{settings.coturn_host}:3478",)),)


def _install_signal_handlers(stop: asyncio.Event) -> None:
    """Judgment call 5: graceful-shutdown trigger for SIGTERM (compose
    stop) and SIGINT (local ctrl-C)."""
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signum, stop.set)
        except NotImplementedError:
            # Windows dev host: the loop API is unsupported; a plain
            # handler still flips the event on the next loop wakeup.
            signal.signal(signum, lambda *_: stop.set())


async def _run_session_control_subscriber(redis: RedisClient, server: SignalingServer) -> None:
    """Judgment call 6 (post-call-pipeline-wiring task): the missing
    subscriber for `session_control:{id}` (docs/13 §2.2's operator
    `DELETE /v1/sessions/{id}`). `sessions.py`'s own judgment call 1
    explains why agent-api cannot call `SessionManager.end()` directly —
    the finalize-before-end invariant needs THIS process's per-call
    `CostTracker` — and publishes `"end"` into the void instead. This
    loop is the other half: it reduces the operator path to exactly the
    same `close_one()` -> `peer.close()` -> ingress ends ->
    `VoiceAgentWorker.run()`'s `finally` -> finalize+end pipeline every
    other hangup already takes, rather than being a second, divergent
    close path.

    One message handled at a time, each in its own try/except: a single
    malformed or failing message must not take the whole subscriber (and
    therefore every future operator hang-up on this worker) down with
    it — the loop logs and keeps listening."""
    async for session_id, message in redis.subscribe_session_control():
        try:
            if message != SESSION_CONTROL_END:
                log.debug(
                    "voice_worker.session_control_ignored",
                    session_id=session_id,
                    message=message,
                )
                continue
            log.info("voice_worker.session_control_end_received", session_id=session_id)
            closed = await server.close_one(session_id, reason="operator_hangup")
            if not closed:
                # Not an error: pub/sub is lossy and a repeat DELETE (or one
                # that lands after the call already ended some other way,
                # docs/13 §2.2) is expected, not exceptional.
                log.info(
                    "voice_worker.session_control_no_active_call", session_id=session_id
                )
        except Exception:
            log.exception("voice_worker.session_control_handler_failed", session_id=session_id)


async def main() -> None:
    settings = get_settings()
    configure_logging(settings)
    setup_observability(settings)
    redis = RedisClient.from_settings(settings)
    engine, sessionmaker = create_engine_and_sessionmaker(settings)
    http = httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=2.0))
    ice_servers = _worker_ice_servers(settings)
    stack = _build_brain_stack(settings, http, sessionmaker, redis)
    brain_factory = _make_brain_factory(settings, sessionmaker, redis, stack)
    deps_of = _lazy_call_deps(settings, brain_factory)
    # Phase-4 T4: process-long, stateless-per-call collaborators (same
    # sharing rationale as `redis`/`stack` above) — `SnapshotIngestor`
    # keys everything off the `session_id` argument, `ContextCompressor`
    # is documented stateless, `EventLog` wraps `redis` the same way
    # `SessionMemory` does. `call_session.py`'s `ContextDispatcher` wiring
    # (judgment call 6) is independent of `deps_of()` above.
    snapshot_ingestor = SnapshotIngestor(redis, ContextCompressor())
    event_log = EventLog(redis)

    def peer_factory(session_id: str, send_signal: SendSignal) -> CallSession:
        return CallSession(
            session_id,
            send_signal,
            ice_servers=ice_servers,
            deps=deps_of(),
            snapshot_ingestor=snapshot_ingestor,
            event_log=event_log,
        )

    server = SignalingServer(redis=redis, peer_factory=peer_factory)
    stop = asyncio.Event()
    _install_signal_handlers(stop)
    # Judgment call 6: one process-long subscriber, started alongside the
    # signal handlers above — same "process-long collaborator" footing as
    # `stack`/`snapshot_ingestor`. A crash here (as opposed to a graceful
    # cancel at shutdown) is a real alarm: it means operator DELETEs stop
    # reaching live calls on this worker until restart.
    subscriber_task = asyncio.create_task(
        _run_session_control_subscriber(redis, server), name="session-control-subscriber"
    )

    def _log_subscriber_crash(task: asyncio.Task[None]) -> None:
        if task.cancelled() or task.exception() is None:
            return
        log.error(
            "voice_worker.session_control_subscriber_crashed", error=repr(task.exception())
        )

    subscriber_task.add_done_callback(_log_subscriber_crash)
    try:
        async with serve(
            server.handle_websocket,
            settings.signaling_bind_host,
            settings.signaling_bind_port,
        ):
            log.info(
                "voice_worker.listening",
                host=settings.signaling_bind_host,
                port=settings.signaling_bind_port,
                path=SIGNALING_PATH,
            )
            await stop.wait()
            log.info("voice_worker.shutdown_started")
            subscriber_task.cancel()
            await asyncio.gather(subscriber_task, return_exceptions=True)
            await server.close_all(reason="agent_hangup")
    finally:
        await http.aclose()
        await engine.dispose()
        await redis.close()
    log.info("voice_worker.stopped")


if __name__ == "__main__":
    asyncio.run(main())
