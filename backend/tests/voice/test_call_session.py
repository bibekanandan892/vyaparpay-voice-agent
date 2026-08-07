"""`CallSession`'s `ContextDispatcher` wiring (Phase-4 T4, call_session.py
judgment call 6) — the both-or-neither constructor guard, `PeerSession`'s
`on_data` seam actually pointing at the dispatcher, and that `close()`
(via `ContextDispatcher.close()`'s sentinel, that class's own judgment
call 7) tears the dispatcher's task down promptly rather than always
burning the full `_CLOSE_TIMEOUT_S`. Also the post-call-pipeline-wiring
double-invocation guard: `close()` called twice concurrently must still
run the worker's `on_call_ended` exactly once (below).

SKIP GUARD: skips only when the [voice] extra (aiortc/av) is absent, same
convention as tests/voice/test_peer_session.py/test_run.py — `CallSession`
imports `PeerSession`, which imports aiortc at module level.
"""

from __future__ import annotations

import pytest

pytest.importorskip("aiortc", reason="[voice] extra not installed")

import asyncio  # noqa: E402
from collections.abc import Callable, Coroutine  # noqa: E402
from typing import Any, cast  # noqa: E402

from app.config import Settings  # noqa: E402
from app.context.context_compressor import ContextCompressor  # noqa: E402
from app.context.event_log import EventLog  # noqa: E402
from app.context.snapshot_ingestor import SnapshotIngestor  # noqa: E402
from app.data.redis_client import RedisClient  # noqa: E402
from app.domain.types import EndReason  # noqa: E402
from app.domain.voice import SignalMessage, SttProvider, TtsProvider, VadModel  # noqa: E402
from app.voice.call_session import CallDeps, CallSession  # noqa: E402
from app.voice.worker import BuiltBrain  # noqa: E402
from tests.fakes import FakeBrain, FakeStt, FakeTts, FakeVad  # noqa: E402
from tests.support.fake_redis import FakeRedis  # noqa: E402


async def _noop_signal(message: SignalMessage) -> None:
    return None


def _ctx_deps() -> tuple[SnapshotIngestor, EventLog]:
    redis = RedisClient(FakeRedis(), session_ttl_seconds=86_400)  # type: ignore[arg-type]
    return SnapshotIngestor(redis, ContextCompressor()), EventLog(redis)


async def test_supplying_only_snapshot_ingestor_raises() -> None:
    ingestor, _ = _ctx_deps()

    with pytest.raises(ValueError, match="together"):
        CallSession("sess-1", _noop_signal, ice_servers=(), snapshot_ingestor=ingestor)


async def test_supplying_only_event_log_raises() -> None:
    _, event_log = _ctx_deps()

    with pytest.raises(ValueError, match="together"):
        CallSession("sess-2", _noop_signal, ice_servers=(), event_log=event_log)


async def test_neither_given_leaves_on_data_none() -> None:
    session = CallSession("sess-3", _noop_signal, ice_servers=())
    try:
        assert session._context_dispatcher is None
        assert session._peer._on_data is None
    finally:
        await session.close()


async def test_both_given_wires_on_data_to_the_dispatcher() -> None:
    ingestor, event_log = _ctx_deps()

    session = CallSession(
        "sess-4",
        _noop_signal,
        ice_servers=(),
        snapshot_ingestor=ingestor,
        event_log=event_log,
    )
    try:
        assert session._context_dispatcher is not None
        # Bound methods are `==`-equal but never `is`-identical per access.
        assert session._peer._on_data == session._context_dispatcher.handle_message
        # The dispatcher's own task is running alongside the drain tasks.
        assert len(session._tasks) == 3  # dispatcher + stt-drain + vad-drain (deps=None)
        assert all(not t.done() for t in session._tasks)
    finally:
        await session.close()


async def test_close_tears_down_the_dispatcher_task_without_hanging_or_raising() -> None:
    ingestor, event_log = _ctx_deps()
    session = CallSession(
        "sess-5",
        _noop_signal,
        ice_servers=(),
        snapshot_ingestor=ingestor,
        event_log=event_log,
    )
    dispatcher_task = session._tasks[0]

    await asyncio.wait_for(session.close(), timeout=2)

    assert dispatcher_task.done()


# ---------------------------------------------------------------------------
# Post-call-pipeline-wiring: the double-invocation guard. An operator
# DELETE can race a client-initiated bye/transport-EOF such that
# CallSession.close() gets called twice for the same live call (both
# `PeerSession.close()` and `CallSession.close()`'s own body are documented
# idempotent, signaling.py judgment call 8). What actually decides whether
# `on_call_ended` — and therefore CostTracker.finalize()/SessionManager
# .end() — runs once or twice is NOT close()'s own idempotency, but that
# `VoiceAgentWorker.run()` is a single asyncio task that only ever executes
# its body, and therefore its `finally`, exactly once. This test proves
# THAT guarantee directly, at the level where a bug would actually double-
# write `call_costs` or double-`RuntimeError` out of `SessionManager.end()`.
# ---------------------------------------------------------------------------


def _real_call_deps(
    settings: Settings,
    brain: FakeBrain,
    on_call_ended: Callable[[EndReason], Coroutine[Any, Any, None]],
) -> CallDeps:
    async def brain_factory(_session_id: str) -> BuiltBrain:
        return BuiltBrain(brain=brain, on_call_ended=on_call_ended)

    return CallDeps(
        settings=settings,
        # Same documented Protocol spelling wart as tests/voice/test_worker.py.
        stt=cast(SttProvider, FakeStt()),
        tts=cast(TtsProvider, FakeTts()),
        vad_factory=lambda: cast(VadModel, FakeVad()),
        brain_factory=brain_factory,
    )


async def test_close_called_twice_concurrently_runs_on_call_ended_exactly_once(
    settings: Settings,
) -> None:
    """The double-invocation guard, proven directly: race two concurrent
    `close()` calls on the SAME live `CallSession` (deps=CallDeps(...), a
    real `VoiceAgentWorker`) and assert the worker's `on_call_ended` fired
    exactly once, with the literal `EndReason.HANGUP` — never twice, and
    never a value computed by re-deriving it from the call under test."""
    on_call_ended_calls: list[EndReason] = []

    async def on_call_ended(reason: EndReason) -> None:
        on_call_ended_calls.append(reason)

    deps = _real_call_deps(settings, FakeBrain(), on_call_ended)
    session = CallSession("sess-race", _noop_signal, ice_servers=(), deps=deps)

    # No offer was ever sent — this races close() against itself before
    # any media negotiated, the harshest version of "close() called twice"
    # (peer_session.py's close() is documented idempotent regardless of
    # whether a track was ever attached).
    await asyncio.wait_for(asyncio.gather(session.close(), session.close()), timeout=5)

    assert on_call_ended_calls == [EndReason.HANGUP]


# ---------------------------------------------------------------------------
# CRITICAL regression (2026-08-07, found on the first real device call):
# worker.py shields on_call_ended from _CLOSE_TIMEOUT_S's cancel so a slow
# finalize is never interrupted mid-write — but that alone means close()
# can return (and signaling.py can free `_active[session_id]`) BEFORE
# finalize has actually finished, reopening the exact reconnect race a
# prior CRITICAL fix closed (signaling.py's own comment). These two tests
# prove call_session.py's judgment call 7 closes that window again: close()
# now separately waits for `worker.finalize_task`, and never cancels it
# even if ITS OWN generous budget (_FINALIZE_TIMEOUT_S) is exceeded.
#
# Both timeouts are monkeypatched to tiny values so these run in well
# under a second rather than genuinely waiting out production-sized
# (5s / 60s) budgets.
# ---------------------------------------------------------------------------


async def test_close_waits_for_a_slow_finalize_without_cancelling_it(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A finalize that outlives the patched _CLOSE_TIMEOUT_S (so the
    turn-loop-bound wait times out and cancels worker_task) must still
    run to completion — proven directly by asserting it actually
    finished before close() returns, not merely that close() didn't
    raise."""
    import app.voice.call_session as call_session_module

    monkeypatch.setattr(call_session_module, "_CLOSE_TIMEOUT_S", 0.05)
    monkeypatch.setattr(call_session_module, "_FINALIZE_TIMEOUT_S", 2.0)

    finished = False

    async def slow_on_call_ended(reason: EndReason) -> None:
        nonlocal finished
        await asyncio.sleep(0.3)  # outlives the patched _CLOSE_TIMEOUT_S
        finished = True

    deps = _real_call_deps(settings, FakeBrain(), slow_on_call_ended)
    session = CallSession("sess-slow-finalize", _noop_signal, ice_servers=(), deps=deps)

    await asyncio.wait_for(session.close(), timeout=5)

    assert finished, "close() returned before the shielded finalize task actually finished"


async def test_close_gives_up_waiting_past_finalize_timeout_but_never_cancels(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other half: if finalize somehow outlives even the generous
    _FINALIZE_TIMEOUT_S, close() stops WAITING — but the task itself
    must never be cancelled. Proven in two steps: close() returns before
    the slow finalize's sleep completes (it gave up), then the task is
    shown to finish normally on its own afterward (it was never
    cancelled)."""
    import app.voice.call_session as call_session_module

    monkeypatch.setattr(call_session_module, "_CLOSE_TIMEOUT_S", 0.02)
    monkeypatch.setattr(call_session_module, "_FINALIZE_TIMEOUT_S", 0.05)

    finished = False

    async def slow_on_call_ended(reason: EndReason) -> None:
        nonlocal finished
        await asyncio.sleep(0.2)  # outlives the patched _FINALIZE_TIMEOUT_S
        finished = True

    deps = _real_call_deps(settings, FakeBrain(), slow_on_call_ended)
    session = CallSession("sess-finalize-timeout", _noop_signal, ice_servers=(), deps=deps)

    await asyncio.wait_for(session.close(), timeout=1)
    assert not finished, "close() should have given up waiting before the sleep finished"

    assert session._worker is not None
    task = session._worker.finalize_task
    assert task is not None
    # Never cancelled: left running, and it finishes on its own.
    await asyncio.wait_for(asyncio.shield(task), timeout=1)
    assert finished
    assert not task.cancelled()
