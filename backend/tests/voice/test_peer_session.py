"""PeerSession loopback tests — two REAL RTCPeerConnections in one process
(host candidates, no network): our PeerSession answers a raw aiortc fake
client, and the assertions cover the whole T3.1 deliverable: offer/answer
completes, uplink PCM actually lands in AudioIngress, egress-fed outbound
audio actually reaches the client, the `ctx` data channel round-trips both
directions, and close() leaves no pending tasks.

SKIP GUARD: skips only when the [voice] extra (aiortc/av) is absent —
CI's gates job installs `.[dev,voice]`, so these run on every CI pass
(same convention as tests/voice/test_silero.py).
"""

from __future__ import annotations

import pytest

pytest.importorskip("aiortc", reason="[voice] extra not installed")
pytest.importorskip("av", reason="[voice] extra not installed")

import array  # noqa: E402
import asyncio  # noqa: E402
import json  # noqa: E402
import math  # noqa: E402
from fractions import Fraction  # noqa: E402

import av  # noqa: E402
from aiortc import (  # noqa: E402
    RTCConfiguration,
    RTCPeerConnection,
    RTCSessionDescription,
)
from aiortc.mediastreams import MediaStreamError, MediaStreamTrack  # noqa: E402

from app.domain.voice import (  # noqa: E402
    DC_TYPE_AGENT_STATE,
    SIG_TYPE_ANSWER,
    SIG_TYPE_ICE,
    DataChannelMessage,
    SignalMessage,
    TtsChunk,
)
from app.voice.audio_egress import AudioEgress  # noqa: E402
from app.voice.audio_ingress import AudioIngress  # noqa: E402
from app.voice.peer_session import PeerSession, PeerSessionError  # noqa: E402

WIRE_RATE = 48_000
TTS_RATE = 24_000
HOST_CANDIDATE = "candidate:1 1 udp 2130706431 127.0.0.1 40000 typ host"


def _sine_pcm(sample_rate: int, duration_ms: int, *, freq: float = 440.0) -> bytes:
    """Deterministic s16le mono sine — loud enough to survive Opus."""
    total = sample_rate * duration_ms // 1000
    samples = array.array(
        "h", (int(12_000 * math.sin(2 * math.pi * freq * i / sample_rate)) for i in range(total))
    )
    return samples.tobytes()


def _has_energy(pcm16: bytes, *, threshold: int = 500) -> bool:
    samples = array.array("h")
    samples.frombytes(pcm16)
    return bool(samples) and max(abs(s) for s in samples) > threshold


def _frame_payload(frame: av.AudioFrame) -> bytes:
    return bytes(frame.planes[0])[: frame.samples * 2]


async def _noop_signal(message: SignalMessage) -> None:
    return None


async def _wait_for(predicate, *, timeout: float = 15.0, message: str) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.05)
    pytest.fail(f"timed out waiting for {message}")


class SineTrack(MediaStreamTrack):
    """The fake client's mic: real-time-paced 20 ms frames of a 440 Hz sine
    (a MediaStreamTrack must pace its own recv() — aiortc's sender does
    not; cf. peer_session judgment call 1)."""

    kind = "audio"

    def __init__(self) -> None:
        super().__init__()
        self._pcm = _sine_pcm(WIRE_RATE, 1000)  # 50 frames, loops cleanly
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
        frame = av.AudioFrame(format="s16", layout="mono", samples=960)
        frame.sample_rate = WIRE_RATE
        frame.time_base = Fraction(1, WIRE_RATE)
        frame.pts = self._frame_no * 960
        offset = (self._frame_no % 50) * 1920
        frame.planes[0].update(self._pcm[offset : offset + 1920])
        self._frame_no += 1
        return frame


class Loopback:
    """One PeerSession (ours) + one raw aiortc client peer, wired directly
    signal-for-signal (no WebSocket — signaling has its own tests)."""

    def __init__(self) -> None:
        self.signals: list[SignalMessage] = []
        self.received_data: list[str | bytes] = []
        self.ingress = AudioIngress()
        self.egress = AudioEgress()
        self.peer = PeerSession(
            "sess-loopback",
            ingress=self.ingress,
            send_signal=self._record_signal,
            ice_servers=(),
            on_data=self.received_data.append,
        )
        self.client = RTCPeerConnection(RTCConfiguration(iceServers=[]))
        self.client_frames: list[av.AudioFrame] = []
        self.client_messages: list[str | bytes] = []
        self.channel_open = asyncio.Event()
        self.channel = self.client.createDataChannel("ctx")
        self.channel.on("open", self.channel_open.set)
        self.channel.on("message", self.client_messages.append)
        self.client.addTrack(SineTrack())
        self.client.on("track", self._on_client_track)
        self._collect_tasks: list[asyncio.Task[None]] = []
        self.egress_task: asyncio.Task[None] | None = None

    async def _record_signal(self, message: SignalMessage) -> None:
        self.signals.append(message)

    def _on_client_track(self, track: MediaStreamTrack) -> None:
        self._collect_tasks.append(asyncio.create_task(self._collect_client_frames(track)))

    async def _collect_client_frames(self, track: MediaStreamTrack) -> None:
        try:
            while True:
                self.client_frames.append(await track.recv())
        except MediaStreamError:
            pass

    async def connect(self) -> None:
        offer = await self.client.createOffer()
        await self.client.setLocalDescription(offer)  # gathers ICE -> complete SDP
        await self.peer.handle_offer(self.client.localDescription.sdp)
        answer = next(m for m in self.signals if m.type == SIG_TYPE_ANSWER)
        await self.client.setRemoteDescription(
            RTCSessionDescription(sdp=answer.payload["sdp"], type="answer")
        )
        self.egress_task = asyncio.create_task(self.egress.run(self.peer.outbound_sink))

    async def close(self) -> None:
        await self.peer.close()
        self.egress.stop()
        if self.egress_task is not None:
            await asyncio.wait_for(self.egress_task, timeout=2)
        await self.client.close()
        for task in self._collect_tasks:
            task.cancel()
        await asyncio.gather(*self._collect_tasks, return_exceptions=True)


async def test_loopback_media_and_messages_flow():
    """The T3.1 acceptance test, end to end over one real peer pair."""
    lb = Loopback()
    vad_frames = []

    async def collect_vad() -> None:
        async for frame in lb.ingress.vad_frames():
            vad_frames.append(frame)

    vad_task = asyncio.create_task(collect_vad())
    try:
        await asyncio.wait_for(lb.connect(), timeout=15)

        # Offer/answer completed; answer carries the server candidates and
        # the explicit end-of-candidates marker follows (judgment call 3).
        answer = next(m for m in lb.signals if m.type == SIG_TYPE_ANSWER)
        assert "a=candidate" in answer.payload["sdp"]
        eoc = next(m for m in lb.signals if m.type == SIG_TYPE_ICE)
        assert eoc.payload == {"candidate": None, "sdpMid": None, "sdpMLineIndex": None}
        await _wait_for(
            lambda: lb.client.connectionState == "connected", message="client connected"
        )

        # Uplink: the client's sine actually lands in AudioIngress as PCM.
        await _wait_for(
            lambda: any(_has_energy(f.pcm16) for f in vad_frames),
            message="uplink PCM through AudioIngress",
        )

        # Downlink: TTS audio enqueued into AudioEgress reaches the client
        # through the paced sink -> _EgressTrack -> Opus -> client decode.
        baseline = len(lb.client_frames)
        lb.egress.enqueue(TtsChunk(pcm=_sine_pcm(TTS_RATE, 400), sentence_no=0))
        lb.egress.drain()
        await _wait_for(
            lambda: any(
                _has_energy(_frame_payload(f)) for f in lb.client_frames[baseline:]
            ),
            message="egress audio at the fake client",
        )

        # Data channel, client -> server: delivered to the on_data seam.
        await asyncio.wait_for(lb.channel_open.wait(), timeout=15)
        await _wait_for(lambda: lb.peer.data_channel_open, message="server channel open")
        lb.channel.send('{"v":1,"type":"ctx.event","seq":1,"ts":0,"payload":{}}')
        await _wait_for(lambda: bool(lb.received_data), message="client->server DC message")
        assert json.loads(lb.received_data[0])["type"] == "ctx.event"

        # Data channel, server -> client: a DataChannelMessage envelope.
        lb.peer.send_data_channel(
            DataChannelMessage(
                type=DC_TYPE_AGENT_STATE,
                seq=1,
                ts=1784536468900,
                payload={"state": "listening", "turn": 0},
            )
        )
        await _wait_for(lambda: bool(lb.client_messages), message="server->client DC message")
        assert json.loads(lb.client_messages[0]) == {
            "v": 1,
            "type": "agent.state",
            "seq": 1,
            "ts": 1784536468900,
            "payload": {"state": "listening", "turn": 0},
        }
    finally:
        await lb.close()

    # close() ends the uplink drain and closes ingress, so the VAD fan-out
    # iterator terminates on its own — no dangling consumer.
    await asyncio.wait_for(vad_task, timeout=5)
    lingering = [
        t
        for t in asyncio.all_tasks()
        if t.get_name().startswith("peer-session:") and not t.done()
    ]
    assert lingering == [], f"leaked peer-session tasks: {lingering}"


async def test_close_is_idempotent_and_offer_after_close_raises():
    peer = PeerSession("sess-close", ingress=AudioIngress(), send_signal=_noop_signal)
    await peer.close()
    await peer.close()  # second close is a no-op, not an error
    with pytest.raises(PeerSessionError):
        await peer.handle_offer("v=0")


async def test_ice_before_offer_raises():
    peer = PeerSession("sess-ice", ingress=AudioIngress(), send_signal=_noop_signal)
    try:
        with pytest.raises(PeerSessionError):
            await peer.handle_remote_ice(
                {"candidate": HOST_CANDIDATE, "sdpMid": "0", "sdpMLineIndex": 0}
            )
    finally:
        await peer.close()


async def test_end_of_candidates_marker_is_a_noop():
    peer = PeerSession("sess-eoc", ingress=AudioIngress(), send_signal=_noop_signal)
    try:
        await peer.handle_remote_ice(
            {"candidate": None, "sdpMid": None, "sdpMLineIndex": None}
        )
    finally:
        await peer.close()


async def test_ice_without_mid_or_mline_index_raises():
    peer = PeerSession("sess-mid", ingress=AudioIngress(), send_signal=_noop_signal)
    try:
        with pytest.raises(PeerSessionError):
            await peer.handle_remote_ice(
                {"candidate": HOST_CANDIDATE, "sdpMid": None, "sdpMLineIndex": None}
            )
    finally:
        await peer.close()


async def test_outbound_sink_rejects_wrong_frame_length():
    peer = PeerSession("sess-sink", ingress=AudioIngress(), send_signal=_noop_signal)
    try:
        with pytest.raises(ValueError, match="1920"):
            peer.outbound_sink(b"\x00" * 100)
    finally:
        await peer.close()


async def test_send_data_channel_before_open_raises():
    peer = PeerSession("sess-dc", ingress=AudioIngress(), send_signal=_noop_signal)
    try:
        assert not peer.data_channel_open
        with pytest.raises(PeerSessionError):
            peer.send_data_channel(
                DataChannelMessage(
                    type=DC_TYPE_AGENT_STATE,
                    seq=1,
                    ts=0,
                    payload={"state": "listening", "turn": 0},
                )
            )
    finally:
        await peer.close()


async def test_egress_track_drop_oldest_keeps_newest_and_never_drops_sentinel():
    """Judgment call 2 in isolation: pushing past the cap with nothing
    draining drops the OLDEST frames (newest audio survives), and the end
    sentinel survives any amount of overflow after end()."""
    from app.voice.audio_egress import FRAME_BYTES
    from app.voice.peer_session import OUTBOUND_MAX_BUFFERED_FRAMES, _EgressTrack

    track = _EgressTrack()
    total = OUTBOUND_MAX_BUFFERED_FRAMES + 20
    for i in range(total):
        track.push(i.to_bytes(2, "little") * (FRAME_BYTES // 2))
    assert track._queue.qsize() == OUTBOUND_MAX_BUFFERED_FRAMES

    # The oldest 20 were dropped: the head is frame #20, not #0.
    head = await asyncio.wait_for(track.recv(), timeout=1)
    head_payload = _frame_payload(head)
    assert head_payload[:2] == (20).to_bytes(2, "little")

    # end() while overflowing: the sentinel must survive drop-oldest.
    track.end()
    for _ in range(OUTBOUND_MAX_BUFFERED_FRAMES - 1):
        track._queue.get_nowait()
    assert track._queue.get_nowait() is None, "end sentinel was dropped by overflow"


async def test_egress_track_stall_episode_counter_resets_on_drain():
    """The pre-connect window is a normal ~sub-second occurrence — the
    warning is per stall episode (counter tracks the episode; one log line
    when it opens, an info summary with the count when it drains), never
    per dropped frame."""
    from app.voice.audio_egress import FRAME_BYTES
    from app.voice.peer_session import OUTBOUND_MAX_BUFFERED_FRAMES, _EgressTrack

    track = _EgressTrack()
    for _ in range(OUTBOUND_MAX_BUFFERED_FRAMES + 40):
        track.push(b"\x00" * FRAME_BYTES)
    assert track._backlog_dropped_frames == 40
    # Draining one frame ends the episode and resets the counter.
    await asyncio.wait_for(track.recv(), timeout=1)
    assert track._backlog_dropped_frames == 0
    track.end()
