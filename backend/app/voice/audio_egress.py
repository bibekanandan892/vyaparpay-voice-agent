"""AudioEgress — the paced playout queue between burst-speed TTS and the
20 ms real-time cadence the outbound track demands (docs/06 §6.4, §7).

TTS chunks (24 kHz mono s16, `TtsChunk.pcm`) land here as fast as
ElevenLabs streams them; the output side hands exactly one 20 ms 48 kHz
frame to the sink per 20 ms of wall-clock time. The queue in between IS
the synthesis lead docs/06 §6.4 talks about — which makes it the single
barge-in flush target, and makes `pause()`/`resume()`/`flush()` the §6.1
duck/commit primitives.

Judgment calls, flagged per house style:

1. **Injectable clock.** `now()` and `sleep()` are constructor seams;
   defaults are the running event loop's clock. Tests run the full pacing
   loop on a fake clock with zero real sleeping.
2. **The sink is a synchronous callable** receiving exact 1920-byte
   frames. PeerSession's sink just appends to its AudioStreamTrack
   buffer; an async sink would let a slow consumer silently skew the
   cadence this class exists to protect.
3. **Absolute deadline ladder, not relative sleeps.** `next_deadline +=
   20 ms` each frame, so scheduling wobble never accumulates as drift —
   a late wakeup catches up instead of shifting every later frame.
4. **`played_ms` counts only genuine audio actually handed to the sink**
   — pause silence and underrun silence are excluded, and a partial
   (zero-padded) frame counts only its real samples. This is the input
   to docs/06 §6.3's played-ms -> character-offset truncation mapping.
5. **`flush()` rebuilds the resampler.** PyAV's AudioResampler is
   single-use once flushed (probed on av 17.1: EOFError on reuse), so
   dropping the object is both correct and exactly flush semantics — the
   ~1 ms tail it holds belongs to the audio being dropped.
6. **`drain()` exists because the resampler withholds a small tail**
   (~32 samples at 48 kHz, ~0.7 ms, filter delay). End-of-turn callers
   drain it so a reply's last millisecond actually plays; the tail is
   attributed to the last enqueued sentence. For the same reason,
   per-sentence byte accounting at chunk boundaries is honest to within
   that same ~0.7 ms.
7. **The queue is unbounded on purpose** — docs/06 §6.4: the queue is
   the synthesis lead and stays in one place precisely so the barge-in
   flush is one queue drop. It is bounded in practice by sentence-level
   dispatch (docs/06 §4.2) and `flush()`; an enqueue-side cap would push
   backpressure into the TTS stream reader, which docs/06 §9 handles at
   a different layer (per-sentence retry policy).
8. **`pause()` retains the queue byte-exact** and keeps the cadence
   emitting silence — a false-positive barge-in resume (docs/06 §6.1
   phase 2) is therefore free: nothing was cancelled, nothing dropped.
9. **`flush()` does not resume.** Duck (`pause()`) and commit (`flush()`)
   are separate signals in docs/06 §6.1, and the state machine that
   knows whether the turn is over lives in the worker — this class never
   guesses at it.
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from fractions import Fraction
from typing import Final

import av

from app.domain.voice import TtsChunk
from app.obs.logging import get_logger

log = get_logger(__name__)

# docs/06 §3.2: ElevenLabs Flash streams PCM 24 kHz mono.
TTS_SAMPLE_RATE: Final = 24_000
# docs/06 §3.1: the wire is 48 kHz, 20 ms frames.
WIRE_SAMPLE_RATE: Final = 48_000
FRAME_MS: Final = 20
FRAME_SAMPLES: Final = WIRE_SAMPLE_RATE * FRAME_MS // 1000  # 960
BYTES_PER_SAMPLE: Final = 2  # s16le
FRAME_BYTES: Final = FRAME_SAMPLES * BYTES_PER_SAMPLE  # 1920
FRAME_INTERVAL_S: Final = FRAME_MS / 1000

SILENCE_FRAME: Final = b"\x00" * FRAME_BYTES

_MS_PER_S: Final = 1000

Clock = Callable[[], float]
SleepFn = Callable[[float], Awaitable[None]]
Sink = Callable[[bytes], None]


def _default_now() -> float:
    return asyncio.get_running_loop().time()


async def _default_sleep(delay: float) -> None:
    await asyncio.sleep(delay)


@dataclass(frozen=True)
class FlushResult:
    """What one `flush()` (barge-in commit, docs/06 §6.4) dropped.

    `played_ms` is the counter's value at flush time (the §6.3 truncation
    input); `dropped_sentences` is `(sentence_no, dropped_ms)` per queued
    sentence in playout order — a partially played sentence reports only
    its unplayed remainder. Per-sentence values round down individually,
    so their sum can undershoot `dropped_ms` by < 1 ms per sentence."""

    played_ms: int
    dropped_ms: int
    dropped_sentences: tuple[tuple[int, int], ...]


@dataclass
class _Segment:
    """Mutable per-sentence byte accounting for the pending queue —
    internal working state, never returned (the immutability rule
    protects what this module emits: `FlushResult` stays frozen)."""

    sentence_no: int
    remaining_bytes: int


class AudioEgress:
    """Enqueue side is burst-speed and synchronous; `run()` is the paced
    loop the worker tasks once per call, feeding the sink one 20 ms frame
    per 20 ms forever until `stop()`."""

    def __init__(self, *, now: Clock | None = None, sleep: SleepFn | None = None) -> None:
        self._now: Clock = now if now is not None else _default_now
        self._sleep: SleepFn = sleep if sleep is not None else _default_sleep
        self._pending = bytearray()
        self._segments: deque[_Segment] = deque()
        self._resampler: av.AudioResampler | None = None
        self._resampler_in_samples = 0
        self._last_sentence_no: int | None = None
        self._played_samples = 0
        self._paused = False
        self._stopped = False
        self._running = False

    # ------------------------------------------------------------------
    # Enqueue side (burst speed)
    # ------------------------------------------------------------------

    def enqueue(self, chunk: TtsChunk) -> None:
        """Upsample one TTS chunk to the wire rate and queue it for paced
        playout. Chunks for one sentence must arrive contiguously (they
        do: one `TtsProvider.synthesize()` stream per sentence)."""
        if len(chunk.pcm) % BYTES_PER_SAMPLE:
            raise ValueError(f"TtsChunk.pcm length {len(chunk.pcm)} is not s16-aligned")
        if not chunk.pcm:
            return
        samples = len(chunk.pcm) // BYTES_PER_SAMPLE
        frame = av.AudioFrame(format="s16", layout="mono", samples=samples)
        frame.sample_rate = TTS_SAMPLE_RATE
        frame.time_base = Fraction(1, TTS_SAMPLE_RATE)
        frame.pts = self._resampler_in_samples
        self._resampler_in_samples += samples
        frame.planes[0].update(chunk.pcm)
        if self._resampler is None:
            self._resampler = av.AudioResampler(
                format="s16", layout="mono", rate=WIRE_SAMPLE_RATE
            )
        added = 0
        for out in self._resampler.resample(frame):
            data = _frame_bytes(out)
            self._pending += data
            added += len(data)
        self._track_segment(chunk.sentence_no, added)
        self._last_sentence_no = chunk.sentence_no

    def drain(self) -> None:
        """Flush the resampler's held tail into the queue (judgment call
        6) — call when a turn's synthesis is complete so the last ~1 ms
        plays. The resampler is rebuilt lazily on the next enqueue."""
        if self._resampler is None:
            return
        added = 0
        for out in self._resampler.resample(None):
            data = _frame_bytes(out)
            self._pending += data
            added += len(data)
        self._resampler = None
        self._resampler_in_samples = 0
        if added and self._last_sentence_no is not None:
            self._track_segment(self._last_sentence_no, added)

    # ------------------------------------------------------------------
    # Barge-in primitives (docs/06 §6.1/§6.4)
    # ------------------------------------------------------------------

    def pause(self) -> None:
        """Duck (§6.1 phase 1): cadence continues with silence, queue and
        played_ms untouched. Reversible for free via `resume()`."""
        self._paused = True

    def resume(self) -> None:
        """Un-duck: playout continues from exactly where it paused."""
        self._paused = False

    def flush(self) -> FlushResult:
        """Commit (§6.1 phase 2): drop exactly the queued-but-unplayed
        audio — including the resampler's undelivered tail (judgment call
        5) — and report what was dropped, per sentence."""
        dropped_sentences = tuple(
            (seg.sentence_no, _bytes_to_ms(seg.remaining_bytes)) for seg in self._segments
        )
        result = FlushResult(
            played_ms=self.played_ms,
            dropped_ms=_bytes_to_ms(len(self._pending)),
            dropped_sentences=dropped_sentences,
        )
        self._pending.clear()
        self._segments.clear()
        self._resampler = None
        self._resampler_in_samples = 0
        log.info(
            "audio_egress.flushed",
            dropped_ms=result.dropped_ms,
            played_ms=result.played_ms,
            dropped_sentence_count=len(result.dropped_sentences),
        )
        return result

    @property
    def played_ms(self) -> int:
        """Milliseconds of genuine audio handed to the sink (judgment
        call 4) — the docs/06 §6.3 truncation-mapping input."""
        return self._played_samples * _MS_PER_S // WIRE_SAMPLE_RATE

    # ------------------------------------------------------------------
    # Paced output side
    # ------------------------------------------------------------------

    async def run(self, sink: Sink) -> None:
        """Emit one 20 ms frame to `sink` per 20 ms of wall clock until
        `stop()`. Underrun and pause both emit silence — the cadence
        never starves the track (docs/06 §7)."""
        if self._running:
            raise RuntimeError("AudioEgress.run() is already running")
        self._running = True
        self._stopped = False
        try:
            next_deadline = self._now()
            while not self._stopped:
                delay = next_deadline - self._now()
                await self._sleep(delay if delay > 0 else 0.0)
                if self._stopped:
                    return
                sink(self._next_frame())
                next_deadline += FRAME_INTERVAL_S
        finally:
            self._running = False

    def stop(self) -> None:
        """End the `run()` loop at its next wakeup (end of call)."""
        self._stopped = True

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _next_frame(self) -> bytes:
        if self._paused or not self._pending:
            return SILENCE_FRAME
        take = min(FRAME_BYTES, len(self._pending))
        chunk = bytes(self._pending[:take])
        del self._pending[:take]
        self._consume_segments(take)
        self._played_samples += take // BYTES_PER_SAMPLE
        if take < FRAME_BYTES:
            chunk += b"\x00" * (FRAME_BYTES - take)
        return chunk

    def _track_segment(self, sentence_no: int, nbytes: int) -> None:
        if nbytes <= 0:
            return
        if self._segments and self._segments[-1].sentence_no == sentence_no:
            self._segments[-1].remaining_bytes += nbytes
        else:
            self._segments.append(_Segment(sentence_no=sentence_no, remaining_bytes=nbytes))

    def _consume_segments(self, nbytes: int) -> None:
        remaining = nbytes
        while remaining > 0 and self._segments:
            head = self._segments[0]
            take = min(head.remaining_bytes, remaining)
            head.remaining_bytes -= take
            remaining -= take
            if head.remaining_bytes == 0:
                self._segments.popleft()


def _bytes_to_ms(nbytes: int) -> int:
    return nbytes // BYTES_PER_SAMPLE * _MS_PER_S // WIRE_SAMPLE_RATE


def _frame_bytes(frame: av.AudioFrame) -> bytes:
    """Slice exactly the audio payload — av plane buffers can be padded
    past `samples * bytes_per_sample`."""
    return bytes(frame.planes[0])[: frame.samples * BYTES_PER_SAMPLE]


__all__ = [
    "FRAME_BYTES",
    "FRAME_INTERVAL_S",
    "FRAME_MS",
    "FRAME_SAMPLES",
    "SILENCE_FRAME",
    "TTS_SAMPLE_RATE",
    "WIRE_SAMPLE_RATE",
    "AudioEgress",
    "FlushResult",
]
