"""AudioIngress — drains decoded uplink audio and fans it out (docs/06 §1, §3.2, §7).

The wire side (48 kHz Opus over SRTP) is already Opus-decoded by aiortc by
the time audio reaches this class: PeerSession (a later task, the only
aiortc importer per docs/06 §1.1) drains `track.recv()` and pushes each
decoded `av.AudioFrame` here. This module resamples to 16 kHz mono
linear16 and fans out to the two consumers docs/06 §1 fixes:

- (a) a contiguous PCM byte stream for STT — `stt_stream()`, shaped for
  `SttProvider.stream(audio=...)` (app/domain/voice.py);
- (b) exact 32 ms hops for the VAD — `vad_frames()`, `AudioFrame` values
  with an honest media-clock `ts_ms`.

Judgment calls, flagged per house style:

1. **Input seam.** `push()` takes an `av.AudioFrame`; `push_pcm()` is the
   raw-bytes seam (it builds an av frame and delegates), so tests and any
   future non-aiortc source can feed synthetic frames or plain PCM. Only
   `av` crosses into this module — aiortc stays in PeerSession.
2. **DTX gap threshold: 40 ms** (`GAP_INSERT_THRESHOLD_MS` — two lost
   20 ms wire frames). aiortc's jitter buffer plus Opus PLC already
   conceal single-frame loss, so a pts jump under 40 ms is jitter noise;
   at or above it, the gap is treated as DTX silence (docs/06 §7) and
   surfaced as inserted zero PCM so the trailing-silence clock sees real
   time. Sub-threshold jumps are absorbed (≤ 40 ms of clock compression,
   noise against the 250 ms endpoint threshold, docs/06 §5).
3. **Gap silence is synthesized at the 16 kHz output side**, not pushed
   through the resampler: silence is rate-invariant, and bypassing the
   resampler keeps its filter state continuous for real audio. Cost is
   sub-sample rounding per gap.
4. **Fan-out queues are bounded (~30 s) with drop-oldest + a warning.**
   Blocking on full would stall the real-time track drain in
   PeerSession; dropping newest would starve a recovering consumer of
   *live* audio. Oldest audio is the least valuable during a backlog
   (docs/06 §9 expects only "brief" buffering across an STT reconnect).
   Never silent: every drop logs.
5. **`ts_ms` derives from the emitted-sample count** (hop_index x 32 ms),
   which includes inserted gap silence — exactly the honest media clock
   docs/06 §7 requires. Frames pushed without a pts are treated as
   contiguous with the previous frame.
6. **A trailing partial hop (< 32 ms) at close is dropped**, not padded:
   a VAD probability over a zero-padded fraction of a hop is noise, and
   the stream is over anyway. The STT stream got those bytes already.
7. **One subscriber per fan-out, enforced at call time.** A second
   subscriber would silently steal frames (queue semantics), so
   `stt_stream()`/`vad_frames()` raise on re-subscription instead.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from fractions import Fraction
from typing import Final

import av

from app.domain.voice import AudioFrame
from app.obs.logging import get_logger

log = get_logger(__name__)

# docs/06 §3.2: Deepgram ingest format — 16 kHz mono linear16.
STT_SAMPLE_RATE: Final = 16_000
BYTES_PER_SAMPLE: Final = 2  # linear16 (s16le)

# docs/06 §5: Silero's native streaming hop — one decision per 32 ms
# frame (the 64-sample context prepend is internal to SileroVad).
VAD_HOP_MS: Final = 32
VAD_HOP_SAMPLES: Final = STT_SAMPLE_RATE * VAD_HOP_MS // 1000  # 512
VAD_HOP_BYTES: Final = VAD_HOP_SAMPLES * BYTES_PER_SAMPLE  # 1024

# Judgment call 2: pts jumps at/above this are DTX gaps -> inserted silence.
GAP_INSERT_THRESHOLD_MS: Final = 40

# Judgment call 4: ~30 s of backlog per fan-out before drop-oldest engages.
MAX_BACKLOG_SECONDS: Final = 30
MAX_QUEUED_STT_BYTES: Final = MAX_BACKLOG_SECONDS * STT_SAMPLE_RATE * BYTES_PER_SAMPLE
MAX_QUEUED_VAD_FRAMES: Final = MAX_BACKLOG_SECONDS * 1000 // VAD_HOP_MS

_MS_PER_S: Final = 1000


class AudioIngress:
    """Push-driven fan-out: PeerSession pushes decoded frames in; the STT
    byte stream and 32 ms VAD hops flow out as async iterators. Call
    `close()` once when the remote track ends."""

    def __init__(self) -> None:
        self._resampler: av.AudioResampler | None = None
        self._stt_queue: asyncio.Queue[bytes | None] = asyncio.Queue()
        self._vad_queue: asyncio.Queue[AudioFrame | None] = asyncio.Queue()
        self._stt_queued_bytes = 0
        self._vad_buffer = bytearray()
        self._hops_emitted = 0
        self._expected_next_ts_ms: float | None = None
        self._closed = False
        self._stt_subscribed = False
        self._vad_subscribed = False

    # ------------------------------------------------------------------
    # Input side
    # ------------------------------------------------------------------

    def push(self, frame: av.AudioFrame) -> None:
        """Ingest one decoded uplink frame (any input rate/layout av can
        resample — the wire reality is 48 kHz mono/stereo s16)."""
        if self._closed:
            raise RuntimeError("AudioIngress.push() after close()")
        if frame.sample_rate <= 0:
            raise ValueError("pushed av.AudioFrame has no sample_rate set")
        frame_ts_ms = self._frame_ts_ms(frame)
        if frame_ts_ms is None:
            # No pts: contiguous with whatever came before (judgment call 5).
            frame_ts_ms = self._expected_next_ts_ms or 0.0
        self._insert_gap_silence(frame_ts_ms)
        self._expected_next_ts_ms = (
            frame_ts_ms + frame.samples * _MS_PER_S / frame.sample_rate
        )
        if self._resampler is None:
            self._resampler = av.AudioResampler(
                format="s16", layout="mono", rate=STT_SAMPLE_RATE
            )
        for out in self._resampler.resample(frame):
            self._emit(_frame_bytes(out))

    def push_pcm(
        self,
        pcm: bytes,
        *,
        sample_rate: int,
        channels: int = 1,
        ts_ms: int | None = None,
    ) -> None:
        """Raw-PCM input seam (judgment call 1): interleaved s16le at
        `sample_rate`. `ts_ms` positions the chunk on the media clock;
        omit it for contiguous-with-previous."""
        if sample_rate <= 0:
            raise ValueError(f"sample_rate must be positive, got {sample_rate}")
        if channels not in (1, 2):
            raise ValueError(f"channels must be 1 or 2, got {channels}")
        frame_size = BYTES_PER_SAMPLE * channels
        if not pcm or len(pcm) % frame_size:
            raise ValueError(
                f"pcm length {len(pcm)} is not a positive multiple of {frame_size}"
            )
        layout = "mono" if channels == 1 else "stereo"
        samples = len(pcm) // frame_size
        frame = av.AudioFrame(format="s16", layout=layout, samples=samples)
        frame.sample_rate = sample_rate
        frame.time_base = Fraction(1, sample_rate)
        if ts_ms is not None:
            frame.pts = round(ts_ms * sample_rate / _MS_PER_S)
        frame.planes[0].update(pcm)
        self.push(frame)

    def close(self) -> None:
        """End of the remote track: flush the resampler tail, drop any
        partial VAD hop (judgment call 6), and end both fan-outs.
        Idempotent."""
        if self._closed:
            return
        self._closed = True
        if self._resampler is not None:
            for out in self._resampler.resample(None):
                self._emit(_frame_bytes(out))
            self._resampler = None
        if self._vad_buffer:
            log.debug(
                "audio_ingress.partial_hop_dropped_at_close",
                dropped_bytes=len(self._vad_buffer),
            )
            self._vad_buffer.clear()
        self._stt_queue.put_nowait(None)
        self._vad_queue.put_nowait(None)

    # ------------------------------------------------------------------
    # Fan-out side
    # ------------------------------------------------------------------

    def stt_stream(self) -> AsyncIterator[bytes]:
        """Fan-out (a): the contiguous 16 kHz mono linear16 byte stream —
        pass directly as `SttProvider.stream()`'s `audio`. Single
        subscriber (judgment call 7)."""
        if self._stt_subscribed:
            raise RuntimeError("stt_stream() already has a subscriber")
        self._stt_subscribed = True
        return self._drain_stt()

    def vad_frames(self) -> AsyncIterator[AudioFrame]:
        """Fan-out (b): exact 32 ms hops (512 samples / 1024 bytes at
        16 kHz) with honest `ts_ms`. Single subscriber (judgment call 7)."""
        if self._vad_subscribed:
            raise RuntimeError("vad_frames() already has a subscriber")
        self._vad_subscribed = True
        return self._drain_vad()

    async def _drain_stt(self) -> AsyncIterator[bytes]:
        while True:
            item = await self._stt_queue.get()
            if item is None:
                return
            self._stt_queued_bytes -= len(item)
            yield item

    async def _drain_vad(self) -> AsyncIterator[AudioFrame]:
        while True:
            item = await self._vad_queue.get()
            if item is None:
                return
            yield item

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    @staticmethod
    def _frame_ts_ms(frame: av.AudioFrame) -> float | None:
        if frame.pts is None or frame.time_base is None:
            return None
        return float(frame.pts * frame.time_base * _MS_PER_S)

    def _insert_gap_silence(self, frame_ts_ms: float) -> None:
        """docs/06 §7: a media-clock jump at/above the threshold surfaces
        as zero PCM so downstream trailing-silence math sees real time
        (judgment calls 2 and 3)."""
        if self._expected_next_ts_ms is None:
            return
        gap_ms = frame_ts_ms - self._expected_next_ts_ms
        if gap_ms < GAP_INSERT_THRESHOLD_MS:
            return
        gap_samples = round(gap_ms * STT_SAMPLE_RATE / _MS_PER_S)
        log.info(
            "audio_ingress.dtx_gap_silence_inserted",
            gap_ms=round(gap_ms),
            gap_samples=gap_samples,
        )
        self._emit(b"\x00" * (gap_samples * BYTES_PER_SAMPLE))

    def _emit(self, pcm: bytes) -> None:
        """Feed resampled 16 kHz mono bytes to both fan-outs."""
        if not pcm:
            return
        self._offer_stt(pcm)
        self._vad_buffer += pcm
        while len(self._vad_buffer) >= VAD_HOP_BYTES:
            hop = bytes(self._vad_buffer[:VAD_HOP_BYTES])
            del self._vad_buffer[:VAD_HOP_BYTES]
            self._offer_vad(
                AudioFrame(
                    pcm16=hop,
                    sample_rate=STT_SAMPLE_RATE,
                    samples=VAD_HOP_SAMPLES,
                    ts_ms=self._hops_emitted * VAD_HOP_MS,
                )
            )
            self._hops_emitted += 1

    def _offer_stt(self, pcm: bytes) -> None:
        self._stt_queued_bytes += len(pcm)
        self._stt_queue.put_nowait(pcm)
        while self._stt_queued_bytes > MAX_QUEUED_STT_BYTES and self._stt_queue.qsize() > 1:
            dropped = self._stt_queue.get_nowait()
            if dropped is not None:  # sentinel only ever arrives after close()
                self._stt_queued_bytes -= len(dropped)
                log.warning(
                    "audio_ingress.stt_backlog_dropped", dropped_bytes=len(dropped)
                )

    def _offer_vad(self, frame: AudioFrame) -> None:
        self._vad_queue.put_nowait(frame)
        while self._vad_queue.qsize() > MAX_QUEUED_VAD_FRAMES:
            dropped = self._vad_queue.get_nowait()
            if dropped is not None:
                log.warning("audio_ingress.vad_backlog_dropped", ts_ms=dropped.ts_ms)


def _frame_bytes(frame: av.AudioFrame) -> bytes:
    """Extract exactly the audio payload — av plane buffers can be padded
    past `samples * bytes_per_sample`, so slice, never take the whole
    plane."""
    return bytes(frame.planes[0])[: frame.samples * BYTES_PER_SAMPLE]


__all__ = [
    "GAP_INSERT_THRESHOLD_MS",
    "MAX_QUEUED_STT_BYTES",
    "MAX_QUEUED_VAD_FRAMES",
    "STT_SAMPLE_RATE",
    "VAD_HOP_BYTES",
    "VAD_HOP_MS",
    "VAD_HOP_SAMPLES",
    "AudioIngress",
]
