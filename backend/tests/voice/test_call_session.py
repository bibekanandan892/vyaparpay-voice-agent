"""`CallSession`'s `ContextDispatcher` wiring (Phase-4 T4, call_session.py
judgment call 6) — the both-or-neither constructor guard, `PeerSession`'s
`on_data` seam actually pointing at the dispatcher, and that `close()`
(via `ContextDispatcher.close()`'s sentinel, that class's own judgment
call 7) tears the dispatcher's task down promptly rather than always
burning the full `_CLOSE_TIMEOUT_S`.

SKIP GUARD: skips only when the [voice] extra (aiortc/av) is absent, same
convention as tests/voice/test_peer_session.py/test_run.py — `CallSession`
imports `PeerSession`, which imports aiortc at module level.
"""

from __future__ import annotations

import pytest

pytest.importorskip("aiortc", reason="[voice] extra not installed")

import asyncio  # noqa: E402

from app.context.context_compressor import ContextCompressor  # noqa: E402
from app.context.event_log import EventLog  # noqa: E402
from app.context.snapshot_ingestor import SnapshotIngestor  # noqa: E402
from app.data.redis_client import RedisClient  # noqa: E402
from app.domain.voice import SignalMessage  # noqa: E402
from app.voice.call_session import CallSession  # noqa: E402
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
