"""PeerSession — one aiortc RTCPeerConnection per call, and the ONLY module
in the codebase allowed to import aiortc (docs/06 §1.1's confinement rule;
grep-enforced in review). It mirrors, never initiates (docs/06 §2: the
client always offers): applies the remote offer, answers, terminates the
uplink audio into AudioIngress, feeds the downlink track from AudioEgress,
and carries DataChannelMessage envelopes on the client-opened `ctx` channel.

Judgment calls, flagged per house style:

1. **recv()-pull vs paced-push — exactly ONE clock, and it is AudioEgress's.**
   aiortc's RTCRtpSender does not pace itself: it loops `await track.recv()`
   and encodes/sends each frame the moment recv() returns, expecting the
   *track* to impose real time (aiortc's own AudioStreamTrack sleeps inside
   recv()). AudioEgress, per its frozen contract, is *already* a pacer: its
   run() loop hands the sink exactly one 1920-byte frame per 20 ms of wall
   clock on an absolute deadline ladder. Two independent pacers would fight
   (drift, bunching, starvation), so the outbound `_EgressTrack` here never
   sleeps and never looks at a clock: its recv() simply awaits the next
   frame on an asyncio.Queue that the synchronous `outbound_sink` (the
   callable handed to `AudioEgress.run()`) fills. The cadence recv() is
   contractually supposed to enforce is therefore *inherited* from the
   queue's arrival rate — the sender blocks ~20 ms per frame because frames
   only EXIST every 20 ms. Underrun cannot starve the sender into missing
   RTP because AudioEgress emits silence frames on underrun (docs/06 §7);
   the queue's steady-state depth is 0–1 frames, so the synthesis lead
   stays inside AudioEgress where the barge-in flush can drop it (§6.4) —
   buffering ahead here would smear the flush target across two layers.
2. **The outbound queue is capped (~1 s, drop-oldest, warn once per
   stall episode).** If the sender stalls (teardown race, pathological
   encoder hiccup) the paced sink keeps producing 50 frames/s; blocking
   is impossible (the sink is synchronous by egress contract) and
   unbounded growth is a leak. Oldest audio is the least valuable during
   a stall — same reasoning as AudioIngress judgment call 4. The cap IS
   reachable in one normal window: between the caller starting
   `AudioEgress.run()` (run.py starts it on offer arrival) and the
   RTCRtpSender actually pulling once ICE/DTLS complete — everything
   dropped there is pre-connect pacing silence, which is exactly why
   drop-oldest is harmless and why the warning is per-episode, not
   per-frame (a sub-second setup window must not emit 50 log lines).
3. **Server-side "trickle" is answer-embedded.** aiortc gathers ICE during
   `setLocalDescription()` (it has no trickle emitter), so the answer SDP
   already carries every server candidate; after sending the answer this
   class emits one `ice` envelope with `"candidate": null` (docs/13 §6's
   end-of-candidates marker) so the wire flow still matches the §2 shape.
   Client→server trickle is real: each `ice` envelope is applied to the
   peer connection as it arrives. Setup latency is unhurt — the server is
   on a fast network and gathers host/srflx in milliseconds; trickle
   matters for the *mobile* side, which docs/06 §2 already fixes.
4. **ICE servers arrive as the frozen domain `IceServer`, not aiortc
   types**, converted internally — callers (signaling, run) never touch an
   aiortc symbol. An empty sequence means NO ice servers (host candidates
   only), explicitly overriding aiortc's surprising default of Google's
   public STUN — the demo's media path must not silently depend on an
   external service nobody configured (docs/06 §10's demo honesty).
5. **`candidate: null` from the client is a no-op.** aiortc needs no
   explicit end-of-candidates signal to complete checks, and
   `addIceCandidate` requires a real candidate — so the marker is logged
   and dropped rather than force-fed.
6. **One audio track, one data channel.** The offer contract (docs/06 §2)
   is one mic track + one `ctx` channel; a second of either is logged and
   ignored rather than wired, because AudioIngress is single-source and
   the `ctx` protocol is single-channel by canon.
7. **Inbound data-channel messages default to log-and-ignore.** The `ctx.*`
   uplink protocol is Phase 4 (docs/13 §4); until then the docs/13 §9
   ignore-unknown rule applies to everything. The `on_data` constructor
   seam exists so that task plugs in a parser without touching this file.
8. **`close()` is idempotent and ordered**: end the outbound track first
   (sentinel unblocks a pending recv(), then stop() — so a still-pacing
   egress drops frames instead of queueing into a corpse), cancel the
   uplink drain task, close the channel and peer connection, then close
   ingress (idempotent; also covers the no-track-ever-arrived case).

Docs: docs/06 §1.1/§2/§7 (roles, trickle, pacing), docs/13 §6 (envelopes).
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping, Sequence
from fractions import Fraction
from typing import Any, Final

import av
from aiortc import (
    RTCConfiguration,
    RTCIceServer,
    RTCPeerConnection,
    RTCSessionDescription,
)
from aiortc.mediastreams import MediaStreamError, MediaStreamTrack
from aiortc.sdp import candidate_from_sdp

from app.domain.voice import (
    SIG_TYPE_ANSWER,
    SIG_TYPE_ICE,
    DataChannelMessage,
    IceServer,
    SignalMessage,
)
from app.obs.logging import get_logger
from app.voice.audio_egress import FRAME_BYTES, FRAME_SAMPLES, WIRE_SAMPLE_RATE
from app.voice.audio_ingress import AudioIngress

log = get_logger(__name__)

# Same shape as signaling.SendSignal — duplicated on purpose: importing it
# from signaling (or vice versa) would couple the two modules for a two-line
# alias, and the domain contract (app/domain/voice.py) is frozen.
SendSignal = Callable[[SignalMessage], Awaitable[None]]
OnData = Callable[[str | bytes], None]

# Judgment call 2: ~1 s of 20 ms frames before drop-oldest engages.
OUTBOUND_MAX_BUFFERED_FRAMES: Final = 50


class PeerSessionError(RuntimeError):
    """A peer-connection operation was invalid in the session's current
    state (ice before offer, send on an unopened channel, use after close).
    """


class _EgressTrack(MediaStreamTrack):
    """The outbound audio track — the passive half of judgment call 1.

    `push()` is fed by the paced egress sink; `recv()` awaits the queue and
    stamps a monotonic pts (960 samples per frame @ 48 kHz) so aiortc's RTP
    timestamps track the media clock exactly. Never sleeps: the arrival
    cadence IS the pacing.
    """

    kind = "audio"

    def __init__(self) -> None:
        super().__init__()
        self._queue: asyncio.Queue[bytes | None] = asyncio.Queue()
        self._pts = 0
        self._push_after_end_logged = False
        self._backlog_dropped_frames = 0

    def push(self, frame: bytes) -> None:
        if self.readyState != "live":
            if not self._push_after_end_logged:
                self._push_after_end_logged = True
                log.debug("peer_session.outbound_push_after_end")
            return
        self._queue.put_nowait(frame)
        while self._queue.qsize() > OUTBOUND_MAX_BUFFERED_FRAMES:
            dropped = self._queue.get_nowait()
            if dropped is None:  # never drop the end sentinel
                self._queue.put_nowait(None)
                break
            # Judgment call 2: one warning per stall episode, not per frame.
            if self._backlog_dropped_frames == 0:
                log.warning("peer_session.outbound_backlog_stall_started")
            self._backlog_dropped_frames += 1

    def end(self) -> None:
        """Sentinel first (unblocks a recv() already awaiting the queue),
        then stop() so subsequent recv() calls fail fast."""
        self._queue.put_nowait(None)
        self.stop()

    async def recv(self) -> av.AudioFrame:
        if self.readyState != "live":
            raise MediaStreamError
        item = await self._queue.get()
        if item is None:
            raise MediaStreamError
        if self._backlog_dropped_frames:
            # The sender is pulling again: the stall episode is over.
            log.info(
                "peer_session.outbound_backlog_stall_ended",
                dropped_frames=self._backlog_dropped_frames,
            )
            self._backlog_dropped_frames = 0
        frame = av.AudioFrame(format="s16", layout="mono", samples=FRAME_SAMPLES)
        frame.sample_rate = WIRE_SAMPLE_RATE
        frame.time_base = Fraction(1, WIRE_SAMPLE_RATE)
        frame.pts = self._pts
        self._pts += FRAME_SAMPLES
        frame.planes[0].update(item)
        return frame


class PeerSession:
    """Owns one RTCPeerConnection for one call (docs/06 §1.1's table row).

    Wiring: `handle_offer`/`handle_remote_ice` are driven by the signaling
    server; `send_signal` carries the answer and end-of-candidates back;
    the remote audio track drains into `ingress`; `outbound_sink` is the
    synchronous callable to hand to `AudioEgress.run()`.
    """

    def __init__(
        self,
        session_id: str,
        *,
        ingress: AudioIngress,
        send_signal: SendSignal,
        ice_servers: Sequence[IceServer] = (),
        on_data: OnData | None = None,
    ) -> None:
        if not session_id:
            raise ValueError("session_id must be non-empty")
        self._session_id = session_id
        self._ingress = ingress
        self._send_signal = send_signal
        self._on_data = on_data
        self._closed = False
        self._sender: object | None = None
        self._channel: Any = None
        self._track_task: asyncio.Task[None] | None = None
        self._outbound = _EgressTrack()
        self._pc = RTCPeerConnection(
            # Judgment call 4: empty config means host-only, never aiortc's
            # implicit Google-STUN default.
            RTCConfiguration(iceServers=[self._to_rtc_ice_server(s) for s in ice_servers])
        )
        self._pc.on("track", self._on_track)
        self._pc.on("datachannel", self._on_datachannel)
        self._pc.on("connectionstatechange", self._on_connection_state_change)

    @staticmethod
    def _to_rtc_ice_server(server: IceServer) -> RTCIceServer:
        return RTCIceServer(
            urls=list(server.urls),
            username=server.username,
            credential=server.credential,
        )

    # ------------------------------------------------------------------
    # Signaling-driven surface (docs/06 §2, docs/13 §6)
    # ------------------------------------------------------------------

    async def handle_offer(self, sdp: str) -> None:
        """Apply a client offer (initial or ICE-restart re-offer — same
        path, docs/13 §6.1) and send the answer + end-of-candidates marker
        via `send_signal` (judgment call 3)."""
        if self._closed:
            raise PeerSessionError("handle_offer() after close()")
        if not sdp:
            raise PeerSessionError("offer sdp is empty")
        await self._pc.setRemoteDescription(RTCSessionDescription(sdp=sdp, type="offer"))
        if self._sender is None:  # re-offers must not add a second track
            self._sender = self._pc.addTrack(self._outbound)
        answer = await self._pc.createAnswer()
        await self._pc.setLocalDescription(answer)  # gathers ICE; SDP carries candidates
        await self._send_signal(
            SignalMessage(type=SIG_TYPE_ANSWER, payload={"sdp": self._pc.localDescription.sdp})
        )
        await self._send_signal(
            SignalMessage(
                type=SIG_TYPE_ICE,
                payload={"candidate": None, "sdpMid": None, "sdpMLineIndex": None},
            )
        )
        log.info("peer_session.answer_sent", session_id=self._session_id)

    async def handle_remote_ice(self, payload: Mapping[str, Any]) -> None:
        """Apply one client-trickled candidate (docs/13 §6's `ice` payload).
        `candidate: null` is the end-of-candidates marker — a no-op here
        (judgment call 5)."""
        if self._closed:
            raise PeerSessionError("handle_remote_ice() after close()")
        candidate = payload.get("candidate")
        if candidate is None:
            log.debug("peer_session.end_of_candidates", session_id=self._session_id)
            return
        if not isinstance(candidate, str) or not candidate:
            raise PeerSessionError("ice payload 'candidate' must be a non-empty string or null")
        if self._pc.remoteDescription is None:
            raise PeerSessionError("ice candidate before any offer")
        sdp_mid = payload.get("sdpMid")
        sdp_mline_index = payload.get("sdpMLineIndex")
        if sdp_mid is None and sdp_mline_index is None:
            raise PeerSessionError("ice payload needs sdpMid or sdpMLineIndex")
        ice = candidate_from_sdp(candidate.removeprefix("candidate:"))
        ice.sdpMid = sdp_mid
        ice.sdpMLineIndex = sdp_mline_index
        await self._pc.addIceCandidate(ice)

    # ------------------------------------------------------------------
    # Media plumbing
    # ------------------------------------------------------------------

    def outbound_sink(self, frame: bytes) -> None:
        """The synchronous sink for `AudioEgress.run()` — judgment call 1's
        push side. Exact 20 ms / 1920-byte frames only (egress contract)."""
        if len(frame) != FRAME_BYTES:
            raise ValueError(f"outbound frame must be {FRAME_BYTES} bytes, got {len(frame)}")
        self._outbound.push(frame)

    def _on_track(self, track: MediaStreamTrack) -> None:
        if track.kind != "audio":
            log.warning("peer_session.non_audio_track_ignored", kind=track.kind)
            return
        if self._track_task is not None:
            log.warning("peer_session.extra_audio_track_ignored", session_id=self._session_id)
            return
        log.info("peer_session.audio_track_received", session_id=self._session_id)
        self._track_task = asyncio.create_task(
            self._drain_track(track), name=f"peer-session:drain:{self._session_id}"
        )

    async def _drain_track(self, track: MediaStreamTrack) -> None:
        """Drain `track.recv()` into AudioIngress until the track ends
        (docs/06 §1: PS → ING). Ends — and closes ingress — on the track's
        MediaStreamError or on cancellation from close()."""
        try:
            while True:
                frame = await track.recv()
                # track.recv() is typed Frame | Packet across aiortc's
                # audio/video tracks generically; _on_track only ever
                # dispatches an audio track here, so this is always an
                # av.AudioFrame in practice — narrow explicitly rather than
                # widen AudioIngress.push()'s frozen-contract signature.
                if not isinstance(frame, av.AudioFrame):
                    raise PeerSessionError(f"audio track yielded non-audio frame {type(frame)!r}")
                self._ingress.push(frame)
        except MediaStreamError:
            log.info("peer_session.remote_track_ended", session_id=self._session_id)
        finally:
            self._ingress.close()

    # ------------------------------------------------------------------
    # Data channel (docs/13 §4 — client-opened `ctx`)
    # ------------------------------------------------------------------

    @property
    def data_channel_open(self) -> bool:
        return self._channel is not None and self._channel.readyState == "open"

    def send_data_channel(self, message: DataChannelMessage) -> None:
        """Send one server→client envelope (transcript.*, agent.state).
        Fail-loud when the channel is not open — the caller owns lifecycle
        and must not silently drop captions."""
        if not self.data_channel_open:
            raise PeerSessionError("ctx data channel is not open")
        self._channel.send(message.model_dump_json())

    def _on_datachannel(self, channel: Any) -> None:
        if self._channel is not None:
            log.warning(
                "peer_session.extra_datachannel_ignored",
                session_id=self._session_id,
                label=channel.label,
            )
            return
        self._channel = channel
        log.info(
            "peer_session.datachannel_open", session_id=self._session_id, label=channel.label
        )
        channel.on("message", self._on_channel_message)

    def _on_channel_message(self, message: str | bytes) -> None:
        if self._on_data is not None:
            self._on_data(message)
            return
        # Judgment call 7 / docs/13 §9: nothing consumes ctx.* until the
        # Phase-4 capture task lands — ignore, never error.
        log.debug(
            "peer_session.datachannel_message_ignored",
            session_id=self._session_id,
            size=len(message),
        )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def _on_connection_state_change(self) -> None:
        log.info(
            "peer_session.connection_state",
            session_id=self._session_id,
            state=self._pc.connectionState,
        )

    async def close(self) -> None:
        """Tear down tracks, tasks, channel, and peer connection without
        leaks. Idempotent — every disconnect path converges here
        (judgment call 8)."""
        if self._closed:
            return
        self._closed = True
        self._outbound.end()
        if self._track_task is not None:
            self._track_task.cancel()
            await asyncio.gather(self._track_task, return_exceptions=True)
        if self._channel is not None:
            self._channel.close()
        await self._pc.close()
        self._ingress.close()
        log.info("peer_session.closed", session_id=self._session_id)


__all__ = [
    "OUTBOUND_MAX_BUFFERED_FRAMES",
    "OnData",
    "PeerSession",
    "PeerSessionError",
    "SendSignal",
]
