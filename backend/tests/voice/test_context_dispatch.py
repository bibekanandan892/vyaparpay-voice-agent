"""`ContextDispatcher` (app/voice/context_dispatch.py, Phase-4 T4) — the
`ctx.*` data-channel -> `app/context/` wiring, plus the `ctx.request_snapshot`
recovery send.

Redis is faked, not containerized — same convention as
`tests/context/conftest.py` (`RedisClient` wrapping `tests/support/
fake_redis.py`'s `FakeRedis`), reused directly here rather than duplicated:
`SnapshotIngestor`/`EventLog` are the REAL Phase-4 T2 components, so a test
that "routes to the right method with the right arguments" is proven by the
actual storage effect landing in `FakeRedis`, not by a call-recorder mock.
`send_data_channel` is a plain recording double (`sent.append`), the same
convention `tests/voice/test_worker.py`'s `Rig` already uses for
`send_message`.

No `pytest.importorskip("aiortc", ...)` guard: this module never imports
`app.voice.peer_session` (or anything that does), unlike
`tests/voice/test_run.py`/`test_peer_session.py` — `ContextDispatcher` only
depends on `app/context/**`, `app/domain/voice.py`, and `app/obs/logging.py`,
so these tests run with or without the `[voice]` extra installed.
"""

from __future__ import annotations

import asyncio
from typing import Any

import orjson

from app.context.context_compressor import ContextCompressor
from app.context.event_log import EventLog
from app.context.snapshot_ingestor import SnapshotIngestor
from app.data.redis_client import RedisClient
from app.domain.voice import DataChannelMessage
from app.voice.context_dispatch import DC_TYPE_CTX_REQUEST_SNAPSHOT, ContextDispatcher
from tests.context.conftest import load_fixture
from tests.support.fake_redis import FakeRedis

SESSION_ID = "sess_ctx_dispatch"
_CTX_KEY = f"ctx:{SESSION_ID}"


def _snapshot_envelope(seq: int, *, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    base = load_fixture("ctx_snapshot_payment_decline")
    return {**base, "seq": seq, "payload": payload if payload is not None else base["payload"]}


def _delta_envelope(seq: int, base_seq: int, **payload_extra: Any) -> dict[str, Any]:
    base = load_fixture("ctx_delta_dialog_dismissed")
    payload = {**base["payload"], "base_seq": base_seq, **payload_extra}
    return {**base, "seq": seq, "payload": payload}


def _event_envelope(seq: int, *, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    base = load_fixture("ctx_event_tap_dismiss")
    return {**base, "seq": seq, "payload": payload if payload is not None else base["payload"]}


async def _until(predicate, *, timeout: float = 5.0, message: str) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError(f"timed out waiting for {message}")
        await asyncio.sleep(0.01)


class YieldingFakeRedis(FakeRedis):
    """`FakeRedis`'s `get`/`set` never hit an `await` that actually
    suspends, so two `SnapshotIngestor.ingest_delta` calls racing on the
    same `ctx:{session_id}` key can never truly interleave against it —
    `test_run_processes_messages_in_delivery_order` would pass identically
    whether `ContextDispatcher.run()` used its single-consumer queue
    (judgment call 1) or a `create_task()`-per-message loop, since nothing
    ever yields control between one message's Redis read and its write.

    Overriding just `get`/`set` (the two ops `_read_ctx`/`_write_ctx` use)
    with a real `asyncio.sleep(0)` gives the event loop an actual point to
    switch tasks at — a regression back to fire-and-forget dispatch would
    let a second delta's `get` interleave before the first delta's `set`,
    losing an update and making this test's `'"seq":3' in stored()`
    assertion fail for real, not just by design argument."""

    async def get(self, key: str) -> str | None:
        await asyncio.sleep(0)
        return await super().get(key)

    async def set(self, key: str, value: str, ex: int | None = None) -> None:
        await asyncio.sleep(0)
        await super().set(key, value, ex=ex)


class Rig:
    """One `ContextDispatcher` over a REAL `SnapshotIngestor`/`EventLog`
    pair wired to `FakeRedis` — see module docstring for why."""

    def __init__(self, *, fake_redis: FakeRedis | None = None) -> None:
        self.fake_redis = fake_redis if fake_redis is not None else FakeRedis()
        self.redis = RedisClient(self.fake_redis, session_ttl_seconds=86_400)  # type: ignore[arg-type]
        self.ingestor = SnapshotIngestor(self.redis, ContextCompressor())
        self.event_log = EventLog(self.redis)
        self.sent: list[DataChannelMessage] = []
        self.dispatcher = ContextDispatcher(
            SESSION_ID,
            snapshot_ingestor=self.ingestor,
            event_log=self.event_log,
            send_data_channel=self.sent.append,
        )
        self._task: asyncio.Task[None] | None = None

    async def process(self, envelope: dict[str, Any]) -> None:
        """Bypass `handle_message`'s queue for deterministic, synchronous
        tests of routing/effects — the `test_handle_message_*`/`test_run_*`
        tests below cover the queue+`run()` wiring itself end to end.
        Serializes first: `_process` always parses raw wire input, exactly
        as it would arrive over the real data channel."""
        await self.dispatcher._process(orjson.dumps(envelope).decode())  # noqa: SLF001

    async def process_raw(self, raw: str | bytes) -> None:
        await self.dispatcher._process(raw)  # noqa: SLF001 -- test-only

    def stored_ctx(self) -> str | None:
        return self.fake_redis.strings.get(_CTX_KEY)

    async def start(self) -> None:
        self._task = asyncio.create_task(self.dispatcher.run(), name="test-context-dispatch")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)


# ---------------------------------------------------------------------------
# Routing: each inbound type reaches the right SnapshotIngestor/EventLog call
# ---------------------------------------------------------------------------


async def test_ctx_snapshot_routes_to_ingest_snapshot() -> None:
    rig = Rig()

    await rig.process(_snapshot_envelope(12))

    stored = rig.stored_ctx()
    assert stored is not None
    assert '"seq":12' in stored
    assert rig.sent == []  # accepted -> nothing sent back


async def test_ctx_delta_routes_to_ingest_delta() -> None:
    rig = Rig()
    await rig.process(_snapshot_envelope(42))

    await rig.process(_delta_envelope(44, base_seq=42))

    stored = rig.stored_ctx()
    assert stored is not None
    assert '"visible":false' in stored  # the delta's dialog-hidden change merged in
    assert rig.sent == []


async def test_ctx_event_routes_to_event_log_append() -> None:
    rig = Rig()

    await rig.process(_event_envelope(1))

    events = await rig.event_log.get_events(SESSION_ID)
    expected = {"type": "tap", "name": "Dismiss", "screen": "PaymentScreen", "ts": 1784536447102}
    assert events == [expected]
    assert rig.sent == []


# ---------------------------------------------------------------------------
# Malformed / unknown messages never raise, never propagate
# ---------------------------------------------------------------------------


async def test_not_valid_json_is_ignored_without_raising() -> None:
    rig = Rig()

    await rig.process_raw("not json at all {{{")  # must not raise

    assert rig.sent == []


async def test_non_object_json_is_ignored_without_raising() -> None:
    rig = Rig()

    await rig.process_raw("[1, 2, 3]")

    assert rig.sent == []


async def test_missing_type_is_ignored_without_raising() -> None:
    rig = Rig()

    await rig.process_raw('{"v": 1, "seq": 1, "ts": 1, "payload": {}}')

    assert rig.sent == []


async def test_unknown_type_is_ignored_without_raising() -> None:
    rig = Rig()

    await rig.process({"v": 1, "type": "ctx.something_future", "seq": 1, "ts": 1, "payload": {}})

    assert rig.sent == []


async def test_binary_frame_carrying_valid_json_is_parsed_like_text() -> None:
    """aiortc data-channel messages arrive as `str` (text frame) or `bytes`
    (binary frame) — `PeerSession`'s `OnData` type is `str | bytes`, so the
    parser must handle both identically."""
    rig = Rig()
    raw_bytes = orjson.dumps(_event_envelope(1))

    await rig.process_raw(raw_bytes)

    events = await rig.event_log.get_events(SESSION_ID)
    assert len(events) == 1


async def test_malformed_event_payload_is_dropped_not_appended() -> None:
    rig = Rig()

    # Missing the required "screen" field for a tap_event (app_event.v1).
    await rig.process(_event_envelope(1, payload={"type": "tap", "name": "Dismiss", "ts": 1}))

    assert await rig.event_log.get_events(SESSION_ID) == []
    assert rig.sent == []


# ---------------------------------------------------------------------------
# ctx.request_snapshot: sent exactly on needs_snapshot_request, never else
# ---------------------------------------------------------------------------


async def test_rejected_delta_with_sequence_gap_sends_one_request_snapshot() -> None:
    rig = Rig()
    await rig.process(_snapshot_envelope(5))  # last-good becomes 5

    # base_seq=99 does not match the stored seq (5) -> REJECTED_SEQUENCE_GAP.
    await rig.process(_delta_envelope(6, base_seq=99))

    assert len(rig.sent) == 1
    message = rig.sent[0]
    assert message.type == DC_TYPE_CTX_REQUEST_SNAPSHOT
    assert message.payload == {"last_good_seq": 5}
    assert message.seq == 1


async def test_rejected_schema_sends_request_snapshot() -> None:
    rig = Rig()

    # Missing required screen_context/v1 fields (only "v"/"screen" here).
    await rig.process(_snapshot_envelope(1, payload={"v": "screen_context/v1", "screen": "X"}))

    assert len(rig.sent) == 1
    assert rig.sent[0].type == DC_TYPE_CTX_REQUEST_SNAPSHOT
    assert rig.sent[0].payload == {"last_good_seq": 0}  # genesis default, nothing accepted yet


async def test_accepted_snapshot_sends_nothing() -> None:
    rig = Rig()

    await rig.process(_snapshot_envelope(1))

    assert rig.sent == []


async def test_request_snapshot_seq_increments_independently_per_dispatcher() -> None:
    rig = Rig()
    await rig.process(_delta_envelope(1, base_seq=99))  # no stored state -> rejected
    await rig.process(_delta_envelope(2, base_seq=99))  # rejected again

    assert [m.seq for m in rig.sent] == [1, 2]
    assert all(m.type == DC_TYPE_CTX_REQUEST_SNAPSHOT for m in rig.sent)


async def test_last_good_seq_tracks_the_most_recent_accepted_ingest() -> None:
    rig = Rig()
    await rig.process(_snapshot_envelope(1))
    await rig.process(_delta_envelope(2, base_seq=1))
    await rig.process(_delta_envelope(3, base_seq=2))

    await rig.process(_delta_envelope(9, base_seq=999))  # now reject

    assert rig.sent[0].payload == {"last_good_seq": 3}


async def test_send_data_channel_failure_is_swallowed() -> None:
    """Judgment call: best-effort send, same convention as worker.py's
    `_send_dc` (judgment call 8 there) — a raising sink must not blow up
    the dispatcher."""
    rig = Rig()

    def _boom(_message: DataChannelMessage) -> None:
        raise RuntimeError("channel not open")

    rig.dispatcher._send_data_channel = _boom  # type: ignore[method-assign] # noqa: SLF001

    await rig.process(_delta_envelope(1, base_seq=99))  # must not raise


# ---------------------------------------------------------------------------
# handle_message() -> run() end-to-end (the actual OnData wiring)
# ---------------------------------------------------------------------------


async def test_handle_message_never_raises_on_garbage_input() -> None:
    rig = Rig()

    rig.dispatcher.handle_message("{{{not json")
    rig.dispatcher.handle_message(b"\xff\xfe not json either")
    rig.dispatcher.handle_message("null")
    rig.dispatcher.handle_message("42")

    # Enqueuing must never raise regardless of content -- nothing to await.


async def test_handle_message_and_run_deliver_an_accepted_snapshot() -> None:
    rig = Rig()
    await rig.start()
    try:
        rig.dispatcher.handle_message(orjson.dumps(_snapshot_envelope(1)).decode())
        await _until(lambda: rig.stored_ctx() is not None, message="the queued snapshot")
    finally:
        await rig.stop()

    assert rig.sent == []


async def test_run_processes_messages_in_delivery_order() -> None:
    """Judgment call 1: a single serialized consumer, not one task per
    message — two back-to-back deltas must apply in the order they were
    enqueued, not race each other's Redis read. Uses `YieldingFakeRedis`
    (not plain `FakeRedis`) so that a regression to a `create_task()`-per-
    message loop would actually be caught here: plain `FakeRedis`'s `get`/
    `set` never suspend, so the two deltas could never interleave against
    it regardless of how `run()` dispatched them."""
    rig = Rig(fake_redis=YieldingFakeRedis())
    await rig.process(_snapshot_envelope(1))
    await rig.start()
    try:
        rig.dispatcher.handle_message(orjson.dumps(_delta_envelope(2, base_seq=1)).decode())
        rig.dispatcher.handle_message(orjson.dumps(_delta_envelope(3, base_seq=2)).decode())
        stored = rig.stored_ctx
        await _until(
            lambda: stored() is not None and '"seq":3' in stored(),
            message="both deltas to apply in order",
        )
    finally:
        await rig.stop()

    # Neither delta was rejected as a false gap (see judgment call 1).
    assert rig.sent == []
