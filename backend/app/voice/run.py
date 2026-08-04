"""voice-worker entrypoint — `python -m app.voice.run`, exactly as
docker-compose.yml's voice-worker service invokes it (docs/04 §1's second
entrypoint): config, logging/tracing init, the `/v1/signal` signaling
server on the configured port, graceful shutdown on SIGTERM — and, since
T5.1, the REAL per-call wiring: every accepted call gets a `CallSession`
(app/voice/call_session.py) running `VoiceAgentWorker` over DeepgramStt /
ElevenLabsTts / SileroVad and a per-call `ConversationManager` brain.

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
from app.agent.session_manager import SessionManager
from app.agent.tool_executor import ToolExecutor
from app.config import Settings, get_settings
from app.context.context_compressor import ContextCompressor
from app.context.event_log import EventLog
from app.data.engine import create_engine_and_sessionmaker
from app.data.redis_client import RedisClient
from app.domain.voice import IceServer
from app.memory.session_memory import SessionMemory
from app.obs.logging import configure_logging, get_logger
from app.obs.tracing import setup_observability
from app.providers.deepgram import DeepgramStt
from app.providers.elevenlabs import ElevenLabsTts
from app.providers.openrouter import OpenRouterLLM
from app.tools.registry import configure as configure_tools
from app.tools.registry import registry
from app.voice.call_session import CallDeps, CallSession
from app.voice.peer_session import SendSignal
from app.voice.signaling import SIGNALING_PATH, SignalingServer
from app.voice.silero import SileroVad
from app.voice.worker import Brain

log = get_logger(__name__)


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
    session_manager = SessionManager(sessionmaker, redis)
    session_memory = SessionMemory(redis)
    # Phase-4 T3: ContextBuilder's slot-4/5 deps — EventLog wraps the same
    # `redis` singleton, ContextCompressor is stateless (its own docstring:
    # "one instance is safely shared across sessions/requests").
    context_builder = ContextBuilder(
        sessionmaker, session_memory, redis, EventLog(redis), ContextCompressor()
    )
    safety_layer = SafetyLayer(registry)
    tool_executor = ToolExecutor(
        registry=registry, safety=safety_layer, redis=redis, sessionmaker=sessionmaker
    )
    # The documented interfaces.py Protocol wart (async-generator vs
    # coroutine spelling) — same ignore as demo_cli's construction.
    llm_router = LLMRouter(llm_provider, settings)  # type: ignore[arg-type]
    return _BrainStack(
        session_manager=session_manager,
        session_memory=session_memory,
        context_builder=context_builder,
        prompt_builder=PromptBuilder(),
        tool_executor=tool_executor,
        safety_layer=safety_layer,
        llm_router=llm_router,
    )


def _make_brain_factory(
    settings: Settings,
    sessionmaker: async_sessionmaker[AsyncSession],
    redis: RedisClient,
    stack: _BrainStack,
) -> Callable[[str], Awaitable[Brain]]:
    """Judgment call 2: attach, then build the per-call brain."""

    async def build(session_id: str) -> Brain:
        session = await stack.session_manager.attach(session_id)
        cost_tracker = CostTracker(settings, session_factory=sessionmaker, redis=redis)
        return ConversationManager(
            session=session,
            context_builder=stack.context_builder,
            prompt_builder=stack.prompt_builder,
            # Same documented Protocol wart as _build_brain_stack.
            llm_router=stack.llm_router,  # type: ignore[arg-type]
            tool_executor=stack.tool_executor,
            safety_layer=stack.safety_layer,
            cost_tracker=cost_tracker,
            session_memory=stack.session_memory,
            tool_registry=registry,
        )

    return build


def _lazy_call_deps(
    settings: Settings, brain_factory: Callable[[str], Awaitable[Brain]]
) -> Callable[[], CallDeps]:
    """Judgment call 1: providers are built on the first call and cached
    for the process; a missing voice key fails THAT call loudly while
    the worker keeps serving."""
    cache: list[CallDeps] = []

    def build() -> CallDeps:
        if not cache:
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
                    tts=ElevenLabsTts(settings),  # type: ignore[arg-type]
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

    def peer_factory(session_id: str, send_signal: SendSignal) -> CallSession:
        return CallSession(
            session_id, send_signal, ice_servers=ice_servers, deps=deps_of()
        )

    server = SignalingServer(redis=redis, peer_factory=peer_factory)
    stop = asyncio.Event()
    _install_signal_handlers(stop)
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
            await server.close_all(reason="agent_hangup")
    finally:
        await http.aclose()
        await engine.dispose()
        await redis.close()
    log.info("voice_worker.stopped")


if __name__ == "__main__":
    asyncio.run(main())
