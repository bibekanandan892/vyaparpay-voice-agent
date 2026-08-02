"""Text REPL harness standing in for the not-yet-built voice transport
(Phase-2 plan decision #3, docs/17-roadmap.md). Drives the real
`ConversationManager` — the same brain the Phase-3 `VoiceAgentWorker`
will drive unchanged (docs/05-agent-architecture.md §1.1's brain/media
split) — over stdin/stdout instead of an audio pipeline, wired to the
identical singleton stack `app/api/main.py`'s `lifespan()` builds, so a
demo run exercises production wiring end to end rather than a second,
divergent bootstrap.

Run as `python -m scripts.demo_cli --user usr_rajesh01` from `backend/`
(mirrors `scripts/seed.py`'s own module/argparse convention). Needs a
live Postgres + Redis + a real `OPENROUTER_API_KEY` — unlike the test
suite, nothing here is a double; run `python -m scripts.seed` first so
the merchant/wallet/limit fixtures this session's tools read actually
exist.

Judgment calls, flagged per house style:

1. **`configure_tools(sessionmaker)` is called explicitly here, even
   though `ToolExecutor.__init__` also calls it** (its own docstring:
   "the constructor also calls `app.tools.registry.configure
   (sessionmaker)`"). Redundant but harmless — both calls pass the same
   sessionmaker, and `configure()` just reassigns a module global — kept
   because it mirrors `app/api/main.py`'s lifespan literally (which has
   no `ToolExecutor` of its own to piggyback on) rather than relying on
   a side effect of a later constructor call to do it implicitly.
2. **`cost_tracker.finalize()` runs BEFORE `session_manager.end()`, not
   after, and this order is load-bearing, not stylistic.**
   `CostTracker.finalize()` is the sole writer of `session:{id}:turns`
   in Redis (`ConversationManager`'s own judgment call #3: "Turn records
   ... are NOT appended here ... CostTracker is the appender"), and
   `SessionManager.end()` drains exactly that key into
   `conversation_turns` as part of the same transaction that marks the
   call ended. Calling `end()` first would drain an empty/stale list —
   confirmed by reading both files, not guessed.
3. **The REPL's `You:`/`Asha:` exchange interleaves with structlog's
   JSON lines on the same stream.** `configure_logging` hard-codes
   `structlog.BytesLoggerFactory()` with no stream parameter, which
   defaults to stdout — the same stream this script prints the
   conversation to. Redirecting it would mean changing
   `configure_logging`'s contract (a frozen Batch-1 module out of this
   task's file list), so this is accepted as a known limitation rather
   than fixed here: expect JSON log lines interleaved with the demo
   transcript on a verbose `log_level`.
4. **Only `/end`, EOF (Ctrl-D), and `KeyboardInterrupt` (Ctrl-C) run the
   finalize/end/summary path.** `ConversationManager.on_stt_final` is
   documented to never raise (its own module docstring judgment call
   #5: a critical-path failure degrades to a spoken apology, not an
   exception), so an unhandled exception escaping the REPL loop here
   would only ever be an infra-level bug in this harness itself, not a
   turn failure — in that case the outer `finally` still tears down
   http/redis/engine/tracer cleanly, but the call is left in `created`
   state in Postgres rather than a possibly-wrong `end()` racing an
   in-flight turn. `SessionManager.end()` is idempotent and safe to run
   later by hand against that session id if that ever matters for a
   demo rehearsal.
"""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Sequence

import httpx
from opentelemetry import trace

import app.tools  # noqa: F401 -- import side effect: registers the 3 Phase-2 tools
from app.agent.context_builder import ContextBuilder
from app.agent.conversation_manager import ConversationManager
from app.agent.cost_tracker import CostTracker
from app.agent.llm_router import LLMRouter
from app.agent.prompt_builder import PromptBuilder
from app.agent.safety_layer import SafetyLayer
from app.agent.session_manager import SessionManager
from app.agent.tool_executor import ToolExecutor
from app.config import get_settings
from app.data.engine import create_engine_and_sessionmaker
from app.data.redis_client import RedisClient
from app.domain.types import EndReason
from app.memory.session_memory import SessionMemory
from app.obs import configure_logging, setup_observability
from app.providers.openrouter import OpenRouterLLM
from app.tools.registry import configure as configure_tools
from app.tools.registry import registry
from scripts.seed import MERCHANT_ID as _DEFAULT_USER


async def _repl(manager: ConversationManager) -> int:
    """Reads `You:` lines until `/end`, EOF (Ctrl-D), or Ctrl-C — all
    three exit the loop the same way, never a crash (judgment call #4).
    Returns the number of turns processed here (the synthetic call-open
    greeting the caller already ran is not counted in this total)."""
    turns = 0
    try:
        while True:
            try:
                line = input("You: ").strip()
            except EOFError:
                print()  # newline so the summary doesn't run into the ^D echo
                break
            if line == "/end":
                break
            reply = await manager.on_stt_final(line)
            turns += 1
            print(f"Asha: {reply}\n")
    except KeyboardInterrupt:
        print()  # newline so the summary doesn't run into the ^C echo
    return turns


async def _run(user_id: str) -> None:
    settings = get_settings()
    configure_logging(settings)
    setup_observability(settings)

    http = httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=2.0))
    engine, sessionmaker = create_engine_and_sessionmaker(settings)
    redis_client = RedisClient.from_settings(settings)

    try:
        llm_provider = OpenRouterLLM(http, settings)
        # Judgment call #1: redundant with ToolExecutor's own constructor
        # call below, kept for parity with app/api/main.py's lifespan.
        configure_tools(sessionmaker)

        session_manager = SessionManager(sessionmaker, redis_client)
        session_memory = SessionMemory(redis_client)
        context_builder = ContextBuilder(sessionmaker, session_memory)
        prompt_builder = PromptBuilder()
        # The trailing type: ignore[arg-type] is the pre-existing Protocol
        # wart llm_router.py's own module docstring documents —
        # `LLMProvider.stream` is spelled `async def ... -> AsyncIterator`,
        # which mypy reads as coroutine-returning, while OpenRouterLLM's
        # real async-generator implementation returns the iterator
        # directly. This is the first call site in the codebase that
        # constructs the real classes through the frozen Protocol-typed
        # parameter rather than a test fake shaped to dodge it, or
        # app/api/main.py's untyped `app.state.llm` assignment —
        # surfacing, not introducing, the mismatch.
        # app/domain/interfaces.py is a frozen contract outside this
        # task's scope.
        llm_router = LLMRouter(llm_provider, settings)  # type: ignore[arg-type]
        safety_layer = SafetyLayer(registry)
        tool_executor = ToolExecutor(
            registry=registry,
            safety=safety_layer,
            redis=redis_client,
            sessionmaker=sessionmaker,
        )
        cost_tracker = CostTracker(settings, session_factory=sessionmaker, redis=redis_client)

        session = await session_manager.create(user_id, None, [])
        print(f"[session {session.session_id} opened for {user_id}]\n")

        manager = ConversationManager(
            session=session,
            context_builder=context_builder,
            prompt_builder=prompt_builder,
            # Same documented Protocol wart as the LLMRouter construction
            # above, one level up: LLMRouterProto.stream has the identical
            # async-generator-vs-coroutine mismatch.
            llm_router=llm_router,  # type: ignore[arg-type]
            tool_executor=tool_executor,
            safety_layer=safety_layer,
            cost_tracker=cost_tracker,
            session_memory=session_memory,
            tool_registry=registry,
        )

        # Call-open trigger (docs/11-prompt-engineering.md §4's turn-1
        # convention): an empty utterance with an empty window renders the
        # synthetic greeting trigger instead of voicing "" back at Asha.
        greeting = await manager.on_stt_final("")
        print(f"Asha: {greeting}\n")

        repl_turns = await _repl(manager)
        turn_count = 1 + repl_turns  # + the greeting turn above

        await cost_tracker.finalize(session.session_id)
        await session_manager.end(session.session_id, EndReason.HANGUP)
        print(
            f"[call ended] turns={turn_count} "
            f"cost=${cost_tracker.call_total():.6f} "
            f"session_id={session.session_id}"
        )
    finally:
        # Mirrors app/api/main.py's lifespan finally block exactly,
        # including the tracer-provider shutdown (same documented reason:
        # BatchSpanProcessor's background thread can lose buffered spans
        # on process exit otherwise).
        await http.aclose()
        await redis_client.close()
        await engine.dispose()
        trace.get_tracer_provider().shutdown()  # type: ignore[attr-defined]


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.demo_cli",
        description="Text REPL harness for Asha, standing in for the Phase-3 voice "
        "transport (Phase-2 plan decision #3).",
    )
    parser.add_argument(
        "--user",
        default=_DEFAULT_USER,
        help=f"merchant user id to open the session as (default: {_DEFAULT_USER})",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> None:
    args = _parse_args(argv)
    asyncio.run(_run(args.user))


if __name__ == "__main__":
    main()
