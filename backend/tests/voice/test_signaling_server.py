"""SignalingServer unit tests — fake WS transport + FakeRedis, no aiortc.

The server's PeerFactory seam (signaling judgment call 2) is exactly what
lets this whole module run without the voice extra: `FakePeer` satisfies
PeerSessionLike, `FakeTransport` satisfies SignalingTransport, and the
token path runs the REAL `app.auth.signaling` mint/store/verify-and-burn
code against the shared FakeRedis (tests/support/fake_redis.py), so the
burn semantics asserted here are the production ones, not a mock's.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from app.auth.signaling import mint_signaling_token, store_signaling_token
from app.data.redis_client import RedisClient
from app.domain.voice import (
    SIG_TYPE_ANSWER,
    SIG_TYPE_BYE,
    SIG_TYPE_ERROR,
    SIG_TYPE_PONG,
    SignalMessage,
)
from app.voice import signaling as signaling_module
from app.voice.signaling import (
    CODE_AUTH_INVALID_TOKEN,
    CODE_AUTH_MISSING_TOKEN,
    CODE_INTERNAL,
    CODE_SESSION_ALREADY_ACTIVE,
    CODE_VALIDATION_SCHEMA,
    CODE_VALIDATION_UNSUPPORTED_VERSION,
    SignalingServer,
)
from tests.support.fake_redis import FakeRedis

SESSION_ID = "sess-a1f3c9"
TOKEN_TTL_S = 300


class FakeTransport:
    """Queue-driven SignalingTransport: tests feed frames in, the server's
    sends accumulate in `sent`, `end()` simulates the client disconnecting.
    """

    def __init__(self) -> None:
        self._incoming: asyncio.Queue[str | bytes | None] = asyncio.Queue()
        self.sent: list[str] = []
        self.closed = False
        self.close_code: int | None = None

    def feed(self, message: str | bytes) -> None:
        self._incoming.put_nowait(message)

    def end(self) -> None:
        self._incoming.put_nowait(None)

    def __aiter__(self) -> FakeTransport:
        return self

    async def __anext__(self) -> str | bytes:
        item = await self._incoming.get()
        if item is None or self.closed:
            raise StopAsyncIteration
        return item

    async def send(self, message: str) -> None:
        if self.closed:
            raise ConnectionError("send on closed transport")
        self.sent.append(message)

    async def close(self, code: int = 1000, reason: str = "") -> None:
        if not self.closed:
            self.closed = True
            self.close_code = code
            self.end()  # unblock a pending __anext__

    def envelopes(self) -> list[dict[str, Any]]:
        return [json.loads(m) for m in self.sent]

    def error_codes(self) -> list[str]:
        return [
            e["payload"]["code"] for e in self.envelopes() if e["type"] == SIG_TYPE_ERROR
        ]


class FakePeer:
    """PeerSessionLike double: records calls, echoes a canned answer so
    tests can watch envelopes flow server -> client."""

    def __init__(self, session_id: str, send_signal: Any) -> None:
        self.session_id = session_id
        self._send_signal = send_signal
        self.offers: list[str] = []
        self.ice: list[dict[str, Any]] = []
        self.closed = False

    async def handle_offer(self, sdp: str) -> None:
        self.offers.append(sdp)
        await self._send_signal(
            SignalMessage(type=SIG_TYPE_ANSWER, payload={"sdp": "v=0 fake-answer"})
        )

    async def handle_remote_ice(self, payload: Any) -> None:
        self.ice.append(dict(payload))

    async def close(self) -> None:
        self.closed = True


@pytest.fixture
def fake_redis() -> FakeRedis:
    return FakeRedis()


@pytest.fixture
def redis_client(fake_redis: FakeRedis) -> RedisClient:
    return RedisClient(fake_redis, session_ttl_seconds=60)  # type: ignore[arg-type]


@pytest.fixture
def server(redis_client: RedisClient) -> tuple[SignalingServer, list[FakePeer]]:
    created: list[FakePeer] = []

    def factory(session_id: str, send_signal: Any) -> FakePeer:
        peer = FakePeer(session_id, send_signal)
        created.append(peer)
        return peer

    return SignalingServer(redis=redis_client, peer_factory=factory), created


async def _mint_and_store(redis_client: RedisClient, session_id: str = SESSION_ID) -> str:
    minted = mint_signaling_token()
    await store_signaling_token(redis_client, session_id, minted.token_hash, ttl_s=TOKEN_TTL_S)
    return minted.token


async def _settle() -> None:
    """Let the connection task process everything currently fed."""
    for _ in range(20):
        await asyncio.sleep(0)


async def _connect(
    server: SignalingServer, transport: FakeTransport, *, token: str, session_id: str = SESSION_ID
) -> asyncio.Task[None]:
    task = asyncio.create_task(
        server.handle_connection(transport, session_id=session_id, token=token)
    )
    await _settle()
    return task


def _frame(type_: str, payload: dict[str, Any], *, v: int = 1) -> str:
    return json.dumps({"v": v, "type": type_, "payload": payload})


async def _finish(transport: FakeTransport, task: asyncio.Task[None]) -> None:
    transport.end()
    await asyncio.wait_for(task, timeout=2)


# ---------------------------------------------------------------------------
# Token verify-and-burn (docs/13 §6.2)
# ---------------------------------------------------------------------------


async def test_token_verified_and_burned_exactly_once(server, redis_client, fake_redis):
    sig_server, created = server
    token = await _mint_and_store(redis_client)
    transport = FakeTransport()

    task = await _connect(sig_server, transport, token=token)

    assert len(created) == 1, "peer factory runs once after a verified token"
    assert f"signal_token:{SESSION_ID}" not in fake_redis.strings, "token hash burned"
    assert transport.error_codes() == []
    await _finish(transport, task)

    # Same token replayed on a fresh connection: burned -> rejected.
    replay = FakeTransport()
    replay_task = await _connect(sig_server, replay, token=token)
    await asyncio.wait_for(replay_task, timeout=2)
    assert replay.error_codes() == [CODE_AUTH_INVALID_TOKEN]
    assert replay.closed
    assert len(created) == 1, "no second peer for a burned token"


async def test_wrong_token_rejected_and_burns_pending_token(server, redis_client, fake_redis):
    sig_server, created = server
    await _mint_and_store(redis_client)
    transport = FakeTransport()

    task = await _connect(sig_server, transport, token="st_completely-wrong")
    await asyncio.wait_for(task, timeout=2)

    assert transport.error_codes() == [CODE_AUTH_INVALID_TOKEN]
    assert transport.closed
    assert created == []
    # auth/signaling design note 4: GETDEL burns unconditionally.
    assert f"signal_token:{SESSION_ID}" not in fake_redis.strings


async def test_missing_token_short_circuits_without_touching_redis(
    server, redis_client, fake_redis
):
    sig_server, created = server
    await _mint_and_store(redis_client)
    transport = FakeTransport()

    task = await _connect(sig_server, transport, token="")
    await asyncio.wait_for(task, timeout=2)

    assert transport.error_codes() == [CODE_AUTH_MISSING_TOKEN]
    assert transport.closed
    assert created == []
    assert f"signal_token:{SESSION_ID}" in fake_redis.strings, "pending token NOT burned"


async def test_second_concurrent_connection_rejected(server, redis_client):
    sig_server, created = server
    first = FakeTransport()
    first_task = await _connect(sig_server, first, token=await _mint_and_store(redis_client))

    # Client re-mints (docs/13 §2's /token endpoint) and dials again while
    # the first connection is still live.
    second = FakeTransport()
    second_task = await _connect(
        sig_server, second, token=await _mint_and_store(redis_client)
    )
    await asyncio.wait_for(second_task, timeout=2)

    assert second.error_codes() == [CODE_SESSION_ALREADY_ACTIVE]
    assert second.closed
    assert len(created) == 1
    assert not first.closed, "the live connection is untouched"
    await _finish(first, first_task)


async def test_peer_factory_failure_sends_internal_and_unregisters(redis_client):
    def exploding_factory(session_id: str, send_signal: Any) -> FakePeer:
        raise RuntimeError("boom")

    sig_server = SignalingServer(redis=redis_client, peer_factory=exploding_factory)
    token = await _mint_and_store(redis_client)
    transport = FakeTransport()

    task = await _connect(sig_server, transport, token=token)
    await asyncio.wait_for(task, timeout=2)

    assert transport.error_codes() == [CODE_INTERNAL]
    assert transport.closed
    assert sig_server.active_sessions == ()


async def test_peer_factory_failure_reported_even_when_logging_itself_fails(
    redis_client, monkeypatch
):
    """Regression for a real repro: structlog's default console renderer
    crashed with UnicodeEncodeError rendering a traceback on Windows' cp1252
    codepage, and `log.exception()` running directly in the recovery path
    let that abort the handler before it could send the client-facing
    error envelope. `_log_exception()` must guarantee the envelope still
    goes out — and must not itself raise — even when logging is broken."""

    def exploding_log(*_args: object, **_kw: object) -> None:
        raise RuntimeError("logging pipeline is broken")

    monkeypatch.setattr(signaling_module.log, "exception", exploding_log)

    def exploding_factory(session_id: str, send_signal: Any) -> FakePeer:
        raise RuntimeError("boom")

    sig_server = SignalingServer(redis=redis_client, peer_factory=exploding_factory)
    token = await _mint_and_store(redis_client)
    transport = FakeTransport()

    task = await _connect(sig_server, transport, token=token)
    await asyncio.wait_for(task, timeout=2)

    assert transport.error_codes() == [CODE_INTERNAL], (
        "the error envelope must still reach the client despite log.exception() failing"
    )
    assert transport.closed
    assert sig_server.active_sessions == ()


# ---------------------------------------------------------------------------
# Envelope routing (docs/13 §6, §9)
# ---------------------------------------------------------------------------


async def test_offer_routed_to_peer_and_answer_flows_back(server, redis_client):
    sig_server, created = server
    transport = FakeTransport()
    task = await _connect(sig_server, transport, token=await _mint_and_store(redis_client))

    transport.feed(_frame("offer", {"sdp": "v=0 client-offer"}))
    await _settle()

    assert created[0].offers == ["v=0 client-offer"]
    answers = [e for e in transport.envelopes() if e["type"] == SIG_TYPE_ANSWER]
    assert answers == [{"v": 1, "type": "answer", "payload": {"sdp": "v=0 fake-answer"}}]
    await _finish(transport, task)


async def test_ice_routed_to_peer(server, redis_client):
    sig_server, created = server
    transport = FakeTransport()
    task = await _connect(sig_server, transport, token=await _mint_and_store(redis_client))

    payload = {"candidate": "candidate:1 1 udp 1 10.0.0.1 4000 typ host", "sdpMid": "0",
               "sdpMLineIndex": 0}
    transport.feed(_frame("ice", payload))
    await _settle()

    assert created[0].ice == [payload]
    assert transport.error_codes() == []
    await _finish(transport, task)


async def test_ice_shape_violations_map_to_validation_schema_not_internal(
    server, redis_client
):
    """docs/13 §1.1 draws VALIDATION_SCHEMA vs INTERNAL deliberately —
    a malformed client payload must never surface as a server fault."""
    sig_server, created = server
    transport = FakeTransport()
    task = await _connect(sig_server, transport, token=await _mint_and_store(redis_client))

    # Real candidate but neither sdpMid nor sdpMLineIndex.
    transport.feed(
        _frame("ice", {"candidate": "candidate:1 1 udp 1 10.0.0.1 4000 typ host"})
    )
    # Non-string, non-null candidate.
    transport.feed(_frame("ice", {"candidate": 42, "sdpMid": "0"}))
    # Empty-string candidate.
    transport.feed(_frame("ice", {"candidate": "", "sdpMid": "0"}))
    await _settle()

    assert transport.error_codes() == [CODE_VALIDATION_SCHEMA] * 3
    assert created[0].ice == [], "no malformed payload ever reached the peer"
    assert not transport.closed, "shape violations never kill the connection"
    await _finish(transport, task)


async def test_unknown_envelope_type_ignored_not_fatal(server, redis_client):
    sig_server, _ = server
    transport = FakeTransport()
    task = await _connect(sig_server, transport, token=await _mint_and_store(redis_client))

    transport.feed(_frame("renegotiate.v2", {"anything": True}))
    await _settle()
    assert transport.error_codes() == [], "docs/13 §9: unknown types are ignored"
    assert not transport.closed

    # The connection is still fully alive: a ping still gets its pong.
    transport.feed(_frame("ping", {"ts": 1784536470000}))
    await _settle()
    pongs = [e for e in transport.envelopes() if e["type"] == SIG_TYPE_PONG]
    assert pongs == [{"v": 1, "type": "pong", "payload": {"ts": 1784536470000}}]
    await _finish(transport, task)


async def test_malformed_json_gets_error_envelope_and_connection_survives(server, redis_client):
    sig_server, _ = server
    transport = FakeTransport()
    task = await _connect(sig_server, transport, token=await _mint_and_store(redis_client))

    transport.feed("{this is not json")
    await _settle()
    assert transport.error_codes() == [CODE_VALIDATION_SCHEMA]
    assert not transport.closed

    transport.feed(_frame("ping", {"ts": 7}))
    await _settle()
    assert any(e["type"] == SIG_TYPE_PONG for e in transport.envelopes())
    await _finish(transport, task)


async def test_binary_frame_rejected_with_error_envelope(server, redis_client):
    sig_server, _ = server
    transport = FakeTransport()
    task = await _connect(sig_server, transport, token=await _mint_and_store(redis_client))

    transport.feed(b"\x00\x01\x02")
    await _settle()
    assert transport.error_codes() == [CODE_VALIDATION_SCHEMA]
    assert not transport.closed
    await _finish(transport, task)


async def test_unsupported_envelope_version_rejected(server, redis_client):
    sig_server, _ = server
    transport = FakeTransport()
    task = await _connect(sig_server, transport, token=await _mint_and_store(redis_client))

    transport.feed(_frame("ping", {"ts": 1}, v=2))
    await _settle()
    assert transport.error_codes() == [CODE_VALIDATION_UNSUPPORTED_VERSION]
    await _finish(transport, task)


async def test_client_sent_answer_is_a_protocol_violation(server, redis_client):
    sig_server, _ = server
    transport = FakeTransport()
    task = await _connect(sig_server, transport, token=await _mint_and_store(redis_client))

    transport.feed(_frame("answer", {"sdp": "v=0 wrong-direction"}))
    await _settle()
    assert transport.error_codes() == [CODE_VALIDATION_SCHEMA]
    assert not transport.closed
    await _finish(transport, task)


async def test_offer_without_sdp_gets_validation_error(server, redis_client):
    sig_server, created = server
    transport = FakeTransport()
    task = await _connect(sig_server, transport, token=await _mint_and_store(redis_client))

    transport.feed(_frame("offer", {}))
    await _settle()
    assert transport.error_codes() == [CODE_VALIDATION_SCHEMA]
    assert created[0].offers == []
    await _finish(transport, task)


async def test_peer_offer_failure_maps_to_internal_error_envelope(redis_client):
    class BrokenPeer(FakePeer):
        async def handle_offer(self, sdp: str) -> None:
            raise RuntimeError("sdp parse failed")

    created: list[FakePeer] = []

    def factory(session_id: str, send_signal: Any) -> FakePeer:
        peer = BrokenPeer(session_id, send_signal)
        created.append(peer)
        return peer

    sig_server = SignalingServer(redis=redis_client, peer_factory=factory)
    transport = FakeTransport()
    task = await _connect(sig_server, transport, token=await _mint_and_store(redis_client))

    transport.feed(_frame("offer", {"sdp": "v=0 offer"}))
    await _settle()
    assert transport.error_codes() == [CODE_INTERNAL]
    assert not transport.closed
    await _finish(transport, task)


# ---------------------------------------------------------------------------
# Teardown paths (docs/13 §6's bye; signaling judgment call 8)
# ---------------------------------------------------------------------------


async def test_bye_closes_peer_and_unregisters(server, redis_client):
    sig_server, created = server
    transport = FakeTransport()
    task = await _connect(sig_server, transport, token=await _mint_and_store(redis_client))
    assert sig_server.active_sessions == (SESSION_ID,)

    transport.feed(_frame("bye", {"reason": "user_hangup"}))
    await asyncio.wait_for(task, timeout=2)

    assert created[0].closed
    assert sig_server.active_sessions == ()
    assert transport.closed


async def test_transport_disconnect_closes_peer(server, redis_client):
    sig_server, created = server
    transport = FakeTransport()
    task = await _connect(sig_server, transport, token=await _mint_and_store(redis_client))

    transport.end()  # client vanished without a bye
    await asyncio.wait_for(task, timeout=2)

    assert created[0].closed
    assert sig_server.active_sessions == ()


async def test_close_all_sends_bye_and_tears_down(server, redis_client):
    sig_server, created = server
    transport = FakeTransport()
    task = await _connect(sig_server, transport, token=await _mint_and_store(redis_client))

    await sig_server.close_all(reason="agent_hangup")
    await asyncio.wait_for(task, timeout=2)

    byes = [e for e in transport.envelopes() if e["type"] == SIG_TYPE_BYE]
    assert byes == [{"v": 1, "type": "bye", "payload": {"reason": "agent_hangup"}}]
    assert created[0].closed
    assert transport.closed
    assert sig_server.active_sessions == ()


# ---------------------------------------------------------------------------
# handle_websocket adapter (path + query parsing)
# ---------------------------------------------------------------------------


class _FakeWsConnection(FakeTransport):
    def __init__(self, path: str) -> None:
        super().__init__()
        self.request = type("Request", (), {"path": path})()


async def test_handle_websocket_rejects_unknown_path(server):
    sig_server, created = server
    connection = _FakeWsConnection("/v2/other?session_id=x&token=y")

    await asyncio.wait_for(sig_server.handle_websocket(connection), timeout=2)

    assert connection.closed
    assert connection.close_code == 1008
    assert created == []


async def test_handle_websocket_extracts_session_and_token(server, redis_client):
    sig_server, created = server
    token = await _mint_and_store(redis_client)
    connection = _FakeWsConnection(f"/v1/signal?session_id={SESSION_ID}&token={token}")

    task = asyncio.create_task(sig_server.handle_websocket(connection))
    await _settle()
    assert [p.session_id for p in created] == [SESSION_ID]
    await _finish(connection, task)
