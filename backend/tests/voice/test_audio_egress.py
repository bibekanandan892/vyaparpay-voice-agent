"""AudioEgress tests (docs/06 §6.4, §7) — the full pacing loop runs on a
fake clock: zero real sleeping, deterministic 20 ms boundaries.

Covers: exact 20 ms cadence, underrun -> silence (never starvation, and
the cadence holds through it), pause/resume (duck semantics — queue
retained byte-exact), flush dropping exactly the unplayed remainder with
per-sentence accounting, played_ms honesty (partial frames, pause,
underrun), and 24 kHz -> 48 kHz upsample correctness.
"""

from __future__ import annotations

import asyncio
import math
import struct
from fractions import Fraction

import pytest

av = pytest.importorskip("av")

from app.domain.voice import TtsChunk  # noqa: E402
from app.voice.audio_egress import (  # noqa: E402
    FRAME_BYTES,
    FRAME_INTERVAL_S,
    SILENCE_FRAME,
    TTS_SAMPLE_RATE,
    WIRE_SAMPLE_RATE,
    AudioEgress,
)

SINE_FREQ_HZ = 440.0
SINE_AMPLITUDE = 12_000
SINE_RMS = SINE_AMPLITUDE / math.sqrt(2)


class FakeClock:
    """now()/sleep() seam: sleeping advances fake time instantly and
    yields once so the paced loop interleaves deterministically with the
    test body — no real time passes anywhere."""

    def __init__(self) -> None:
        self.t = 0.0

    def now(self) -> float:
        return self.t

    async def sleep(self, delay: float) -> None:
        assert delay >= 0, f"egress asked for a negative sleep: {delay}"
        self.t += delay
        await asyncio.sleep(0)


class RecordingSink:
    def __init__(self, clock: FakeClock) -> None:
        self._clock = clock
        self.frames: list[tuple[float, bytes]] = []

    def __call__(self, frame: bytes) -> None:
        self.frames.append((self._clock.t, frame))

    def audio_bytes(self) -> bytes:
        """Everything the sink received that wasn't a pure-silence frame
        (the sine test signal never yields 20 ms of exact zeros)."""
        return b"".join(f for _, f in self.frames if f != SILENCE_FRAME)


def sine_pcm(samples: int, *, phase_offset: int = 0) -> bytes:
    out = bytearray()
    for i in range(samples):
        value = int(
            SINE_AMPLITUDE
            * math.sin(2 * math.pi * SINE_FREQ_HZ * (i + phase_offset) / TTS_SAMPLE_RATE)
        )
        out += struct.pack("<h", value)
    return bytes(out)


def rms(pcm: bytes) -> float:
    values = struct.unpack(f"<{len(pcm) // 2}h", pcm)
    return math.sqrt(sum(v * v for v in values) / max(1, len(values)))


def upsample_reference(chunks: list[bytes]) -> bytes:
    """The byte-exact 48 kHz expectation: the same chunk framing through
    a fresh PyAV resampler, flushed."""
    resampler = av.AudioResampler(format="s16", layout="mono", rate=WIRE_SAMPLE_RATE)
    out = bytearray()
    pts = 0
    for pcm in chunks:
        samples = len(pcm) // 2
        frame = av.AudioFrame(format="s16", layout="mono", samples=samples)
        frame.sample_rate = TTS_SAMPLE_RATE
        frame.time_base = Fraction(1, TTS_SAMPLE_RATE)
        frame.pts = pts
        pts += samples
        frame.planes[0].update(pcm)
        for resampled in resampler.resample(frame):
            out += bytes(resampled.planes[0])[: resampled.samples * 2]
    for resampled in resampler.resample(None):
        out += bytes(resampled.planes[0])[: resampled.samples * 2]
    return bytes(out)


async def pump(task: asyncio.Task[None], sink: RecordingSink, count: int) -> None:
    """Yield to the paced loop until the sink holds >= count frames."""
    while len(sink.frames) < count:
        if task.done():
            task.result()  # surface a crash instead of hanging the test
            raise AssertionError("egress.run() ended before emitting enough frames")
        await asyncio.sleep(0)


async def stop_and_join(egress: AudioEgress, task: asyncio.Task[None]) -> None:
    egress.stop()
    await task


async def test_frames_leave_on_exact_20ms_boundaries() -> None:
    clock = FakeClock()
    sink = RecordingSink(clock)
    egress = AudioEgress(now=clock.now, sleep=clock.sleep)
    chunks = [sine_pcm(480, phase_offset=k * 480) for k in range(10)]  # 10 x 20 ms
    for k, pcm in enumerate(chunks):
        egress.enqueue(TtsChunk(pcm=pcm, sentence_no=0))
        assert k >= 0
    egress.drain()

    task = asyncio.create_task(egress.run(sink))
    await pump(task, sink, 12)
    await stop_and_join(egress, task)

    timestamps = [ts for ts, _ in sink.frames]
    for i, ts in enumerate(timestamps):
        assert ts == pytest.approx(i * FRAME_INTERVAL_S, abs=1e-9)
    # 200 ms in at 24 kHz -> exactly 10 audio frames at 48 kHz, byte-equal
    # to the same chunks through a reference resampler.
    reference = upsample_reference(chunks)
    audio_frames = [f for _, f in sink.frames if f != SILENCE_FRAME]
    assert b"".join(audio_frames) == reference
    assert egress.played_ms == 200


async def test_underrun_emits_silence_and_cadence_never_breaks() -> None:
    clock = FakeClock()
    sink = RecordingSink(clock)
    egress = AudioEgress(now=clock.now, sleep=clock.sleep)
    egress.enqueue(TtsChunk(pcm=sine_pcm(1200), sentence_no=0))  # 50 ms only
    egress.drain()

    task = asyncio.create_task(egress.run(sink))
    await pump(task, sink, 6)

    # 50 ms -> 2 full frames + one half-real half-padded frame; the
    # padded tail counts only its real samples (played_ms honesty).
    assert sink.frames[0][1] != SILENCE_FRAME
    assert sink.frames[1][1] != SILENCE_FRAME
    third = sink.frames[2][1]
    assert third[:FRAME_BYTES // 2] != bytes(FRAME_BYTES // 2)
    assert third[FRAME_BYTES // 2 :] == bytes(FRAME_BYTES // 2)
    assert all(f == SILENCE_FRAME for _, f in sink.frames[3:])
    assert egress.played_ms == 50

    # Late audio resumes playout without ever having starved the cadence.
    egress.enqueue(TtsChunk(pcm=sine_pcm(480, phase_offset=1200), sentence_no=1))
    egress.drain()
    resumed_at = len(sink.frames)
    await pump(task, sink, resumed_at + 2)
    await stop_and_join(egress, task)

    assert any(f != SILENCE_FRAME for _, f in sink.frames[resumed_at:])
    assert egress.played_ms == 70
    timestamps = [ts for ts, _ in sink.frames]
    for i, ts in enumerate(timestamps):
        assert ts == pytest.approx(i * FRAME_INTERVAL_S, abs=1e-9)


async def test_pause_ducks_to_silence_and_resume_continues_byte_exact() -> None:
    clock = FakeClock()
    sink = RecordingSink(clock)
    egress = AudioEgress(now=clock.now, sleep=clock.sleep)
    chunks = [sine_pcm(480, phase_offset=k * 480) for k in range(10)]  # 200 ms
    for pcm in chunks:
        egress.enqueue(TtsChunk(pcm=pcm, sentence_no=0))
    egress.drain()

    task = asyncio.create_task(egress.run(sink))
    await pump(task, sink, 3)

    egress.pause()
    paused_at = len(sink.frames)
    played_at_pause = egress.played_ms
    assert played_at_pause == paused_at * 20  # everything so far was real audio

    await pump(task, sink, paused_at + 5)
    assert all(f == SILENCE_FRAME for _, f in sink.frames[paused_at : paused_at + 5])
    assert egress.played_ms == played_at_pause  # duck silence never counts

    egress.resume()
    resumed_at = len(sink.frames)
    await pump(task, sink, resumed_at + (10 - paused_at) + 1)
    await stop_and_join(egress, task)

    # The queue was retained byte-exact: all real audio ever played, in
    # order, equals the reference stream — no skip, no repeat.
    assert sink.audio_bytes() == upsample_reference(chunks)
    assert egress.played_ms == 200


async def test_flush_drops_exactly_the_unplayed_remainder_per_sentence() -> None:
    clock = FakeClock()
    sink = RecordingSink(clock)
    egress = AudioEgress(now=clock.now, sleep=clock.sleep)
    # Sentence 0 as two chunks (they must merge into one segment),
    # sentence 1 as one chunk: 200 ms + 200 ms queued.
    egress.enqueue(TtsChunk(pcm=sine_pcm(2400), sentence_no=0))
    egress.enqueue(TtsChunk(pcm=sine_pcm(2400, phase_offset=2400), sentence_no=0))
    egress.enqueue(TtsChunk(pcm=sine_pcm(4800, phase_offset=4800), sentence_no=1))
    egress.drain()

    task = asyncio.create_task(egress.run(sink))
    await pump(task, sink, 5)

    played_frames = len(sink.frames)  # every frame so far was real audio
    result = egress.flush()

    assert result.played_ms == played_frames * 20 == egress.played_ms
    assert result.dropped_ms == 400 - played_frames * 20
    # Per-sentence accounting is honest only to within the resampler's
    # ~0.7 ms filter-delay tail at chunk boundaries, and each value
    # rounds down individually (FlushResult docstring / judgment call 6)
    # — so sentence order and totals are exact, per-sentence ms is +/-1.
    assert [no for no, _ in result.dropped_sentences] == [0, 1]
    (_, dropped_s0), (_, dropped_s1) = result.dropped_sentences
    assert abs(dropped_s0 - (200 - played_frames * 20)) <= 1
    assert abs(dropped_s1 - 200) <= 1
    assert 0 <= result.dropped_ms - (dropped_s0 + dropped_s1) <= 2

    # After the flush the cadence keeps running on silence, and played_ms
    # stays frozen — the queue really is gone.
    flushed_at = len(sink.frames)
    await pump(task, sink, flushed_at + 4)
    await stop_and_join(egress, task)
    assert all(f == SILENCE_FRAME for _, f in sink.frames[flushed_at:])
    assert egress.played_ms == result.played_ms


async def test_upsample_doubles_length_and_preserves_energy() -> None:
    clock = FakeClock()
    sink = RecordingSink(clock)
    egress = AudioEgress(now=clock.now, sleep=clock.sleep)
    pcm_24k = sine_pcm(4800)  # 200 ms at 24 kHz
    egress.enqueue(TtsChunk(pcm=pcm_24k, sentence_no=0))
    egress.drain()

    task = asyncio.create_task(egress.run(sink))
    await pump(task, sink, 11)
    await stop_and_join(egress, task)

    audio = sink.audio_bytes()
    assert len(audio) == len(pcm_24k) * 2  # exact 2x after drain()
    assert rms(audio) == pytest.approx(SINE_RMS, rel=0.02)
    assert egress.played_ms == 200


async def test_empty_chunk_is_dropped_and_odd_length_rejected() -> None:
    egress = AudioEgress()
    egress.enqueue(TtsChunk(pcm=b"", sentence_no=0))  # no-op, no segment
    with pytest.raises(ValueError):
        egress.enqueue(TtsChunk(pcm=b"\x00", sentence_no=0))
    assert egress.flush().dropped_ms == 0


async def test_run_rejects_concurrent_second_loop() -> None:
    clock = FakeClock()
    sink = RecordingSink(clock)
    egress = AudioEgress(now=clock.now, sleep=clock.sleep)
    task = asyncio.create_task(egress.run(sink))
    await pump(task, sink, 1)

    with pytest.raises(RuntimeError):
        await egress.run(sink)

    await stop_and_join(egress, task)
