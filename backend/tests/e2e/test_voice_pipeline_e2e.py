"""Fake-peer E2E for the Phase-3 voice pipeline (T5.1) — a real aiortc
client peer talking to the REAL `SignalingServer` (over a real WebSocket,
with a real verify-and-burn of a real one-time signaling token), the REAL
`PeerSession`, the REAL `CallSession` wiring, and the REAL
`VoiceAgentWorker` turn machine. Audio goes in at the client's mic track,
Asha's (fake-TTS) audio comes back out of the client's speaker track, and
the `ctx` data channel carries the docs/13 §4 transcript/state envelopes.

What is real here, and what is not — the same honesty convention
tests/e2e/test_canonical_conversation.py establishes:

REAL: SignalingServer + its token auth (`mint_signaling_token` →
`store_signaling_token` → `verify_and_burn`), the websockets transport,
both RTCPeerConnections (offer/answer, ICE on host candidates, DTLS-SRTP,
Opus encode/decode both directions), the `ctx` data channel, AudioIngress
(48 kHz Opus → 16 kHz PCM fan-out), VadEndpointer's §5.3 endpoint policy,
the sentence chunker, AudioEgress's 20 ms paced queue, and the whole
worker turn machine including its spans.

FAKED: the three vendor/brain seams the Protocols exist for — VAD
(`_EnergyVad`, an honest energy gate over the real decoded frames rather
than onnxruntime), STT (`_ScriptedStt`, which emits its scripted partial
and final once real uplink audio has actually flowed through it), TTS
(`tests/fakes.py`'s `FakeTts`, a deterministic square wave), and the
brain (`FakeBrain`). Redis is `tests/support/fake_redis.py`'s in-memory
`FakeRedis` behind the REAL `RedisClient`.

DOCKER NOTE: this file lives in tests/e2e/ so it runs in CI's
docker-gated E2E job alongside `test_canonical_conversation.py` (the
local gate command ignores tests/e2e). Unlike that test it needs no
container of its own — only the `[voice]` extra — so it is skipped, not
failed, where aiortc/av are absent.
"""

from __future__ import annotations

import pytest

pytest.importorskip("aiortc", reason="[voice] extra not installed")
pytest.importorskip("av", reason="[voice] extra not installed")
pytest.importorskip("websockets", reason="[voice] extra not installed")

import array  # noqa: E402
import asyncio  # noqa: E402
import json  # noqa: E402
import math  # noqa: E402
from collections.abc import AsyncIterator, Callable  # noqa: E402
from fractions import Fraction  # noqa: E402
from typing import Any, cast  # noqa: E402

import av  # noqa: E402
import orjson  # noqa: E402
from aiortc import (  # noqa: E402
    RTCConfiguration,
    RTCPeerConnection,
    RTCSessionDescription,
)
from aiortc.mediastreams import MediaStreamError, MediaStreamTrack  # noqa: E402
from websockets.asyncio.client import connect  # noqa: E402
from websockets.asyncio.server import serve  # noqa: E402

from app.auth.signaling import mint_signaling_token, store_signaling_token  # noqa: E402
from app.config import Settings  # noqa: E402
from app.data.redis_client import RedisClient  # noqa: E402
from app.domain.voice import (  # noqa: E402
    DC_TYPE_AGENT_STATE,
    DC_TYPE_TRANSCRIPT_FINAL,
    DC_TYPE_TRANSCRIPT_PARTIAL,
    SIG_TYPE_ANSWER,
    SIG_TYPE_ICE,
    SIG_TYPE_OFFER,
    SignalMessage,
    SttEvent,
    SttFinal,
    SttPartial,
    SttProvider,
    TtsProvider,
    VadModel,
)
from app.voice.call_session import CallDeps, CallSession  # noqa: E402
from app.voice.peer_session import SendSignal  # noqa: E402
from app.voice.signaling import SIGNALING_PATH, SignalingServer  # noqa: E402
from tests.fakes import FakeBrain, FakeTts  # noqa: E402
from tests.support.fake_redis import FakeRedis  # noqa: E402

WIRE_RATE = 48_000
FRAME_SAMPLES = 960  # 20 ms at 48 kHz

UTTERANCE = "My payment to Amazon Business was declined."
REPLY = "I can see the decline. Your daily limit was exceeded."

# The scripted STT emits its partial once this much real uplink PCM has
# flowed through it, and its final shortly after — both keyed on bytes
# actually received, so the transcript can never precede the audio.
_PARTIAL_AFTER_BYTES = 16_000 * 2 // 10  # ~100 ms of 16 kHz s16
_FINAL_AFTER_BYTES = _PARTIAL_AFTER_BYTES * 3

# _EnergyVad's speech gate over real decoded frames (the client's sine is
# amplitude 12000; its silence is exact zeros).
_ENERGY_SPEECH_THRESHOLD = 500


def _sine_pcm(sample_rate: int, duration_ms: int, *, freq: float = 440.0) -> bytes:
    total = sample_rate * duration_ms // 1000
    samples = array.array(
        "h", (int(12_000 * math.sin(2 * math.pi * freq * i / sample_rate)) for i in range(total))
    )
    return samples.tobytes()


def _frame_payload(frame: av.AudioFrame) -> bytes:
    return bytes(frame.planes[0])[: frame.samples * 2]


def _has_energy(pcm16: bytes, *, threshold: int = _ENERGY_SPEECH_THRESHOLD) -> bool:
    samples = array.array("h")
    samples.frombytes(pcm16)
    return bool(samples) and max(abs(s) for s in samples) > threshold


async def _until(predicate: Callable[[], bool], *, message: str, timeout: float = 30.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.05)
    pytest.fail(f"timed out waiting for {message}")


class UtteranceTrack(MediaStreamTrack):
    """The fake client's mic: real-time-paced 20 ms frames — a 440 Hz sine
    for `speech_ms`, then digital silence forever. That shape is what
    drives the REAL VadEndpointer through min-speech confirmation and
    then past the 250 ms trailing-silence endpoint (docs/06 §5.3 row 3).
    A MediaStreamTrack must pace its own recv() (peer_session judgment
    call 1)."""

    kind = "audio"

    def __init__(self, *, speech_ms: int = 600) -> None:
        super().__init__()
        self._speech = _sine_pcm(WIRE_RATE, 1000)
        self._silence = b"\x00" * (FRAME_SAMPLES * 2)
        self._speech_frames = speech_ms // 20
        self._frame_no = 0
        self._start: float | None = None

    async def recv(self) -> av.AudioFrame:
        if self.readyState != "live":
            raise MediaStreamError
        loop = asyncio.get_running_loop()
        if self._start is None:
            self._start = loop.time()
        delay = self._start + self._frame_no * 0.02 - loop.time()
        if delay > 0:
            await asyncio.sleep(delay)
        frame = av.AudioFrame(format="s16", layout="mono", samples=FRAME_SAMPLES)
        frame.sample_rate = WIRE_RATE
        frame.time_base = Fraction(1, WIRE_RATE)
        frame.pts = self._frame_no * FRAME_SAMPLES
        if self._frame_no < self._speech_frames:
            offset = (self._frame_no % 50) * FRAME_SAMPLES * 2
            frame.planes[0].update(self._speech[offset : offset + FRAME_SAMPLES * 2])
        else:
            frame.planes[0].update(self._silence)
        self._frame_no += 1
        return frame


class _EnergyVad:
    """`VadModel` over the REAL decoded uplink frames: speech iff the hop
    carries energy. Honest for this fixture (the client sends a loud sine
    then exact zeros) and keeps onnxruntime — and Silero's model file —
    out of the E2E, which is exactly the split `VadModel` exists for."""

    def prob(self, frame_30ms: bytes) -> float:
        return 0.9 if _has_energy(frame_30ms) else 0.0


class _ScriptedStt:
    """`SttProvider` that transcribes on real audio arrival: after
    ~100 ms of uplink PCM it emits the partial (which the completeness
    hold needs — it ends in terminal punctuation), and after ~300 ms the
    authoritative final. Byte-keyed rather than time-keyed so the
    transcript can never run ahead of the audio that produced it."""

    def __init__(self, utterance: str) -> None:
        self._utterance = utterance
        self.bytes_received = 0
        self.streams_opened = 0

    async def stream(
        self, audio: AsyncIterator[bytes], *, sample_rate: int
    ) -> AsyncIterator[SttEvent]:
        self.streams_opened += 1
        partial_sent = False
        final_sent = False
        async for chunk in audio:
            self.bytes_received += len(chunk)
            ts_ms = self.bytes_received // (sample_rate * 2 // 1000)
            if not partial_sent and self.bytes_received >= _PARTIAL_AFTER_BYTES:
                partial_sent = True
                yield SttPartial(text=self._utterance, ts_ms=ts_ms)
            elif not final_sent and self.bytes_received >= _FINAL_AFTER_BYTES:
                final_sent = True
                yield SttFinal(text=self._utterance, ts_ms=ts_ms)


class FakeClientPeer:
    """The far end: a real RTCPeerConnection driven over the real
    signaling WebSocket, exactly as `WebRtcClient` does (docs/06 §2 — the
    client offers, the server mirrors)."""

    def __init__(self, url: str) -> None:
        self._url = url
        self.pc = RTCPeerConnection(RTCConfiguration(iceServers=[]))
        self.track = UtteranceTrack()
        self.pc.addTrack(self.track)
        self.channel = self.pc.createDataChannel("ctx")
        self.envelopes: list[dict[str, Any]] = []
        self.channel.on("message", self._on_message)
        self.received_frames: list[av.AudioFrame] = []
        self.pc.on("track", self._on_track)
        self._ws: Any = None
        self._reader: asyncio.Task[None] | None = None
        self._collector: asyncio.Task[None] | None = None
        self._answered = asyncio.Event()

    def _on_message(self, message: str | bytes) -> None:
        self.envelopes.append(json.loads(message))

    def _on_track(self, track: MediaStreamTrack) -> None:
        self._collector = asyncio.create_task(self._collect(track))

    async def _collect(self, track: MediaStreamTrack) -> None:
        try:
            while True:
                self.received_frames.append(await track.recv())
        except MediaStreamError:
            pass

    async def connect(self) -> None:
        self._ws = await connect(self._url)
        self._reader = asyncio.create_task(self._read_signals())
        offer = await self.pc.createOffer()
        await self.pc.setLocalDescription(offer)  # gathers ICE -> complete SDP
        await self._send(
            SignalMessage(type=SIG_TYPE_OFFER, payload={"sdp": self.pc.localDescription.sdp})
        )
        await asyncio.wait_for(self._answered.wait(), timeout=20)

    async def _send(self, message: SignalMessage) -> None:
        await self._ws.send(message.model_dump_json())

    async def _read_signals(self) -> None:
        try:
            async for raw in self._ws:
                message = SignalMessage.model_validate(orjson.loads(raw))
                if message.type == SIG_TYPE_ANSWER:
                    await self.pc.setRemoteDescription(
                        RTCSessionDescription(sdp=message.payload["sdp"], type="answer")
                    )
                    self._answered.set()
                elif message.type == SIG_TYPE_ICE and message.payload.get("candidate"):
                    pass  # server candidates ride the answer SDP (PS call 3)
        except Exception:  # pragma: no cover - transport teardown noise
            pass

    def envelopes_of(self, type_: str, *, role: str | None = None) -> list[dict[str, Any]]:
        out = [e for e in self.envelopes if e["type"] == type_]
        if role is not None:
            out = [e for e in out if e["payload"].get("role") == role]
        return out

    async def close(self) -> None:
        await self.pc.close()
        for task in (self._reader, self._collector):
            if task is not None:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        if self._ws is not None:
            await self._ws.close()


async def test_voice_pipeline_end_to_end_through_real_signaling() -> None:
    """Audio in at the client's mic → endpoint → transcript → brain →
    fake-TTS audio decoded back at the client, with the docs/13 §4
    envelopes on the `ctx` channel."""
    settings = Settings(  # type: ignore[arg-type]
        jwt_secret="test-jwt-secret",
        database_url="postgresql+asyncpg://test:test@localhost/test",
        openrouter_api_key="test-openrouter-key",
    )
    redis = RedisClient(cast(Any, FakeRedis()), session_ttl_seconds=settings.session_ttl_seconds)

    session_id = "e2evoice01"
    minted = mint_signaling_token()
    await store_signaling_token(redis, session_id, minted.token_hash, ttl_s=300)

    stt = _ScriptedStt(UTTERANCE)
    tts = FakeTts()
    brain = FakeBrain()
    brain.script(REPLY)

    async def brain_factory(_session_id: str) -> FakeBrain:
        return brain

    deps = CallDeps(
        settings=settings,
        # Same documented Protocol spelling wart as the unit tests: the
        # fakes are async generators, the Protocols read as coroutines.
        stt=cast(SttProvider, stt),
        tts=cast(TtsProvider, tts),
        vad_factory=lambda: cast(VadModel, _EnergyVad()),
        brain_factory=brain_factory,
    )
    sessions: list[CallSession] = []

    def peer_factory(sid: str, send_signal: SendSignal) -> CallSession:
        session = CallSession(sid, send_signal, ice_servers=(), deps=deps)
        sessions.append(session)
        return session

    server = SignalingServer(redis=redis, peer_factory=peer_factory)

    async with serve(server.handle_websocket, "127.0.0.1", 0) as ws_server:
        port = ws_server.sockets[0].getsockname()[1]
        url = f"ws://127.0.0.1:{port}{SIGNALING_PATH}?session_id={session_id}&token={minted.token}"
        client = FakeClientPeer(url)
        try:
            await client.connect()
            await _until(
                lambda: client.pc.connectionState == "connected", message="the media path"
            )

            # The real uplink actually reached the STT seam...
            await _until(lambda: stt.bytes_received > 0, message="uplink PCM at the STT seam")
            # ...the real endpointer opened a turn off the real VAD...
            await _until(lambda: bool(brain.calls), message="the brain turn")
            # ...and the agent's caption was frozen at the end of it.
            await _until(
                lambda: bool(client.envelopes_of(DC_TYPE_TRANSCRIPT_FINAL, role="agent")),
                message="the agent transcript.final on the ctx channel",
            )
            # Asha's synthesized audio was Opus-encoded, sent, and decoded
            # back at the client's speaker track.
            await _until(
                lambda: any(_has_energy(_frame_payload(f)) for f in client.received_frames),
                message="agent audio at the fake client",
            )

            # -- the transcript the brain actually heard ----------------
            assert brain.calls == [UTTERANCE]

            # -- docs/13 §4 envelopes, server -> client -----------------
            user_final = client.envelopes_of(DC_TYPE_TRANSCRIPT_FINAL, role="user")
            agent_final = client.envelopes_of(DC_TYPE_TRANSCRIPT_FINAL, role="agent")
            assert user_final[0]["payload"] == {"turn": 1, "role": "user", "text": UTTERANCE}
            assert agent_final[0]["payload"] == {"turn": 1, "role": "agent", "text": REPLY}
            assert all(e["v"] == 1 for e in client.envelopes)
            seqs = [e["seq"] for e in client.envelopes]
            assert seqs == sorted(seqs)  # per-direction counter, ordered

            # The indicator was driven, never inferred (docs/13 §4).
            states = [e["payload"]["state"] for e in client.envelopes_of(DC_TYPE_AGENT_STATE)]
            assert "thinking" in states
            assert "speaking" in states
            await _until(lambda: "listening" in [
                e["payload"]["state"] for e in client.envelopes_of(DC_TYPE_AGENT_STATE)
            ], message="the listening transition at end of turn")

            # Agent captions lead the audio sentence by sentence (§4.2).
            agent_partials = [
                e["payload"]["text"]
                for e in client.envelopes_of(DC_TYPE_TRANSCRIPT_PARTIAL, role="agent")
            ]
            assert agent_partials[0] == "I can see the decline."
            assert agent_partials[-1] == REPLY

            # Sentence-level TTS dispatch really happened per sentence.
            assert [text for text, _ in tts.calls] == [
                "I can see the decline.",
                "Your daily limit was exceeded.",
            ]
        finally:
            await client.close()
            for session in sessions:
                await session.close()
            await server.close_all(reason="agent_hangup")

    # The one-time token was burned by the real verify-and-burn path, so
    # the same URL can never be replayed (docs/13 §6.2).
    assert await redis.take_signaling_token_hash(session_id) is None
